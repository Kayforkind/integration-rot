"""CLI: integration-rot scan | check | fix | verify | drift | snapshot | fetch | propose | demo | mcp | serve | agent"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .agent import cmd_agent
from .analyzer import analyze, print_console, to_json, to_markdown
from .api_server import serve_forever
from .deprecations import draft_entries_from_feed, fetch_all, load_db
from .fixer import detect_func_name, draft_fix, write_fix
from .mcp_server import serve_stdio
from .proposer import build_pr_body, open_pr
from .scanner import scan_repo
from .schema_drift import check_drift, check_drift_history, print_diff
from .verifier import print_evidence, verify_fix


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
    vendors = sorted({d.vendor for d in scan.dependencies if d.vendor} |
                     {c.vendor for c in scan.direct_calls})
    if args.db:
        entries = load_db(args.db)
    elif vendors:
        entries = fetch_all(vendors)
    else:
        # No manifests / direct calls to attribute a vendor: pattern-based
        # detection is the only signal, so check the full DB instead of
        # an empty vendor-filtered set (which would find nothing).
        entries = load_db()
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


def _parse_params(pairs: list[str] | None) -> dict:
    """Parse KEY=VALUE extra fixer params from --param flags."""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            print(f"error: --param must be KEY=VALUE, got {p!r}", file=sys.stderr)
            sys.exit(2)
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def _resolve_func(args):
    """--func wins; otherwise auto-detect from the finding (first code-pattern
    match -> enclosing def); None lets each fixer's own default apply."""
    if getattr(args, "func", None):
        return args.func
    return detect_func_name(args.repo, args.file, args.entry)


def cmd_fix(args) -> int:
    kwargs = {"module": args.module}
    func_name = _resolve_func(args)
    if func_name:
        kwargs["func_name"] = func_name
    kwargs.update(_parse_params(args.param))
    try:
        draft = draft_fix(args.entry, args.repo, args.file, **kwargs)
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


def cmd_drift(args) -> int:
    try:
        diff = check_drift(args.vendor, args.spec, args.snapshot)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_diff(diff)
    # exit code 1 when drift is detected (CI-friendly, like `check`)
    return 1 if not diff.empty() else 0


def cmd_snapshot(args) -> int:
    diff, new_path, prev = check_drift_history(args.vendor, args.spec)
    print(f"Saved snapshot: {new_path}")
    if prev is None:
        print("First snapshot — baseline recorded, nothing to diff against yet.")
        print("Run again after the vendor publishes a new spec version.")
        return 0
    print(f"Diffed against previous snapshot: {prev.name}")
    print_diff(diff)
    # exit code 1 when drift is detected (CI-friendly, like `check`/`drift`)
    return 1 if not diff.empty() else 0


def cmd_fetch(args) -> int:
    try:
        drafts = draft_entries_from_feed(args.vendor, args.feed)
    except Exception as e:
        print(f"error: could not read feed: {e}", file=sys.stderr)
        return 2
    if not drafts:
        print(f"No deprecation signals found in {args.feed}.")
        return 0
    print(f"{len(drafts)} candidate(s) from {args.feed} — "
          "REVIEW BEFORE ADDING TO THE DB:\n")
    for d in drafts:
        print(f"- {d['candidate_title']} ({d['published'] or 'no date'})")
        print(f"  signals: {', '.join(d['matched_signals'])}")
        print(f"  link: {d['link']}")
    if args.output:
        Path(args.output).write_text(json.dumps(drafts, indent=2), encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


def cmd_verify(args) -> int:
    kwargs = {"module": args.module}
    func_name = _resolve_func(args)
    if func_name:
        kwargs["func_name"] = func_name
    kwargs.update(_parse_params(args.param))
    try:
        res = verify_fix(args.repo, args.entry, args.file, **kwargs)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print_evidence(res)
    if not res.changes_applied:
        return 0
    return 0 if res.passed else 1


def cmd_propose(args) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GITHUB_TOKEN is not set — export a token with `repo` "
              "scope to open PRs", file=sys.stderr)
        return 2
    kwargs = {"module": args.module}
    func_name = _resolve_func(args)
    if func_name:
        kwargs["func_name"] = func_name
    kwargs.update(_parse_params(args.param))
    try:
        draft = draft_fix(args.entry, args.repo, args.file, **kwargs)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if draft.empty():
        print("Nothing to propose — the fixer found no matching code.")
        for n in draft.notes:
            print(f"  note: {n}")
        return 0

    entries = {e.id: e for e in load_db()}
    entry = entries.get(args.entry)
    if entry is None:
        print(f"error: unknown deprecation entry '{args.entry}'", file=sys.stderr)
        return 2
    scan = scan_repo(args.repo)
    report = analyze(scan, [entry])
    findings = [f for f in report.findings if f.entry.id == entry.id]

    title = args.title or (f"fix: migrate deprecated {entry.vendor} API "
                           f"({entry.id}) [integration-rot]")
    body = build_pr_body(entry, draft, findings)
    try:
        pr = open_pr(args.owner, args.repo_name, head=args.head, base=args.base,
                     title=title, body=body, token=token, draft_pr=args.draft)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Pull request opened: {pr.get('html_url')}")
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


def cmd_mcp(args) -> int:
    return serve_stdio()


def cmd_serve(args) -> int:
    return serve_forever(port=args.port, host=args.host)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="integration-rot",
        description="Integration-rot autopilot: find deprecated API usage, "
                    "detect schema drift, draft fixes, and propose PRs.")
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
    f.add_argument("--func", default=None, help="function name for test import (auto-detected from the finding when omitted)")
    f.add_argument("--param", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="extra fixer param, e.g. --param start_func=begin_otp (repeatable)")
    f.add_argument("--apply", action="store_true", help="write patch + test to repo")
    f.set_defaults(fn=cmd_fix)

    dr = sub.add_parser("drift", help="diff a vendor OpenAPI spec against the pinned snapshot")
    dr.add_argument("--vendor", required=True, help="vendor name, e.g. stripe")
    dr.add_argument("--spec", required=True,
                    help="fresh OpenAPI spec: local path or http(s) URL")
    dr.add_argument("--snapshot", default=None,
                    help="override pinned snapshot path "
                         "(default: data/openapi_snapshots/<vendor>.json)")
    dr.set_defaults(fn=cmd_drift)

    sn = sub.add_parser("snapshot",
                        help="save a timestamped OpenAPI snapshot and diff it "
                             "against the previous one (drift history)")
    sn.add_argument("--vendor", required=True, help="vendor name, e.g. stripe")
    sn.add_argument("--spec", required=True,
                    help="fresh OpenAPI spec: local path or http(s) URL")
    sn.set_defaults(fn=cmd_snapshot)

    fe = sub.add_parser("fetch",
                        help="scan a vendor changelog RSS/Atom feed for "
                             "deprecation signals (candidates for review)")
    fe.add_argument("--vendor", required=True, help="vendor name, e.g. twilio")
    fe.add_argument("--feed", required=True,
                    help="feed URL or local XML file")
    fe.add_argument("--output", default=None,
                    help="write candidate drafts to a JSON file")
    fe.set_defaults(fn=cmd_fetch)

    v = sub.add_parser("verify",
                       help="apply a fix draft to an isolated copy of the repo "
                            "and run its contract tests (executed verification)")
    v.add_argument("repo", help="path to target repo")
    v.add_argument("--entry", required=True, help="deprecation entry id, e.g. stripe-charges-api")
    v.add_argument("--file", required=True, help="repo-relative source file to patch")
    v.add_argument("--module", default="app", help="python module name for test import")
    v.add_argument("--func", default=None, help="function name for test import (auto-detected from the finding when omitted)")
    v.add_argument("--param", action="append", default=[],
                   metavar="KEY=VALUE",
                   help="extra fixer param (repeatable)")
    v.set_defaults(fn=cmd_verify)

    pr = sub.add_parser("propose", help="open a GitHub PR with the fix draft")
    pr.add_argument("repo", help="path to target repo (changes must be on --head already)")
    pr.add_argument("--entry", required=True, help="deprecation entry id")
    pr.add_argument("--file", required=True, help="repo-relative source file to patch")
    pr.add_argument("--owner", required=True, help="GitHub repo owner/org")
    pr.add_argument("--repo-name", required=True, help="GitHub repo name")
    pr.add_argument("--head", required=True, help="branch containing the fix")
    pr.add_argument("--base", default="main", help="base branch (default: main)")
    pr.add_argument("--title", default=None, help="PR title (default: generated)")
    pr.add_argument("--draft", action="store_true", help="open as a draft PR")
    pr.add_argument("--module", default="app", help="python module name for test import")
    pr.add_argument("--func", default=None, help="function name for test import (auto-detected from the finding when omitted)")
    pr.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                    help="extra fixer param (repeatable)")
    pr.set_defaults(fn=cmd_propose)

    d = sub.add_parser("demo", help="run the full pipeline on the bundled sample app")
    d.set_defaults(fn=cmd_demo)

    m = sub.add_parser("mcp", help="start the MCP server on stdio "
                                   "(for AI assistants / MCP clients)")
    m.set_defaults(fn=cmd_mcp)

    sv = sub.add_parser("serve", help="start the JSON REST API server")
    sv.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    sv.add_argument("--host", default="127.0.0.1",
                    help="bind address (default: 127.0.0.1)")
    sv.set_defaults(fn=cmd_serve)

    a = sub.add_parser("agent",
                       help="autonomous migrate loop: scan -> draft -> "
                            "verify -> keep-or-revert, on a temp copy "
                            "(never modifies your repo in place)")
    a.add_argument("--path", required=True, help="path to the target repo")
    a.add_argument("--max-iterations", type=int, default=5,
                   help="max fix attempts (default: 5)")
    a.add_argument("--today", default=None,
                   help="override date YYYY-MM-DD (for tests/demo)")
    a.add_argument("--planner-endpoint", default=None,
                   help="experimental: OpenAI-compatible chat-completions "
                        "base URL for ordering findings; falls back to the "
                        "built-in deterministic planner on any failure")
    a.add_argument("--format", choices=["console", "json"], default="console")
    a.set_defaults(fn=cmd_agent)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
