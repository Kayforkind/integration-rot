"""Tests for the analyzer: matching semantics and risk ranking."""
from datetime import date

from integration_rot.analyzer import analyze, to_json, to_markdown
from integration_rot.models import Dependency, DeprecationEntry, ScanResult

TODAY = date(2026, 9, 26)


def _entry(**kw):
    base = dict(id="e1", vendor="Stripe", title="T", announced="2020-01-01",
                sunset=None, severity="warning", packages=[],
                code_patterns=[], migration="m", source_url="https://x.example")
    base.update(kw)
    return DeprecationEntry(**base)


def _scan(deps=(), source_files=(), repo="/tmp/r"):
    return ScanResult(repo=repo, dependencies=list(deps),
                      direct_calls=[], source_files=list(source_files))


def test_package_match(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    dep = Dependency(name="stripe", version="2.56.0", ecosystem="pypi",
                     manifest="requirements.txt", vendor="Stripe")
    entry = _entry(id="stripe-x", packages=[{"ecosystem": "pypi", "name": "stripe"}])
    report = analyze(_scan([dep], ["a.py"], str(tmp_path)), [entry], today=TODAY)
    assert len(report.findings) == 1
    assert report.findings[0].dependency is dep


def test_no_match_when_vendor_absent(tmp_path):
    dep = Dependency(name="requests", ecosystem="pypi", vendor=None)
    entry = _entry(id="stripe-x", packages=[{"ecosystem": "pypi", "name": "stripe"}])
    report = analyze(_scan([dep], [], str(tmp_path)), [entry], today=TODAY)
    assert report.findings == []


def test_code_pattern_match(tmp_path):
    (tmp_path / "a.py").write_text("stripe.Charge.create(amount=1)\n")
    dep = Dependency(name="stripe", version="8.0.0", ecosystem="npm",
                     manifest="package.json", vendor="Stripe")
    entry = _entry(id="stripe-charges", code_patterns=[r"stripe\.Charge\.create"])
    report = analyze(_scan([dep], ["a.py"], str(tmp_path)), [entry], today=TODAY)
    assert len(report.findings) == 1
    assert any("a.py:1" in ev for ev in report.findings[0].evidence)


def test_pattern_without_vendor_usage_is_skipped(tmp_path):
    (tmp_path / "a.py").write_text("stripe.Charge.create(amount=1)\n")
    entry = _entry(id="stripe-charges", code_patterns=[r"stripe\.Charge\.create"])
    report = analyze(_scan([], ["a.py"], str(tmp_path)), [entry], today=TODAY)
    assert report.findings == []


def test_risk_ranking():
    dep = Dependency(name="x", vendor="Twilio")
    scan = _scan([dep])
    past = _entry(id="past", vendor="Twilio", severity="warning", sunset="2021-12-31")
    soon = _entry(id="soon", vendor="Twilio", severity="warning", sunset="2026-10-26")
    later = _entry(id="later", vendor="Twilio", severity="warning", sunset="2027-06-01")
    nodate = _entry(id="nodate", vendor="Twilio", severity="warning", sunset=None)
    report = analyze(scan, [later, nodate, soon, past], today=TODAY)
    risks = {f.entry.id: f.risk for f in report.findings}
    assert risks == {"past": "critical", "soon": "high",
                     "later": "low", "nodate": "medium"}
    # ranked critical first
    assert [f.entry.id for f in report.findings][0] == "past"


def test_breaking_bumps_risk():
    dep = Dependency(name="x", vendor="Twilio")
    entry = _entry(id="b", vendor="Twilio", severity="breaking", sunset="2026-10-26")
    report = analyze(_scan([dep]), [entry], today=TODAY)
    assert report.findings[0].risk == "critical"  # high bumped by breaking


def test_renderers():
    dep = Dependency(name="x", vendor="Twilio")
    entry = _entry(id="b", vendor="Twilio", title="Big Problem", severity="breaking",
                   sunset="2021-01-01")
    report = analyze(_scan([dep]), [entry], today=TODAY)
    md = to_markdown(report)
    assert "CRITICAL" in md and "Big Problem" in md
    import json
    data = json.loads(to_json(report))
    assert data["counts"]["critical"] == 1
    assert data["findings"][0]["id"] == "b"


def test_pattern_match_without_manifest(tmp_path):
    # Entries naming packages must still flag code-pattern hits in repos with
    # no dependency manifests (no scanned deps at all) — previously the
    # analyzer skipped these entries before ever checking patterns.
    (tmp_path / "a.py").write_text("rtm = RTMClient(token=\"x\")\n")
    entry = _entry(id="slack-rtm",
                   packages=[{"ecosystem": "pypi", "name": "slack-sdk"}],
                   code_patterns=[r"RTMClient"])
    report = analyze(_scan([], ["a.py"], str(tmp_path)), [entry], today=TODAY)
    assert len(report.findings) == 1
    assert report.findings[0].dependency is None
    assert any("a.py:1" in ev for ev in report.findings[0].evidence)


def test_package_only_entry_without_dep_match_stays_silent(tmp_path):
    # Package-only entries (no code patterns) with no dep match must not
    # start flagging once the manifest-less fall-through exists.
    (tmp_path / "a.py").write_text("x = 1\n")
    entry = _entry(id="pkg-only",
                   packages=[{"ecosystem": "pypi", "name": "stripe"}])
    report = analyze(_scan([], ["a.py"], str(tmp_path)), [entry], today=TODAY)
    assert report.findings == []


def test_console_marks_auto_fix(tmp_path, capsys):
    from integration_rot.analyzer import print_console
    (tmp_path / "a.py").write_text("stripe.Charge.create(amount=1)\n")
    dep = Dependency(name="stripe", version="8.0.0", ecosystem="npm",
                     manifest="package.json", vendor="Stripe")
    entry = _entry(id="s", code_patterns=[r"stripe\.Charge\.create"],
                   fix_available=True)
    report = analyze(_scan([dep], ["a.py"], str(tmp_path)), [entry], today=TODAY)
    assert len(report.findings) == 1
    print_console(report)
    assert "[auto-fix available]" in capsys.readouterr().out
