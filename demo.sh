#!/usr/bin/env bash
# One-command demo of the integration-rot autopilot MVP.
set -e
cd "$(dirname "$0")"
python3 -m integration_rot.cli demo
