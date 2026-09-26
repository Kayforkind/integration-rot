"""Dependency scanner.

Walks a target repo, parses manifest files (npm, PyPI, Go, Bundler, Maven),
resolves versions where a lockfile is present, maps packages to vendors, and
heuristically finds hardcoded calls to vendor API hostnames in source files.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Dependency, DirectCall, ScanResult
from .vendor_map import HOST_TO_VENDOR, vendor_for_package, vendor_for_host

MANIFEST_FILES = {
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "go.mod",
    "Gemfile",
    "pom.xml",
}

SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java"}

# matches e.g. https://api.stripe.com/v1/charges  -> host "api.stripe.com"
_URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")


# ---------------------------------------------------------------------------
# Version-spec matching (small pragmatic subset: ==, <, <=, >, >=, ^, ~, ranges)
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple:
    """Parse '8.215.0' (or 'v8.215.0') into a comparable tuple of ints."""
    v = v.strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for p in v.split("."):
        m = re.match(r"(\d+)", p)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _cmp(a: str, b: str) -> int:
    pa, pb = _parse_version(a), _parse_version(b)
    return (pa > pb) - (pa < pb)


def version_satisfies(version: str | None, spec: str | None) -> bool:
    """True if `version` satisfies a simple spec like '<9.0.0', '^8', '>=1.2,<2'."""
    if not version or not spec:
        return True  # unknown version/spec -> treat as possibly affected
    spec = spec.strip()
    for clause in [c.strip() for c in spec.split(",") if c.strip()]:
        if clause.startswith("^"):
            base = clause[1:].strip()
            major = _parse_version(base)[0]
            if not (_cmp(version, base) >= 0 and _parse_version(version)[0] == major):
                return False
        elif clause.startswith("~"):
            base = clause[1:].strip()
            p = _parse_version(base)
            upper = (p[0], p[1] + 1, 0)
            if not (_cmp(version, base) >= 0 and _parse_version(version) < upper):
                return False
        else:
            m = re.match(r"(<=|>=|==|!=|<|>)\s*(.+)", clause)
            if not m:
                # bare version like "8.0.0" -> exact-ish match on major.minor.patch
                if _cmp(version, clause) != 0:
                    return False
                continue
            op, ref = m.group(1), m.group(2).strip()
            c = _cmp(version, ref)
            ok = {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0,
                  "==": c == 0, "!=": c != 0}[op]
            if not ok:
                return False
    return True


# ---------------------------------------------------------------------------
# Manifest parsers
# ---------------------------------------------------------------------------

def _parse_package_json(path: Path, rel: str) -> list[Dependency]:
    data = json.loads(path.read_text(encoding="utf-8"))
    deps = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            deps.append(Dependency(name=name, spec=str(spec), ecosystem="npm",
                                   manifest=rel, vendor=vendor_for_package(name)))
    return deps


def _parse_package_lock(path: Path, rel: str) -> dict[str, str]:
    """Return {package_name: resolved_version} from a lockfile."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for key, val in (data.get("packages") or {}).items():
        if key.startswith("node_modules/") and "/" not in key[len("node_modules/"):]:
            name = key[len("node_modules/"):]
            if isinstance(val, dict) and val.get("version"):
                out[name] = val["version"]
    # legacy lockfileVersion=1 format
    for name, val in (data.get("dependencies") or {}).items():
        if isinstance(val, dict) and val.get("version") and name not in out:
            out[name] = val["version"]
    return out


def _parse_requirements(path: Path, rel: str) -> list[Dependency]:
    deps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+)\s*(.*)$", line)
        if not m:
            continue
        name = re.sub(r"\[.*\]", "", m.group(1))
        spec = m.group(2).strip() or None
        version = None
        if spec:
            mm = re.match(r"==\s*([^,;\s]+)", spec)
            if mm:
                version = mm.group(1)
        deps.append(Dependency(name=name, version=version, spec=spec,
                               ecosystem="pypi", manifest=rel,
                               vendor=vendor_for_package(name)))
    return deps


def _parse_go_mod(path: Path, rel: str) -> list[Dependency]:
    deps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        m = re.match(r"^([^\s]+)\s+v([^\s]+)", line)
        if m and not line.startswith("module") and not line.startswith("go "):
            name = m.group(1)
            deps.append(Dependency(name=name, version=m.group(2), spec="v" + m.group(2),
                                   ecosystem="go", manifest=rel,
                                   vendor=vendor_for_package(name.split("/")[-1])))
    return deps


def _parse_gemfile(path: Path, rel: str) -> list[Dependency]:
    deps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"""\s*gem\s+['"]([^'"]+)['"]\s*(?:,\s*['"]([^'"]+)['"])?""", line)
        if m:
            deps.append(Dependency(name=m.group(1), spec=m.group(2),
                                   ecosystem="bundler", manifest=rel,
                                   vendor=vendor_for_package(m.group(1))))
    return deps


def _parse_pom_xml(path: Path, rel: str) -> list[Dependency]:
    deps = []
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return deps
    ns = {"m": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {"m": ""}
    for dep in root.findall(".//m:dependency", ns) or root.findall(".//dependency"):
        gid = dep.find("m:groupId", ns)
        aid = dep.find("m:artifactId", ns)
        ver = dep.find("m:version", ns)
        gid = gid.text if gid is not None else ""
        aid = aid.text if aid is not None else ""
        name = f"{gid}:{aid}".strip(":")
        deps.append(Dependency(name=name, version=ver.text if ver is not None else None,
                               ecosystem="maven", manifest=rel,
                               vendor=vendor_for_package(aid)))
    return deps


PARSERS = {
    "package.json": _parse_package_json,
    "requirements.txt": _parse_requirements,
    "go.mod": _parse_go_mod,
    "Gemfile": _parse_gemfile,
    "pom.xml": _parse_pom_xml,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_repo(repo_path: str | Path) -> ScanResult:
    """Inventory third-party API dependencies of the repo at `repo_path`."""
    repo = Path(repo_path)
    if not repo.is_dir():
        raise ValueError(f"not a directory: {repo_path}")

    dependencies: list[Dependency] = []
    lock_versions: dict[str, str] = {}

    for dirpath, _dirnames, filenames in os.walk(repo):
        # skip vendored / dependency dirs
        if any(skip in Path(dirpath).parts for skip in
               ("node_modules", ".git", "__pycache__", "venv", ".venv", "vendor")):
            continue
        for fname in filenames:
            if fname not in PARSERS and fname != "package-lock.json":
                continue
            full = Path(dirpath) / fname
            rel = str(full.relative_to(repo))
            try:
                if fname == "package-lock.json":
                    lock_versions.update(_parse_package_lock(full, rel))
                else:
                    dependencies.extend(PARSERS[fname](full, rel))
            except (OSError, ValueError, json.JSONDecodeError):
                continue  # best effort: skip unreadable manifests

    # resolve npm versions from lockfile where available
    for dep in dependencies:
        if dep.ecosystem == "npm" and not dep.version and dep.name in lock_versions:
            dep.version = lock_versions[dep.name]

    # de-duplicate: prefer entries that carry a resolved version
    by_key: dict[tuple, Dependency] = {}
    for dep in dependencies:
        key = (dep.ecosystem, dep.name.lower())
        if key not in by_key or (dep.version and not by_key[key].version):
            by_key[key] = dep

    source_files = _collect_source_files(repo)
    direct_calls = _find_direct_calls(repo, source_files)

    return ScanResult(repo=str(repo), dependencies=list(by_key.values()),
                      direct_calls=direct_calls, source_files=source_files)


def _collect_source_files(repo: Path) -> list[str]:
    out = []
    for dirpath, _d, filenames in os.walk(repo):
        if any(skip in Path(dirpath).parts for skip in
               ("node_modules", ".git", "__pycache__", "venv", ".venv", "vendor")):
            continue
        for fname in filenames:
            if Path(fname).suffix in SOURCE_EXTENSIONS:
                out.append(str((Path(dirpath) / fname).relative_to(repo)))
    return sorted(out)


def _find_direct_calls(repo: Path, source_files: list[str]) -> list[DirectCall]:
    calls = []
    for rel in source_files:
        try:
            lines = (repo / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            for m in _URL_RE.finditer(line):
                vendor = vendor_for_host(m.group(1))
                if vendor:
                    calls.append(DirectCall(vendor=vendor, host=m.group(1),
                                            file=rel, line=i, snippet=line.strip()[:160]))
    return calls
