"""REST API for integration-rot (stdlib http.server, zero new dependencies).

Endpoints:
    GET  /health        -> {"status": "ok", "version": ...}
    GET  /deprecations  -> the deprecation DB entries
    GET  /fixers        -> the eight migration fixers and what each drafts
    POST /scan          -> {"path": ...} inventory dependencies + direct calls
    POST /check         -> {"path": ...} risk-ranked findings (exit semantics)
    POST /fix           -> {"entry_id"|"finding_id", "path", "file", ...}
                           drafts a fix (dry-run: writes nothing to disk)

All responses are JSON. Errors are JSON {"error": ...} with proper status
codes. The server binds localhost by default.

Run it with:  integration-rot serve --port 8000
"""
from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__
from .analyzer import analyze
from .deprecations import load_db
from .fixer import draft_fix
from .mcp_server import FIXER_CATALOG, _finding_to_dict
from .scanner import scan_repo


def _entry_to_dict(e) -> dict:
    return {
        "id": e.id, "vendor": e.vendor, "title": e.title,
        "announced": e.announced, "sunset": e.sunset,
        "severity": e.severity, "fix_available": e.fix_available,
        "packages": e.packages, "code_patterns": e.code_patterns,
        "migration": e.migration, "source_url": e.source_url,
    }


def api_scan(body: dict) -> tuple[int, dict]:
    path = body.get("path")
    if not path:
        return 400, {"error": "missing required field: path"}
    try:
        scan = scan_repo(path)
    except (OSError, ValueError) as e:
        return 400, {"error": f"cannot scan path: {e}"}
    return 200, {
        "repo": str(scan.repo),
        "dependencies": [
            {"name": d.name, "version": d.version, "spec": d.spec,
             "ecosystem": d.ecosystem, "manifest": d.manifest,
             "vendor": d.vendor} for d in scan.dependencies],
        "direct_calls": [
            {"vendor": c.vendor, "host": c.host, "file": c.file,
             "line": c.line, "snippet": c.snippet} for c in scan.direct_calls],
    }


def api_check(body: dict) -> tuple[int, dict]:
    path = body.get("path")
    if not path:
        return 400, {"error": "missing required field: path"}
    today = date.fromisoformat(body["today"]) if body.get("today") else None
    try:
        scan = scan_repo(path)
    except (OSError, ValueError) as e:
        return 400, {"error": f"cannot scan path: {e}"}
    entries = load_db(body["db"]) if body.get("db") else load_db()
    report = analyze(scan, entries, today=today)
    findings = [_finding_to_dict(f) for f in report.findings]
    return 200, {
        "repo": str(report.repo),
        "generated": report.generated,
        "counts": report.counts,
        "findings": findings,
        "would_exit": 1 if any(f["risk"] in ("critical", "high")
                               for f in findings) else 0,
    }


def api_fix(body: dict) -> tuple[int, dict]:
    # `finding_id` is accepted as an alias for `entry_id`.
    entry_id = body.get("entry_id") or body.get("finding_id")
    path = body.get("path")
    rel_file = body.get("file")
    missing = [k for k, v in (("entry_id", entry_id), ("path", path),
                              ("file", rel_file)) if not v]
    if missing:
        return 400, {"error": f"missing required fields: {', '.join(missing)}"}
    try:
        draft = draft_fix(entry_id, path, rel_file,
                          module=body.get("module", "app"),
                          func_name=body.get("func_name", "create_charge"))
    except KeyError as e:
        return 422, {"error": str(e)}
    return 200, {
        "entry_id": draft.entry_id,
        "empty": draft.empty(),
        "changes": [{"path": c.path, "diff": c.diff} for c in draft.changes],
        "tests": [{"path": t, "content": c} for t, c in draft.tests],
        "notes": list(draft.notes),
        "applied": False,  # this endpoint only drafts; nothing is written
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"integration-rot/{__version__}"

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> tuple[dict | None, str | None]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "invalid Content-Length"
        if length <= 0:
            return {}, None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, f"invalid JSON body: {e}"

    def log_message(self, fmt, *args):  # keep stdout clean for CLI use
        pass

    def do_GET(self):  # noqa: N802 - http.server naming
        route = urlparse(self.path).path.rstrip("/") or "/"
        if route == "/health":
            self._send(200, {"status": "ok", "version": __version__})
        elif route == "/deprecations":
            self._send(200, {"count": len(_DB),
                             "deprecations": [_entry_to_dict(e) for e in _DB]})
        elif route == "/fixers":
            self._send(200, {"count": len(FIXER_CATALOG),
                             "fixers": FIXER_CATALOG})
        else:
            self._send(404, {"error": f"unknown route: {route}"})

    def do_POST(self):  # noqa: N802 - http.server naming
        route = urlparse(self.path).path.rstrip("/") or "/"
        body, err = self._read_json()
        if err:
            self._send(400, {"error": err})
            return
        if route == "/scan":
            status, payload = api_scan(body)
        elif route == "/check":
            status, payload = api_check(body)
        elif route == "/fix":
            status, payload = api_fix(body)
        else:
            status, payload = 404, {"error": f"unknown route: {route}"}
        self._send(status, payload)

    def do_PUT(self):  # noqa: N802
        self._send(405, {"error": "method not allowed"})

    def do_DELETE(self):  # noqa: N802
        self._send(405, {"error": "method not allowed"})


_DB = load_db()


def serve(port: int = 8000, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Build the server (does not block). Use serve_forever() to run it."""
    server = ThreadingHTTPServer((host, port), Handler)
    return server


def serve_forever(port: int = 8000, host: str = "127.0.0.1") -> int:
    server = serve(port, host)
    addr = server.server_address
    print(f"integration-rot API listening on http://{addr[0]}:{addr[1]}")
    print("endpoints: GET /health /deprecations /fixers | "
          "POST /scan /check /fix")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(serve_forever())
