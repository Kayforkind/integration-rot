"""Analyzer: cross-references scanned dependencies against the deprecation DB
and produces a ranked risk report."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from .models import Dependency, DeprecationEntry, Finding, Report, ScanResult
from .scanner import version_satisfies

RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    # month-precision dates (e.g. "2023-04") are treated as the first of the month
    if len(s) == 7 and s[4] == "-":
        s += "-01"
    return date.fromisoformat(s)


def _package_matches(dep: Dependency, criterion: dict) -> bool:
    if criterion.get("ecosystem") and dep.ecosystem != criterion["ecosystem"]:
        return False
    if criterion.get("name") and dep.name.lower() != criterion["name"].lower():
        return False
    spec = criterion.get("version_spec")
    if spec and not version_satisfies(dep.version, spec):
        return False
    return True


def _pattern_hits(entry: DeprecationEntry, repo: str,
                  source_files: list[str]) -> list[str]:
    """Return evidence strings for code patterns matched in repo source."""
    hits = []
    if not entry.code_patterns:
        return hits
    compiled = [re.compile(p) for p in entry.code_patterns]
    for rel in source_files:
        try:
            lines = Path(repo, rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            for rx in compiled:
                if rx.search(line):
                    hits.append(f"{rel}:{i}: `{line.strip()[:120]}` "
                                f"(matches {entry.id})")
                    break
    return hits


def _risk_for(entry: DeprecationEntry, today: date) -> tuple[str, int | None]:
    sunset = _parse_date(entry.sunset)
    days = (sunset - today).days if sunset else None

    if days is not None and days < 0:
        risk = "critical"          # already sunset: broken or breaking imminently
    elif days is not None and days <= 90:
        risk = "high"
    elif days is not None and days <= 180:
        risk = "medium"
    elif days is not None:
        risk = "low"               # dated, but far out
    elif entry.severity == "breaking":
        risk = "high"              # breaking, no date: treat seriously
    elif entry.severity == "warning":
        risk = "medium"
    else:
        risk = "low"

    # breaking severity bumps one level (floor at critical)
    if entry.severity == "breaking" and days is not None and days >= 0:
        order = ["low", "medium", "high", "critical"]
        risk = order[min(order.index(risk) + 1, 3)]
    return risk, days


def analyze(scan: ScanResult, entries: list[DeprecationEntry],
            today: date | None = None) -> Report:
    """Match deprecations against a scan result; return a ranked Report."""
    today = today or date.today()
    findings: list[Finding] = []

    for entry in entries:
        # package-level match: entry applies if ANY of its package criteria
        # match a scanned dep (empty criteria = vendor-wide, needs pattern evidence)
        matched_dep: Dependency | None = None
        if entry.packages:
            for dep in scan.dependencies:
                if (dep.vendor or "").lower() == entry.vendor.lower() and \
                        any(_package_matches(dep, c) for c in entry.packages):
                    matched_dep = dep
                    break
            if matched_dep is None and not entry.code_patterns:
                # package-only entry with no dependency match and no code
                # patterns to fall back on: nothing to check.
                continue
            # Otherwise (no dep match, but the entry has code patterns):
            # fall through to pattern matching below. A pattern hit still
            # flags the finding (repos without manifests are common); the
            # "no pattern hits and no dep" skip further down decides.
        else:
            # vendor-wide entry: only relevant if the vendor is used at all
            vendor_used = any((d.vendor or "").lower() == entry.vendor.lower()
                              for d in scan.dependencies) or \
                any(c.vendor.lower() == entry.vendor.lower() for c in scan.direct_calls)
            if not vendor_used:
                continue

        evidence: list[str] = []
        if matched_dep:
            ver = matched_dep.version or matched_dep.spec or "unknown version"
            evidence.append(f"dependency `{matched_dep.name}` {ver} "
                            f"({matched_dep.ecosystem}, {matched_dep.manifest})")

        pattern_hits = _pattern_hits(entry, scan.repo, scan.source_files)
        evidence.extend(pattern_hits)

        # If the entry names code patterns but none matched and no dep matched
        # with certainty, it's not actionable — skip to avoid noise.
        if entry.code_patterns and not pattern_hits and matched_dep is None:
            continue
        if entry.code_patterns and not pattern_hits and entry.packages:
            # package matches but the deprecated usage isn't present in code:
            # still worth flagging (SDK may expose it), mark as potential.
            evidence.append("note: deprecated API pattern not found in scanned "
                            "source — verify usage before acting")

        risk, days = _risk_for(entry, today)
        findings.append(Finding(entry=entry, dependency=matched_dep,
                                evidence=evidence, risk=risk,
                                days_to_sunset=days))

    findings.sort(key=lambda f: (RISK_ORDER[f.risk], f.entry.vendor, f.entry.id))
    return Report(repo=scan.repo,
                  generated=datetime.now().isoformat(timespec="seconds"),
                  findings=findings)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def to_markdown(report: Report) -> str:
    lines = [f"# Integration-rot report — `{report.repo}`",
             f"_Generated {report.generated}_", ""]
    counts = report.counts
    lines.append("**Summary:** " + ", ".join(
        f"{counts[k]} {k}" for k in ("critical", "high", "medium", "low")))
    lines.append("")
    if not report.findings:
        lines.append("No deprecated API usage detected. 🎉")
        return "\n".join(lines)
    for f in report.findings:
        e = f.entry
        sunset = f"Sunsets **{e.sunset}**" + (
            f" ({f.days_to_sunset} days)" if f.days_to_sunset is not None and f.days_to_sunset >= 0
            else " (already sunset!)" if f.days_to_sunset is not None else "")
        lines.append(f"## [{f.risk.upper()}] {e.vendor}: {e.title}")
        lines.append(f"- **Sunset:** {sunset if e.sunset else 'not announced'}")
        lines.append(f"- **Announced:** {e.announced or 'not published'} — [source]({e.source_url})")
        lines.append("- **Evidence:**")
        for ev in f.evidence:
            lines.append(f"  - {ev}")
        lines.append(f"- **Migration:** {e.migration}")
        lines.append(f"- **Auto-fix available:** {'yes' if e.fix_available else 'no'}")
        lines.append("")
    return "\n".join(lines)


def to_json(report: Report) -> str:
    def finding_dict(f: Finding) -> dict:
        e = f.entry
        return {"id": e.id, "vendor": e.vendor, "title": e.title,
                "risk": f.risk, "days_to_sunset": f.days_to_sunset,
                "sunset": e.sunset, "announced": e.announced,
                "severity": e.severity, "evidence": f.evidence,
                "migration": e.migration, "source_url": e.source_url,
                "fix_available": e.fix_available}
    return json.dumps({"repo": report.repo, "generated": report.generated,
                       "counts": report.counts,
                       "findings": [finding_dict(f) for f in report.findings]},
                      indent=2)


def print_console(report: Report) -> None:
    counts = report.counts
    print(f"\nIntegration-rot report for {report.repo}")
    print(f"  critical={counts['critical']} high={counts['high']} "
          f"medium={counts['medium']} low={counts['low']}\n")
    for f in report.findings:
        e = f.entry
        when = f"sunset {e.sunset}" if e.sunset else "no sunset date"
        auto = " [auto-fix available]" if e.fix_available else ""
        print(f"  [{f.risk.upper():8}] {e.vendor}: {e.title} ({when}){auto}")
        for ev in f.evidence:
            print(f"             - {ev}")
    print()
