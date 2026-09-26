"""Tests for the MCP server: framing, initialize, tools/list, tools/call."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from integration_rot.mcp_server import (
    _handle,
    _read_message,
    _write_message,
    serve_stdio,
)

SAMPLE_APP = Path(__file__).resolve().parent.parent / "demo" / "sample-app"


def frame(msg: dict) -> bytes:
    body = json.dumps(msg).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def read_frame(proc) -> dict:
    headers = {}
    while True:
        line = proc.stdout.readline().decode("latin-1")
        assert line, "server closed stdout unexpectedly"
        line = line.strip()
        if line == "":
            break
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    want = int(headers["content-length"])
    body = b""
    while len(body) < want:
        chunk = proc.stdout.read(want - len(body))
        assert chunk, "server closed stdout mid-frame"
        body += chunk
    return json.loads(body.decode())


def call(proc, msg_id, method, params=None):
    proc.stdin.write(frame({"jsonrpc": "2.0", "id": msg_id,
                            "method": method, "params": params or {}}))
    proc.stdin.flush()
    return read_frame(proc)


@pytest.fixture(scope="module")
def mcp_proc():
    proc = subprocess.Popen(
        [sys.executable, "-m", "integration_rot.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    yield proc
    proc.stdin.close()
    proc.wait(timeout=30)


def test_initialize(mcp_proc):
    resp = call(mcp_proc, 1, "initialize", {"protocolVersion": "2025-06-18"})
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == "integration-rot"
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list(mcp_proc):
    resp = call(mcp_proc, 2, "tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"scan_repo", "check_findings", "draft_fix",
                     "lookup_deprecation", "get_fixers"}
    for t in resp["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"


def _tool_text(resp):
    assert "error" not in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_scan_repo_tool(mcp_proc):
    resp = call(mcp_proc, 3, "tools/call",
                {"name": "scan_repo", "arguments": {"path": str(SAMPLE_APP)}})
    data = _tool_text(resp)
    names = {d["name"] for d in data["dependencies"]}
    assert "stripe" in names


def test_check_findings_tool(mcp_proc):
    resp = call(mcp_proc, 4, "tools/call",
                {"name": "check_findings",
                 "arguments": {"path": str(SAMPLE_APP),
                               "today": "2026-09-26"}})
    data = _tool_text(resp)
    ids = {f["entry_id"] for f in data["findings"]}
    assert "stripe-charges-api" in ids
    # post-honesty-pass DB: both sample-app findings are medium (no invented
    # sunsets), so `check` would exit 0
    assert data["would_exit"] == 0
    assert {f["risk"] for f in data["findings"]} == {"medium"}


def test_draft_fix_tool(mcp_proc):
    resp = call(mcp_proc, 5, "tools/call",
                {"name": "draft_fix",
                 "arguments": {"entry_id": "stripe-charges-api",
                               "path": str(SAMPLE_APP), "file": "app.py"}})
    data = _tool_text(resp)
    assert data["empty"] is False
    assert any("PaymentIntent" in c["diff"] for c in data["changes"])


def test_draft_fix_unknown_entry(mcp_proc):
    resp = call(mcp_proc, 6, "tools/call",
                {"name": "draft_fix",
                 "arguments": {"entry_id": "twitter-free-api-retirement",
                               "path": str(SAMPLE_APP), "file": "app.py"}})
    assert resp["result"]["isError"] is True


def test_lookup_deprecation_tool(mcp_proc):
    resp = call(mcp_proc, 7, "tools/call",
                {"name": "lookup_deprecation",
                 "arguments": {"vendor": "stripe"}})
    data = _tool_text(resp)
    assert data["count"] >= 1
    assert all("stripe" in e["vendor"].lower() for e in data["entries"])


def test_get_fixers_tool(mcp_proc):
    resp = call(mcp_proc, 8, "tools/call", {"name": "get_fixers",
                                            "arguments": {}})
    data = _tool_text(resp)
    assert data["count"] == 8
    assert {f["entry_id"] for f in data["fixers"]} >= {"stripe-charges-api",
                                                      "sendgrid-v2-api"}


def test_unknown_tool_error(mcp_proc):
    resp = call(mcp_proc, 9, "tools/call",
                {"name": "nope", "arguments": {}})
    assert resp["error"]["code"] == -32601


def test_missing_required_arg(mcp_proc):
    resp = call(mcp_proc, 10, "tools/call",
                {"name": "scan_repo", "arguments": {}})
    assert resp["error"]["code"] == -32602


def test_notification_gets_no_response():
    # notifications (no id) must not produce a response
    assert _handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _handle({"jsonrpc": "2.0", "method": "unknown/method"}) is None


def test_framing_roundtrip_unit(capsys):
    # _write_message produces parseable Content-Length framing
    import io
    old = sys.stdout
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    try:
        _write_message({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        raw = sys.stdout.buffer.getvalue()
    finally:
        sys.stdout = old
    head, body = raw.split(b"\r\n\r\n")
    assert head.startswith(b"Content-Length:")
    assert json.loads(body) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
