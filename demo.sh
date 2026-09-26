#!/usr/bin/env bash
# One-command demo of the integration-rot autopilot MVP.
set -e
cd "$(dirname "$0")"
if command -v integration-rot >/dev/null 2>&1; then
  integration-rot demo
else
  python3 -m integration_rot.cli demo
fi
