"""Tests for the PR proposer (GitHub REST API via urllib, mocked)."""
import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from integration_rot.fixer import FixDraft, FileChange
from integration_rot.models import DeprecationEntry
from integration_rot.proposer import build_pr_body, open_pr


def _entry():
    return DeprecationEntry(
        id="stripe-charges-api", vendor="Stripe",
        title="Legacy Charges API superseded by PaymentIntents",
        announced="2019-09-14", sunset=None, severity="warning",
        packages=[], code_patterns=[],
        migration="Migrate to PaymentIntent.",
        source_url="https://docs.stripe.com", fix_available=True)


def _draft():
    d = FixDraft(entry_id="stripe-charges-api")
    d.changes.append(FileChange(path="app.py", diff="--- a/app.py\n+++ b/app.py\n",
                                new_content="new"))
    d.tests.append(("tests/test_x.py", "def test_x():\n    assert True\n"))
    d.notes.append("verify manually")
    return d


def _fake_urlopen(payload: dict, status: int = 201):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_open_pr_posts_correct_request():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["payload"] = json.loads(req.data.decode())
        return _fake_urlopen({"html_url": "https://github.com/o/r/pull/1",
                              "number": 1})

    with patch("urllib.request.urlopen", fake_urlopen):
        pr = open_pr("octo", "repo", head="fix/x", base="main",
                     title="T", body="B", token="SECRET")

    assert captured["url"] == "https://api.github.com/repos/octo/repo/pulls"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer SECRET"
    assert captured["payload"]["head"] == "fix/x"
    assert captured["payload"]["base"] == "main"
    assert captured["payload"]["title"] == "T"
    assert pr["html_url"] == "https://github.com/o/r/pull/1"


def test_open_pr_draft_flag():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode())
        return _fake_urlopen({"html_url": "u", "number": 2})

    with patch("urllib.request.urlopen", fake_urlopen):
        open_pr("o", "r", head="h", base="b", title="t", body="b",
                token="x", draft_pr=True)
    assert captured["payload"]["draft"] is True


def test_open_pr_raises_on_api_error():
    err = urllib.error.HTTPError(
        url="https://api.github.com/repos/o/r/pulls", code=422,
        msg="Unprocessable Entity", hdrs=None,
        fp=io.BytesIO(json.dumps({"message": "Validation Failed"}).encode()))
    with patch("urllib.request.urlopen", MagicMock(side_effect=err)):
        with pytest.raises(RuntimeError, match="Validation Failed"):
            open_pr("o", "r", head="h", base="b", title="t", body="b", token="x")


def test_build_pr_body_contains_everything():
    body = build_pr_body(_entry(), _draft())
    assert "Stripe" in body and "Legacy Charges API" in body
    assert "```diff" in body and "--- a/app.py" in body
    assert "tests/test_x.py" in body and "```python" in body
    assert "verify manually" in body
    assert "integration-rot v" in body
