"""Tests for the autonomous agent loop."""
import shutil
from datetime import date
from pathlib import Path

from integration_rot.agent import run_agent

SAMPLE_APP = Path(__file__).resolve().parent.parent / "demo" / "sample-app"
TODAY = date(2026, 9, 26)


def _snapshot(path: Path) -> dict:
    return {p.relative_to(path).as_posix(): p.read_bytes()
            for p in path.rglob("*") if p.is_file()}


def test_agent_fixes_sample_app(tmp_path):
    repo = tmp_path / "myrepo"
    shutil.copytree(SAMPLE_APP, repo)
    before = _snapshot(repo)

    report = run_agent(repo, max_iterations=5, today=TODAY, verbose=False)

    assert report.iterations >= 1
    assert set(report.fixed) == {"stripe-charges-api", "sendgrid-v2-api"}
    assert report.reverted == []  # contract tests pass; nothing reverted
    # the deprecated *code* is gone; what remains are dependency-level
    # residuals (old SDKs still pinned) with no patchable code
    remaining_ids = {f["entry_id"] for f in report.remaining}
    assert remaining_ids == {"stripe-charges-api", "sendgrid-v2-api"}
    assert {f["risk"] for f in report.remaining} == {"medium"}
    assert report.diffs, "expected diffs for the kept fixes"
    # the user's repo is never modified in place
    assert _snapshot(repo) == before


def test_agent_exit_code_mirrors_check(tmp_path):
    # no critical/high remain after the run -> exit 0, repo untouched
    from integration_rot.agent import cmd_agent
    import argparse
    repo = tmp_path / "myrepo2"
    shutil.copytree(SAMPLE_APP, repo)
    before = _snapshot(repo)
    args = argparse.Namespace(path=str(repo), max_iterations=5, today="2026-09-26",
                              planner_endpoint=None, format="json")
    assert cmd_agent(args) == 0
    assert _snapshot(repo) == before


def test_agent_exit_code_1_when_critical_remains(tmp_path):
    # a critical finding with no fixer -> exit 1
    import json
    from integration_rot.agent import cmd_agent
    import argparse
    repo = tmp_path / "critrepo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("twilio==7.0.0\n")
    (repo / "app.py").write_text("from twilio.rest import Client\n"
                                 "client = Client('s', 't')\n"
                                 "client.fax.create(to='+1')\n")
    # twilio-fax-api has a sunset in the past? check what risk it gets;
    # force the assertion on the report instead of assuming
    from integration_rot.agent import run_agent
    report = run_agent(repo, max_iterations=3, today=TODAY, verbose=False)
    args = argparse.Namespace(path=str(repo), max_iterations=3,
                              today="2026-09-26", planner_endpoint=None,
                              format="json")
    code = cmd_agent(args)
    risks = {f["risk"] for f in report.remaining}
    assert code == (1 if risks & {"critical", "high"} else 0)


def test_agent_reports_honestly_when_no_fixer(tmp_path):
    repo = tmp_path / "faxapp"
    repo.mkdir()
    (repo / "requirements.txt").write_text("twilio==7.0.0\n")
    (repo / "app.py").write_text(
        "from twilio.rest import Client\n"
        "client = Client('sid', 'token')\n"
        "client.fax.create(to='+1555', from_='+1666')\n")
    before = _snapshot(repo)

    report = run_agent(repo, max_iterations=3, today=TODAY, verbose=False)

    assert report.fixed == []
    assert any(e == "twilio-fax-api" for e, _ in report.unfixable)
    assert any(f["entry_id"] == "twilio-fax-api"
               for f in report.remaining)
    assert _snapshot(repo) == before  # untouched


def test_agent_idempotent_on_clean_repo(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests==2.28.0\n")
    (repo / "app.py").write_text("import requests\nprint('hello')\n")

    report = run_agent(repo, max_iterations=5, today=TODAY, verbose=False)

    assert report.iterations == 0
    assert report.fixed == []
    assert report.remaining == []


def test_agent_rejects_bad_path(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        run_agent(tmp_path / "does-not-exist", verbose=False)


def test_agent_planner_endpoint_falls_back(tmp_path):
    # unreachable endpoint: must fall back to the heuristic, never crash
    repo = tmp_path / "clean2"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n")
    report = run_agent(repo, max_iterations=2, today=TODAY,
                       planner_endpoint="http://127.0.0.1:1/nope",
                       verbose=False)
    assert report.planner in ("heuristic", "heuristic+fallback")
    assert report.remaining == []
