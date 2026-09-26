"""Tests for scanner: manifest parsing, version resolution, vendor mapping."""
import json

import pytest

from integration_rot.scanner import scan_repo, version_satisfies
from integration_rot.vendor_map import vendor_for_host, vendor_for_package


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"stripe": "^8.0.0", "@sendgrid/mail": "~6.5.0"},
        "devDependencies": {"jest": "^29.0.0"},
    }))
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "x"},
            "node_modules/stripe": {"version": "8.215.0"},
            "node_modules/@sendgrid/mail": {"version": "6.5.4"},
            "node_modules/jest": {"version": "29.7.0"},
        },
    }))
    (tmp_path / "requirements.txt").write_text(
        "twilio==7.16.0\nrequests>=2.28,<3\n# a comment\n")
    (tmp_path / "app.py").write_text(
        'import requests\nrequests.post("https://api.sendgrid.com/api/mail.send.json")\n')
    return tmp_path


def test_scan_finds_and_resolves(repo):
    scan = scan_repo(repo)
    by_name = {d.name: d for d in scan.dependencies}
    assert by_name["stripe"].ecosystem == "npm"
    assert by_name["stripe"].version == "8.215.0"  # resolved from lockfile
    assert by_name["stripe"].vendor == "Stripe"
    assert by_name["@sendgrid/mail"].vendor == "SendGrid"
    assert by_name["twilio"].ecosystem == "pypi"
    assert by_name["twilio"].version == "7.16.0"
    assert by_name["twilio"].vendor == "Twilio"
    # unmapped package has no vendor but is still inventoried
    assert by_name["jest"].vendor is None
    assert by_name["requests"].vendor is None


def test_scan_direct_calls(repo):
    scan = scan_repo(repo)
    assert len(scan.direct_calls) == 1
    call = scan.direct_calls[0]
    assert call.vendor == "SendGrid"
    assert call.host == "api.sendgrid.com"
    assert call.file == "app.py"
    assert call.line == 2


def test_scan_missing_dir():
    with pytest.raises(ValueError):
        scan_repo("/nonexistent/path/xyz")


@pytest.mark.parametrize("version,spec,expected", [
    ("8.215.0", "<9.0.0", True),
    ("9.0.0", "<9.0.0", False),
    ("8.0.0", "^8.0.0", True),
    ("9.0.0", "^8.0.0", False),
    ("8.1.5", "~8.1.0", True),
    ("8.2.0", "~8.1.0", False),
    ("2.56.0", ">=2.0,<3", True),
    ("3.0.0", ">=2.0,<3", False),
    ("1.2.3", "==1.2.3", True),
    ("1.2.4", "==1.2.3", False),
    (None, "<9", True),          # unknown version -> permissive
    ("8.0.0", None, True),       # unknown spec -> permissive
])
def test_version_satisfies(version, spec, expected):
    assert version_satisfies(version, spec) is expected


def test_vendor_maps():
    assert vendor_for_package("Stripe") == "Stripe"
    assert vendor_for_package("@slack/web-api") == "Slack"
    assert vendor_for_package("definitely-not-a-vendor") is None
    assert vendor_for_host("api.stripe.com") == "Stripe"
    assert vendor_for_host("example.com") is None
