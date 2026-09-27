#!/usr/bin/env python3
"""Build + deploy the integration-rot docs/waitlist Cloudflare Worker.

Injects docs/index.html into worker/worker.template.js and uploads it with
the WAITLIST KV namespace bound. Auth via the vault-backed custom.cloudflare
credential (surrogate) — the real token never touches disk or logs.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_ID = "56faf9a57ad29af2d943fdedfb5ecda9"
SCRIPT_NAME = "integration-rot-docs"
KV_NAMESPACE_ID = "3073f43a260e4a81b6f5f3d7d467558e"
API = "https://api.cloudflare.com/client/v4"


def build() -> bytes:
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    template = (ROOT / "worker" / "worker.template.js").read_text(encoding="utf-8")
    worker = template.replace("__HTML__", json.dumps(html))
    assert "__HTML__" not in worker, "template placeholder not replaced"
    return worker.encode("utf-8")


def deploy(script: bytes) -> dict:
    metadata = {
        "body_part": "script",
        "bindings": [
            {"type": "kv_namespace", "name": "WAITLIST", "namespace_id": KV_NAMESPACE_ID}
        ],
    }
    boundary = "----irworker9f8e7d6c5b4a"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="script"; filename="worker.js"\r\n'
        "Content-Type: application/javascript\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + script + tail

    req = urllib.request.Request(
        f"{API}/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}",
        data=body,
        method="PUT",
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    add_surrogate_to_request(
        req, "custom.cloudflare", entry_name="access_token",
        allowed_hosts=["api.cloudflare.com"],
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return {"http_status": resp.status, "result": read_json_response(resp)}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:1500]}")


def main() -> None:
    script = build()
    print(f"built worker: {len(script)} bytes")
    out = deploy(script)
    print("deploy http_status:", out["http_status"])
    result = out["result"] or {}
    print("success:", result.get("success"))
    if not result.get("success"):
        print(json.dumps(result.get("errors"), indent=1)[:1500])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
