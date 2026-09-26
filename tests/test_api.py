"""Tests for the REST API: real HTTP against a localhost server."""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from integration_rot.api_server import serve

SAMPLE_APP = Path(__file__).resolve().parent.parent / "demo" / "sample-app"


def _request(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


@pytest.fixture(scope="module")
def base_url():
    server = serve(port=0)  # ephemeral port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    thread.join(timeout=10)


def test_health(base_url):
    status, data = _request("GET", base_url + "/health")
    assert status == 200
    assert data["status"] == "ok"
    assert data["version"]


def test_deprecations(base_url):
    status, data = _request("GET", base_url + "/deprecations")
    assert status == 200
    assert data["count"] == 17
    assert any(d["id"] == "stripe-charges-api" for d in data["deprecations"])


def test_fixers(base_url):
    status, data = _request("GET", base_url + "/fixers")
    assert status == 200
    assert data["count"] == 8


def test_scan(base_url):
    status, data = _request("POST", base_url + "/scan",
                            {"path": str(SAMPLE_APP)})
    assert status == 200
    assert {d["name"] for d in data["dependencies"]} >= {"stripe"}


def test_scan_missing_path(base_url):
    status, data = _request("POST", base_url + "/scan", {})
    assert status == 400
    assert "error" in data


def test_check(base_url):
    status, data = _request("POST", base_url + "/check",
                            {"path": str(SAMPLE_APP),
                             "today": "2026-09-26"})
    assert status == 200
    assert "stripe-charges-api" in {f["entry_id"] for f in data["findings"]}
    assert data["would_exit"] == 0  # no critical/high in the honest DB


def test_fix_draft(base_url):
    status, data = _request("POST", base_url + "/fix",
                            {"entry_id": "stripe-charges-api",
                             "path": str(SAMPLE_APP), "file": "app.py"})
    assert status == 200
    assert data["empty"] is False
    assert data["applied"] is False  # dry-run only; nothing is written
    assert any("PaymentIntent" in c["diff"] for c in data["changes"])


def test_fix_alias_finding_id(base_url):
    status, _ = _request("POST", base_url + "/fix",
                         {"finding_id": "stripe-charges-api",
                          "path": str(SAMPLE_APP), "file": "app.py"})
    assert status == 200


def test_fix_no_fixer(base_url):
    status, data = _request("POST", base_url + "/fix",
                            {"entry_id": "twitter-free-api-retirement",
                             "path": str(SAMPLE_APP), "file": "app.py"})
    assert status == 422
    assert "error" in data


def test_unknown_route(base_url):
    status, data = _request("GET", base_url + "/nope")
    assert status == 404


def test_method_not_allowed(base_url):
    status, _ = _request("DELETE", base_url + "/health")
    assert status == 405
