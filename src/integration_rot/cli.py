"""CLI: integration-rot scan | check | fix | demo"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .analyzer import analyze, print_console, to_json, to_markdown
from .deprecations import fetch_all, load_db
from .fixer import draft_fix, write_fix
from .scanner import scan_repo


def cmd_scan(args) -> int:
    scan = scan_repo(args.repo)
    print(f"Dependencies found in {args.repo}:")
    for dep in sorted(scan.dependencies, key=lambda d: (d.ecosystem, d.name)):
        ver = dep.version or dep.spec or "?"
        vendor = f" [{dep.vendor}]" if dep.vendor else ""
        print(f"  {dep.ecosystem:8} {dep.name} {ver}{vendor}  ({dep.manifest})")
    if scan.direct_calls:
        print("\nDirect vendor API calls:")
        for c in scan.direct_calls:
            print(f"  [{c.vendor}] {c.file}:{c.line}: {c.snippet}")
    return 0


def cmd_check(args) -> int:
    scan = scan_repo(args.repo)
    entries = load_db(args.db) if args.db else fetch_all(
        sorted({d.vendor for d in scan.dependencies if d.vendor} |
               {c.vendor for c in scan.direct_calls}))
    today = date.fromisoformat(args.today) if args.today else None
    report = analyze(scan, entries, today=today)
    if args.format == "json":
        print(to_json(report))
    elif args.format == "md":
        print(to_markdown(report))
    else:
        print_console(report)
    # exit code 1 if any critical/high findings (CI-friendly)
    return 1 if any(f.risk in ("critical", "high")
                    for f in report.findings) else 0


def cmd_fix(args) -> int:
    try:
        draft = draft_fix(args.entry, args.repo, args.file,
                          module=args.module, func_name=args.func)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if draft.empty():
        print("Nothing to fix.")
        for n in draft.notes:
            print(f"  note: {n}")
        return 0
    for change in draft.changes:
        print(change.diff)
    for test_path, content in draft.tests:
        print(f"\n--- generated contract test: {test_path} ---")
        print(content)
    print("\nNotes:")
    for n in draft.notes:
        print(f"  - {n}")
    if args.apply:
        written = write_fix(args.repo, draft)
        print(f"\nApplied. Wrote: {', '.join(written)}")
    else:
        print("\nDry run — pass --apply to write the patch and test to the repo.")
    return 0


def cmd_demo(args) -> int:
    demo_repo = Path(__file__).resolve().parent.parent.parent / "demo" / "sample-app"
    print(f"=== 1. SCAN  ({demo_repo}) ===")
    scan = scan_repo(demo_repo)
    for dep in sorted(scan.dependencies, key=lambda d: (d.ecosystem, d.name)):
        ver = dep.version or dep.spec or "?"
        print(f"  {dep.ecosystem:8} {dep.name} {ver} [{dep.vendor}]")
    for c in scan.direct_calls:
        print(f"  direct call [{c.vendor}] {c.file}:{c.line}")

    print("\n=== 2. CHECK (deprecation analysis) ===")
    entries = load_db()
    report = analyze(scan, entries, today=date(2026, 9, 26))
    print_console(report)

    print("=== 3. FIX (draft for stripe-charges-api) ===")
    draft = draft_fix("stripe-charges-api", demo_repo, "app.py",
                      module="app", func_name="create_charge")
    for change in draft.changes:
        print(change.diff)
    print(f"\nContract test would be written to: {draft.tests[0][0]}")
    print("\nDemo complete. Run `integration-rot fix --apply` to write changes,")
    print("or see README.md for the full workflow.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="integration-rot",
        description="Integration-rot autopilot MVP: find deprecated API usage, draft fixes.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="inventory third-party API dependencies")
    s.add_argument("repo", help="path to target repo")
    s.set_defaults(fn=cmd_scan)

    c = sub.add_parser("check", help="analyze repo against deprecation DB")
    c.add_argument("repo", help="path to target repo")
    c.add_argument("--db", default=None, help="path to deprecations.json")
    c.add_argument("--today", default=None, help="override date YYYY-MM-DD (for tests/demo)")
    c.add_argument("--format", choices=["console", "json", "md"], default="console")
    c.set_defaults(fn=cmd_check)

    f = sub.add_parser("fix", help="draft a fix for a deprecation entry")
    f.add_argument("repo", help="path to target repo")
    f.add_argument("--entry", required=True, help="deprecation entry id, e.g. stripe-charges-api")
    f.add_argument("--file", required=True, help="repo-relative source file to patch")
    f.add_argument("--module", default="app", help="python module name for test import")
    f.add_argument("--func", default="create_charge", help="function name for test import")
    f.add_argument("--apply", action="store_true", help="write patch + test to repo")
    f.set_defaults(fn=cmd_fix)

    d = sub.add_parser("demo", help="run the full pipeline on the bundled sample app")
    d.set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
