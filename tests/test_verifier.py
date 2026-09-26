"""Tests for executed verification (verifier.py)."""
import pytest

from integration_rot.fixer import FileChange, FixDraft
from integration_rot.verifier import print_evidence, verify_fix

GH_APP = '''import requests


def get_user(username, token):
    resp = requests.get(f"https://api.github.com/users/{username}?access_token={token}")
    return resp.json()
'''


@pytest.fixture
def gh_repo(tmp_path):
    (tmp_path / "gh.py").write_text(GH_APP)
    return tmp_path


def test_verify_passes_on_github_fix(gh_repo):
    res = verify_fix(gh_repo, "github-api-query-auth", "gh.py")
    assert res.entry_id == "github-api-query-auth"
    assert res.changes_applied, "expected the fix to be applied in the sandbox"
    assert res.tests == ["tests/test_github_auth_header_contract.py"]
    assert res.tests_run == 1
    assert res.passed, f"contract test should pass:\n{res.output}"
    assert "2 passed" in res.output or "passed" in res.output


def test_verify_does_not_touch_target_repo(gh_repo):
    before = (gh_repo / "gh.py").read_text()
    verify_fix(gh_repo, "github-api-query-auth", "gh.py")
    assert (gh_repo / "gh.py").read_text() == before
    assert not (gh_repo / "tests").exists()


def test_verify_empty_draft(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n")
    res = verify_fix(tmp_path, "github-api-query-auth", "clean.py")
    assert res.changes_applied == []
    assert not res.passed
    assert "nothing to verify" in res.output


def test_verify_reports_failing_contract_test(tmp_path, monkeypatch):
    import integration_rot.verifier as verifier

    (tmp_path / "x.py").write_text("x = 1\n")

    def fake_draft(entry_id, repo_path, rel_path, **kwargs):
        d = FixDraft(entry_id=entry_id)
        d.changes.append(FileChange(path=rel_path, diff="", new_content="x = 2\n"))
        d.tests.append(("tests/test_always_fails.py",
                        "def test_always_fails():\n    assert False, 'boom'\n"))
        return d

    monkeypatch.setattr(verifier, "draft_fix", fake_draft)
    res = verify_fix(tmp_path, "some-entry", "x.py")
    assert res.tests_run == 1
    assert not res.passed
    assert "1 failed" in res.output


def test_verify_unknown_entry_raises(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n")
    with pytest.raises(KeyError):
        verify_fix(tmp_path, "no-such-entry", "x.py")


def test_print_evidence_renders(capsys):
    from integration_rot.verifier import VerifyResult
    res = VerifyResult(entry_id="e", file="f.py", changes_applied=["f.py"],
                       tests=["tests/t.py"], tests_run=1, passed=True,
                       output="1 passed", notes=["a note"])
    print_evidence(res)
    out = capsys.readouterr().out
    assert "PASS" in out and "a note" in out
