"""Schema-drift detection.

Compares a pinned vendor OpenAPI snapshot (data/openapi_snapshots/<vendor>.json)
against a fresh spec (URL or local path) and reports added/removed/changed
endpoints, parameters, and request/response fields.

The pinned snapshot is a curated excerpt of high-traffic endpoints; the fresh
spec may be the vendor's full OpenAPI document — only the snapshot's endpoints
are compared, so a full spec works as input.

`save_snapshot` / `list_snapshots` / `check_drift_history` keep a timestamped
history under data/openapi_snapshots/<vendor>/ so drift can be tracked over
time, not just against the original pin.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "openapi_snapshots"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class EndpointChange:
    path: str
    method: str            # "GET" | "POST" | ...
    details: list = field(default_factory=list)  # human-readable change lines


@dataclass
class SchemaDiff:
    vendor: str
    added: list = field(default_factory=list)     # list[(method, path)]
    removed: list = field(default_factory=list)   # list[(method, path)]
    changed: list = field(default_factory=list)   # list[EndpointChange]

    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self) -> str:
        return (f"{self.vendor}: {len(self.added)} endpoint(s) added, "
                f"{len(self.removed)} removed, {len(self.changed)} changed")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_spec(path_or_url: str | Path) -> dict:
    """Load an OpenAPI document from a local path or an http(s) URL."""
    s = str(path_or_url)
    if s.startswith(("http://", "https://")):
        with urllib.request.urlopen(s, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return json.loads(Path(s).read_text(encoding="utf-8"))


def load_snapshot(vendor: str,
                 snapshot_path: str | Path | None = None) -> dict:
    """Load the pinned snapshot for a vendor. Returns the parsed JSON doc."""
    path = Path(snapshot_path) if snapshot_path else \
        SNAPSHOT_DIR / f"{vendor.lower()}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no pinned OpenAPI snapshot for vendor '{vendor}' at {path} "
            f"(add one under data/openapi_snapshots/)")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Normalization: full OpenAPI dict -> comparable endpoint records
# ---------------------------------------------------------------------------

def _resolve(node, root):
    if isinstance(node, dict) and set(node) == {"$ref"}:
        ref = node["$ref"]
        if ref.startswith("#/"):
            cur = root
            for part in ref[2:].split("/"):
                cur = cur[part]
            return cur
    return node


def _field_type(schema, root) -> str:
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return "unknown"
    t = schema.get("type", "object")
    if t == "array":
        return f"array[{_field_type(schema.get('items', {}), root)}]"
    return t


def _fields_of(schema, root) -> dict:
    schema = _resolve(schema, root) or {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return {k: _field_type(v, root) for k, v in props.items()}


def normalize_spec(spec: dict, only: set[tuple[str, str]] | None = None) -> list[dict]:
    """Extract comparable endpoint records from a full OpenAPI document.

    `only`: optional set of (METHOD, path) to keep — used to restrict a full
    vendor spec to the endpoints covered by the pinned snapshot.
    """
    root = spec
    out = []
    for path, ops in (spec.get("paths") or {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            key = (method.upper(), path)
            if only is not None and key not in only:
                continue
            params = []
            for p in op.get("parameters", []):
                p = _resolve(p, root)
                if isinstance(p, dict):
                    params.append({"name": p.get("name"), "in": p.get("in"),
                                   "type": _field_type(p.get("schema", {}), root)})
            req_fields: dict = {}
            rb = (op.get("requestBody", {}) or {}).get("content") or {}
            if rb:
                ctype = sorted(rb.keys())[0]
                req_fields = _fields_of((rb[ctype] or {}).get("schema"), root)
            resp_fields: dict = {}
            responses = op.get("responses", {}) or {}
            status = next((s for s in ("200", "201") if s in responses),
                          next(iter(responses), None))
            if status:
                content = (responses[status] or {}).get("content") or {}
                if content:
                    ctype = sorted(content.keys())[0]
                    resp_fields = _fields_of((content[ctype] or {}).get("schema"), root)
            out.append({"path": path, "method": method.upper(),
                        "summary": op.get("summary", ""),
                        "parameters": params,
                        "request_fields": req_fields,
                        "response_fields": resp_fields})
    return out


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def _key(ep: dict) -> tuple[str, str]:
    return (ep["method"].upper(), ep["path"])


def _diff_params(old: list[dict], new: list[dict]) -> list[str]:
    details = []
    o = {(p["name"], p["in"]): p.get("type") for p in old}
    n = {(p["name"], p["in"]): p.get("type") for p in new}
    for name, loc in sorted(set(n) - set(o)):
        details.append(f"parameter `{name}` ({loc}) added")
    for name, loc in sorted(set(o) - set(n)):
        details.append(f"parameter `{name}` ({loc}) removed")
    for name, loc in sorted(set(o) & set(n)):
        if o[(name, loc)] != n[(name, loc)]:
            details.append(f"parameter `{name}` ({loc}) type changed: "
                           f"{o[(name, loc)]} -> {n[(name, loc)]}")
    return details


def _diff_fields(old: dict, new: dict, label: str) -> list[str]:
    details = []
    for name in sorted(set(new) - set(old)):
        details.append(f"{label} field `{name}` added")
    for name in sorted(set(old) - set(new)):
        details.append(f"{label} field `{name}` removed")
    for name in sorted(set(old) & set(new)):
        if old[name] != new[name]:
            details.append(f"{label} field `{name}` type changed: "
                           f"{old[name]} -> {new[name]}")
    return details


def diff_specs(old_endpoints: list[dict], new_endpoints: list[dict],
               vendor: str = "") -> SchemaDiff:
    """Diff two normalized endpoint lists. Returns a SchemaDiff."""
    old_map = {_key(e): e for e in old_endpoints}
    new_map = {_key(e): e for e in new_endpoints}
    diff = SchemaDiff(vendor=vendor)
    for key in sorted(set(new_map) - set(old_map)):
        diff.added.append(key)
    for key in sorted(set(old_map) - set(new_map)):
        diff.removed.append(key)
    for key in sorted(set(old_map) & set(new_map)):
        o, n = old_map[key], new_map[key]
        details = []
        details.extend(_diff_params(o.get("parameters", []), n.get("parameters", [])))
        details.extend(_diff_fields(o.get("request_fields", {}),
                                    n.get("request_fields", {}), "request"))
        details.extend(_diff_fields(o.get("response_fields", {}),
                                    n.get("response_fields", {}), "response"))
        if details:
            diff.changed.append(EndpointChange(path=key[1], method=key[0],
                                               details=details))
    return diff


def check_drift(vendor: str, spec_source: str | Path,
                snapshot_path: str | Path | None = None) -> SchemaDiff:
    """Diff a fresh spec against the vendor's pinned snapshot."""
    snapshot = load_snapshot(vendor, snapshot_path)
    old = snapshot["endpoints"]
    fresh = load_spec(spec_source)
    only = {_key(e) for e in old}
    if isinstance(fresh, dict) and "endpoints" in fresh and "paths" not in fresh:
        new = fresh["endpoints"]  # already-normalized snapshot-shaped doc
    else:
        new = normalize_spec(fresh, only=only)
    return diff_specs(old, new, vendor=snapshot["_meta"].get("vendor", vendor))


def print_diff(diff: SchemaDiff) -> None:
    """Render a SchemaDiff to the console."""
    print(f"\nSchema drift for {diff.vendor}: {diff.summary()}\n")
    for method, path in diff.added:
        print(f"  [ADDED]   {method} {path}")
    for method, path in diff.removed:
        print(f"  [REMOVED] {method} {path}")
    for change in diff.changed:
        print(f"  [CHANGED] {change.method} {change.path}")
        for d in change.details:
            print(f"            - {d}")
    if diff.empty():
        print("  No drift detected — pinned snapshot matches the fresh spec. ✓")
    print()


# ---------------------------------------------------------------------------
# Snapshot history: timestamped storage + diffing over time
# ---------------------------------------------------------------------------

def history_dir(vendor: str) -> Path:
    """Directory holding timestamped snapshots for a vendor."""
    return SNAPSHOT_DIR / vendor.lower()


def _snapshot_doc(vendor: str, spec: dict) -> dict:
    """Normalize a spec (full OpenAPI doc or snapshot-shaped) into the
    snapshot document shape: {"_meta": {...}, "endpoints": [...] }."""
    if isinstance(spec, dict) and "endpoints" in spec and "paths" not in spec:
        endpoints = spec["endpoints"]
    else:
        endpoints = normalize_spec(spec)
    return {
        "_meta": {
            "vendor": vendor,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "generator": f"integration-rot {__version__}",
        },
        "endpoints": endpoints,
    }


def save_snapshot(vendor: str, spec_source: str | Path) -> Path:
    """Save a timestamped snapshot of a vendor spec. Returns the file path."""
    spec = load_spec(spec_source)
    doc = _snapshot_doc(vendor, spec)
    dest = history_dir(vendor)
    dest.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = dest / f"{ts}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def list_snapshots(vendor: str) -> list[Path]:
    """Return timestamped snapshots for a vendor, oldest first."""
    d = history_dir(vendor)
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def check_drift_history(vendor: str, spec_source: str | Path
                        ) -> tuple[SchemaDiff, Path, Path | None]:
    """Save a new timestamped snapshot and diff it against the most recent
    previous one.

    Returns (diff, new_snapshot_path, previous_snapshot_path | None).
    When no previous snapshot exists, the diff is empty and this call
    establishes the baseline.
    """
    prev_list = list_snapshots(vendor)
    prev = prev_list[-1] if prev_list else None
    new_path = save_snapshot(vendor, spec_source)
    new_doc = json.loads(new_path.read_text(encoding="utf-8"))
    if prev is None:
        return SchemaDiff(vendor=vendor), new_path, None
    old_doc = json.loads(prev.read_text(encoding="utf-8"))
    diff = diff_specs(old_doc["endpoints"], new_doc["endpoints"], vendor=vendor)
    return diff, new_path, prev
