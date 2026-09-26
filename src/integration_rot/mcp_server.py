"""Model Context Protocol server for integration-rot (stdio, stdlib only).

Exposes the scanner, analyzer, fixer, and deprecation DB as MCP tools so an
AI assistant (or any MCP client) can drive integration-rot over JSON-RPC
without shelling out to the CLI. No third-party `mcp` package is used:
framing (Content-Length headers) and dispatch are hand-rolled with the
standard library, keeping the project's zero-runtime-dependency promise.

Run it with:  integration-rot mcp
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .analyzer import analyze
from .deprecations import load_db
from .fixer import detect_func_name, draft_fix
from .scanner import scan_repo

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "integration-rot"

# The eight fixers that ship, and what each one drafts. Kept as data so the
# MCP `get_fixers` tool, the REST API, and docs all describe the same set.
FIXER_CATALOG = [
    {"entry_id": "stripe-charges-api",
     "drafts": "stripe.Charge.create(...) -> stripe.PaymentIntent.create(...) "
               "(SCA-ready PaymentIntents migration)"},
    {"entry_id": "twilio-authy-api",
     "drafts": "Twilio Authy API calls -> Twilio Verify v2 API calls"},
    {"entry_id": "sendgrid-v2-api",
     "drafts": "SendGrid v2 mail.send.json payload -> v3 /mail/send JSON API"},
    {"entry_id": "plaid-legacy-transactions",
     "drafts": "Plaid legacy /transactions/get -> /transactions/sync cursor flow"},
    {"entry_id": "slack-rtm-api",
     "drafts": "Slack RTM API (rtm.start / websocket) -> Socket Mode + Web API"},
    {"entry_id": "github-api-query-auth",
     "drafts": "GitHub ?access_token= query-param auth -> Authorization header"},
    {"entry_id": "salesforce-api-v21-v30",
     "drafts": "Salesforce API v21-v30 URLs -> v59.0"},
    {"entry_id": "mailchimp-api-2-retirement",
     "drafts": "Mailchimp API v2 call shapes -> v3 migration draft "
               "(endpoint/payload mapping needs manual review)"},
]


def _finding_to_dict(f) -> dict:
    return {
        "entry_id": f.entry.id,
        "vendor": f.entry.vendor,
        "title": f.entry.title,
        "risk": f.risk,
        "severity": f.entry.severity,
        "sunset": f.entry.sunset,
        "days_to_sunset": f.days_to_sunset,
        "fix_available": f.entry.fix_available,
        "evidence": list(f.evidence),
        "migration": f.entry.migration,
        "source_url": f.entry.source_url,
    }


def tool_scan_repo(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise ValueError("missing required argument: path")
    scan = scan_repo(path)
    return {
        "repo": str(scan.repo),
        "dependencies": [
            {"name": d.name, "version": d.version, "spec": d.spec,
             "ecosystem": d.ecosystem, "manifest": d.manifest,
             "vendor": d.vendor}
            for d in sorted(scan.dependencies, key=lambda d: (d.ecosystem, d.name))
        ],
        "direct_calls": [
            {"vendor": c.vendor, "host": c.host, "file": c.file,
             "line": c.line, "snippet": c.snippet}
            for c in scan.direct_calls
        ],
        "source_files": list(scan.source_files),
    }


def tool_check_findings(args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise ValueError("missing required argument: path")
    today = date.fromisoformat(args["today"]) if args.get("today") else None
    scan = scan_repo(path)
    entries = load_db(args["db"]) if args.get("db") else load_db()
    report = analyze(scan, entries, today=today)
    findings = [_finding_to_dict(f) for f in report.findings]
    return {
        "repo": str(report.repo),
        "generated": report.generated,
        "counts": report.counts,
        "findings": findings,
        # mirrors `integration-rot check` exit semantics for CI use
        "would_exit": 1 if any(f["risk"] in ("critical", "high")
                               for f in findings) else 0,
    }


def tool_draft_fix(args: dict) -> dict:
    entry_id = args.get("entry_id")
    path = args.get("path")
    rel_file = args.get("file")
    missing = [k for k, v in (("entry_id", entry_id), ("path", path),
                              ("file", rel_file)) if not v]
    if missing:
        raise ValueError(f"missing required arguments: {', '.join(missing)}")
    try:
        # func_name: explicit value wins; otherwise detect it from the finding
        # (first code-pattern match -> enclosing def). When detection fails,
        # omit it and let each fixer's own default apply.
        kwargs: dict = {"module": args.get("module", "app")}
        func_name = args.get("func_name") or detect_func_name(
            path, rel_file, entry_id)
        if func_name:
            kwargs["func_name"] = func_name
        draft = draft_fix(entry_id, path, rel_file, **kwargs)
    except KeyError as e:
        return {"isError": True, "error": str(e)}
    return {
        "entry_id": draft.entry_id,
        "empty": draft.empty(),
        "changes": [{"path": c.path, "diff": c.diff} for c in draft.changes],
        "tests": [{"path": t, "content": c} for t, c in draft.tests],
        "notes": list(draft.notes),
    }


def tool_lookup_deprecation(args: dict) -> dict:
    vendor = (args.get("vendor") or "").lower()
    package = (args.get("package") or "").lower()
    if not vendor and not package:
        raise ValueError("provide at least one of: vendor, package")
    out = []
    for e in load_db():
        hit = False
        if vendor and vendor in e.vendor.lower():
            hit = True
        if package and any(package in p.get("name", "").lower()
                           for p in e.packages):
            hit = True
        if hit:
            out.append({
                "id": e.id, "vendor": e.vendor, "title": e.title,
                "announced": e.announced, "sunset": e.sunset,
                "severity": e.severity, "fix_available": e.fix_available,
                "migration": e.migration, "source_url": e.source_url,
            })
    return {"count": len(out), "entries": out}


def tool_get_fixers(args: dict) -> dict:
    return {"count": len(FIXER_CATALOG), "fixers": FIXER_CATALOG}


TOOLS = {
    "scan_repo": (tool_scan_repo,
                  "Inventory a repo's third-party API dependencies and direct "
                  "vendor API calls.",
                  {"type": "object",
                   "properties": {"path": {"type": "string",
                                           "description": "path to the target repo"}},
                   "required": ["path"]}),
    "check_findings": (tool_check_findings,
                       "Analyze a repo against the deprecation DB; returns "
                       "risk-ranked findings plus the exit code `check` would use.",
                       {"type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "db": {"type": "string",
                                   "description": "optional custom deprecations.json"},
                            "today": {"type": "string",
                                      "description": "override date YYYY-MM-DD"}},
                        "required": ["path"]}),
    "draft_fix": (tool_draft_fix,
                  "Draft a migration patch (unified diff + contract test) for "
                  "a deprecation entry in one repo file. Dry-run: writes nothing.",
                  {"type": "object",
                   "properties": {
                       "entry_id": {"type": "string"},
                       "path": {"type": "string",
                                "description": "path to the target repo"},
                       "file": {"type": "string",
                                "description": "repo-relative source file to patch"},
                       "module": {"type": "string", "default": "app"},
                       "func_name": {"type": "string",
                                     "description": "function under test; "
                                                    "auto-detected from the "
                                                    "finding when omitted"}},
                   "required": ["entry_id", "path", "file"]}),
    "lookup_deprecation": (tool_lookup_deprecation,
                           "Look up deprecation DB entries by vendor and/or "
                           "package name.",
                           {"type": "object",
                            "properties": {
                                "vendor": {"type": "string"},
                                "package": {"type": "string"}},
                            "required": []}),
    "get_fixers": (tool_get_fixers,
                   "List the migration fixers that ship with integration-rot "
                   "and what each one drafts.",
                   {"type": "object", "properties": {}, "required": []}),
}


# ---------------------------------------------------------------------------
# JSON-RPC framing (Content-Length, LSP/MCP style) over stdio
# ---------------------------------------------------------------------------

def _read_message() -> dict | None:
    """Read one framed message from stdin. Returns None on EOF."""
    content_length = None
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return None  # EOF
        line = raw.decode("latin-1").strip()
        if line == "":
            break  # blank line ends the header block
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    if content_length is None:
        return None
    body = sys.stdin.buffer.read(content_length)
    if len(body) < content_length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode("latin-1"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _handle(msg: dict) -> dict | None:
    """Dispatch one JSON-RPC message. Returns the response, or None for
    notifications (no `id`) which must not be answered."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    def result(payload):
        return {"jsonrpc": "2.0", "id": msg_id, "result": payload}

    def error(code, message):
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return result({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })
    if method == "tools/list":
        return result({"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (_, desc, schema) in TOOLS.items()
        ]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOLS:
            if msg_id is None:
                return None
            return error(-32601, f"unknown tool: {name}")
        fn, _, _ = TOOLS[name]
        try:
            payload = fn(arguments)
        except (ValueError, KeyError) as e:
            if msg_id is None:
                return None
            return error(-32602, str(e))
        except Exception as e:  # noqa: BLE001 - surface as JSON-RPC error
            if msg_id is None:
                return None
            return error(-32603, f"tool {name} failed: {e}")
        if isinstance(payload, dict) and payload.pop("isError", False):
            content = [{"type": "text",
                        "text": f"error: {payload.get('error', 'tool failed')}"}]
            resp = result({"content": content, "isError": True})
        else:
            resp = result({"content": [{"type": "text",
                                        "text": json.dumps(payload, indent=2)}]})
        return resp
    # Unknown methods: notifications (no id) are silently ignored; requests
    # get a method-not-found error.
    if msg_id is None:
        return None
    return error(-32601, f"unknown method: {method}")


def serve_stdio() -> int:
    """Run the MCP server loop on stdin/stdout until EOF."""
    # reconfigure stdout for binary writes; keep stderr for diagnostics
    while True:
        try:
            msg = _read_message()
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            print(f"mcp: read error: {e}", file=sys.stderr)
            continue
        if msg is None:
            break  # EOF or unrecoverable framing error
        try:
            resp = _handle(msg)
        except Exception as e:  # noqa: BLE001 - never kill the loop on dispatch
            print(f"mcp: dispatch error: {e}", file=sys.stderr)
            continue
        if resp is not None:
            _write_message(resp)
    return 0


if __name__ == "__main__":
    sys.exit(serve_stdio())
