"""Tests for the fixer: Stripe Charges -> PaymentIntents drafting."""
import pytest

from integration_rot.fixer import (
    draft_fix,
    draft_stripe_charges_fix,
    generate_stripe_contract_test,
    write_fix,
)

BEFORE = '''import stripe

stripe.api_key = "sk_test_123"


def create_charge(amount_cents, currency, token, description=""):
    charge = stripe.Charge.create(
        amount=amount_cents,
        currency=currency,
        source=token,
        description=description,
    )
    return charge
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app.py").write_text(BEFORE)
    return tmp_path


def test_draft_rewrites_charge_create(repo):
    draft = draft_stripe_charges_fix(repo, "app.py")
    assert not draft.empty()
    assert len(draft.changes) == 1
    change = draft.changes[0]
    assert "stripe.PaymentIntent.create" in change.new_content
    assert "stripe.Charge.create" not in change.new_content
    assert "payment_method=token" in change.new_content
    assert "confirm=True" in change.new_content
    assert "automatic_payment_methods" in change.new_content
    assert "-    charge = stripe.Charge.create(" in change.diff
    assert "+    charge = stripe.PaymentIntent.create(" in change.diff
    # raw token must NOT be presented as a valid payment_method: the draft
    # flags conversion to a PaymentMethod ID (pm_...) as a manual step
    assert "TODO(manual, required)" in change.new_content
    assert "pm_..." in change.new_content
    assert "tok_... token" in change.new_content
    assert any("manual step" in n for n in draft.notes)
    assert any("payment-methods/transitioning" in n for n in draft.notes)


def test_draft_no_match(repo):
    (repo / "clean.py").write_text("x = 1\n")
    draft = draft_stripe_charges_fix(repo, "clean.py")
    assert draft.empty()


def test_contract_test_generation():
    path, content = generate_stripe_contract_test("app", "create_charge")
    assert path == "tests/test_stripe_payment_intent_contract.py"
    assert "PaymentIntent.create" in content
    assert '"source" not in kwargs' in content
    assert 'kwargs["payment_method"]' in content
    # the contract test must require a PaymentMethod ID, not a raw token
    assert 'startswith("pm_")' in content
    assert "not a legacy token" in content


def test_draft_fix_registry(repo):
    draft = draft_fix("stripe-charges-api", repo, "app.py",
                      module="app", func_name="create_charge")
    assert not draft.empty()
    assert len(draft.tests) == 1  # contract test sketch included


def test_draft_fix_unknown_entry(repo):
    with pytest.raises(KeyError):
        draft_fix("nope-not-real", repo, "app.py")


def test_write_fix_applies(repo):
    draft = draft_fix("stripe-charges-api", repo, "app.py",
                      module="app", func_name="create_charge")
    written = write_fix(repo, draft)
    assert "app.py" in written
    new = (repo / "app.py").read_text()
    assert "PaymentIntent.create" in new
    test_file = repo / "tests" / "test_stripe_payment_intent_contract.py"
    assert test_file.exists()


# ---------------------------------------------------------------------------
# v0.2.0: Twilio Authy -> Verify, SendGrid v2 -> v3, Plaid sync, Slack Socket Mode
# ---------------------------------------------------------------------------

from integration_rot.fixer import (
    draft_plaid_transactions_fix,
    draft_sendgrid_v2_fix,
    draft_slack_rtm_fix,
    draft_twilio_authy_fix,
    generate_plaid_contract_test,
    generate_sendgrid_contract_test,
    generate_slack_contract_test,
    generate_twilio_contract_test,
)

AUTHY_BEFORE = '''from authy.api import AuthyApiClient

authy_api = AuthyApiClient("AUTHY_KEY")


def start_verification(phone_number, country_code):
    resp = authy_api.phones.verification_start(phone_number, country_code, via="sms")
    return resp.ok()


def check_verification(phone_number, country_code, code):
    resp = authy_api.phones.verification_check(phone_number, country_code, code)
    return resp.ok()
'''

SG_BEFORE = '''import requests


def send_email(to, subject, body):
    resp = requests.post(
        "https://api.sendgrid.com/api/mail.send.json",
        data={"to": to, "from": "noreply@example.com", "subject": subject, "text": body},
        headers={"Authorization": "Bearer SG.key"},
        timeout=10,
    )
    return resp.status_code
'''

PLAID_BEFORE = '''from plaid import Client

client = Client()


def get_all(access_token, start_date, end_date):
    response = client.Transactions.get(access_token, start_date, end_date)
    return response["transactions"]
'''

SLACK_BEFORE = '''from slack import RTMClient

rtm_client = RTMClient(token="xoxb-123")


@RTMClient.run_on(event="message")
def handle(**payload):
    print(payload["data"]["text"])


rtm_client.start()
'''


def test_twilio_authy_rewrite(tmp_path):
    (tmp_path / "a.py").write_text(AUTHY_BEFORE)
    draft = draft_twilio_authy_fix(tmp_path, "a.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "from twilio.rest import Client" in new
    assert "from authy.api import AuthyApiClient" not in new
    assert "verifications.create" in new
    assert "verification_checks.create" in new
    assert "VERIFY_SERVICE_SID" in new
    assert 'channel="sms"' in new
    assert any("cursor" not in n for n in draft.notes)
    assert any("status" in n for n in draft.notes)  # .ok() semantics note


def test_twilio_authy_no_match(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n")
    assert draft_twilio_authy_fix(tmp_path, "clean.py").empty()


def test_twilio_contract_test():
    path, content = generate_twilio_contract_test("myapp")
    assert path == "tests/test_twilio_verify_contract.py"
    assert "verifications.create" in content
    assert "verification_checks.create" in content
    assert "from myapp import start_verification, check_verification" in content


def test_sendgrid_v2_rewrite(tmp_path):
    (tmp_path / "s.py").write_text(SG_BEFORE)
    draft = draft_sendgrid_v2_fix(tmp_path, "s.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "api.sendgrid.com/v3/mail/send" in new
    assert "api/mail.send.json" not in new
    assert "json={" in new and "data={" not in new
    assert '"personalizations"' in new
    assert '"content"' in new


def test_sendgrid_contract_test():
    path, content = generate_sendgrid_contract_test("myapp", "send_email")
    assert path == "tests/test_sendgrid_v3_contract.py"
    assert "/v3/mail/send" in content
    assert '"personalizations" in payload' in content


def test_plaid_rewrite(tmp_path):
    (tmp_path / "p.py").write_text(PLAID_BEFORE)
    draft = draft_plaid_transactions_fix(tmp_path, "p.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "transactions_sync(" in new
    assert "Transactions.get(" not in new
    assert any("cursor" in n for n in draft.notes)
    assert any("has_more" in n for n in draft.notes)


def test_plaid_contract_test():
    path, content = generate_plaid_contract_test("myapp", "sync_transactions")
    assert path == "tests/test_plaid_sync_contract.py"
    assert "transactions_sync" in content
    assert "has_more" in content


def test_slack_rtm_rewrite(tmp_path):
    (tmp_path / "s.py").write_text(SLACK_BEFORE)
    draft = draft_slack_rtm_fix(tmp_path, "s.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "from slack_sdk.socket_mode import SocketModeClient" in new
    assert "from slack import RTMClient" not in new
    assert "SocketModeClient(" in new
    assert "app_token=" in new
    assert "@RTMClient.run_on" not in new
    assert "socket_mode_request_listeners" in new
    assert "socket_client.connect()" in new


def test_slack_contract_test():
    path, content = generate_slack_contract_test("myapp")
    assert path == "tests/test_slack_socket_mode_contract.py"
    assert "SocketModeClient" in content
    assert "xapp-" in content


def test_registry_dispatches_all_fixers(tmp_path):
    (tmp_path / "a.py").write_text(AUTHY_BEFORE)
    (tmp_path / "s.py").write_text(SG_BEFORE)
    (tmp_path / "p.py").write_text(PLAID_BEFORE)
    (tmp_path / "r.py").write_text(SLACK_BEFORE)
    for entry, fname in [("twilio-authy-api", "a.py"),
                         ("sendgrid-v2-api", "s.py"),
                         ("plaid-legacy-transactions", "p.py"),
                         ("slack-rtm-api", "r.py")]:
        draft = draft_fix(entry, tmp_path, fname, module="m")
        assert not draft.empty(), entry
        assert len(draft.tests) == 1, entry


# ---------------------------------------------------------------------------
# GitHub: ?access_token= -> Authorization header
# ---------------------------------------------------------------------------

GH_BEFORE = '''import requests


def get_user(username, token):
    resp = requests.get(f"https://api.github.com/users/{username}?access_token={token}")
    return resp.json()


def get_repo(owner, repo, token):
    return requests.get(
        "https://api.github.com/repos/" + owner + "/" + repo + "?access_token=" + token
    ).json()
'''


def test_github_query_auth_rewrite(tmp_path):
    from integration_rot.fixer import draft_github_query_auth_fix
    (tmp_path / "gh.py").write_text(GH_BEFORE)
    draft = draft_github_query_auth_fix(tmp_path, "gh.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "access_token=" not in new
    assert 'headers={"Authorization": f"Bearer {token}"}' in new
    # concatenation case: `+ "?access_token=" + token` collapses cleanly
    assert '+ "?access_token="' not in new and "+ token" not in new.split("headers")[0]
    compile(new, "gh.py", "exec")


def test_github_query_auth_no_match(tmp_path):
    from integration_rot.fixer import draft_github_query_auth_fix
    (tmp_path / "clean.py").write_text('import requests\nrequests.get("https://api.github.com/user")\n')
    draft = draft_github_query_auth_fix(tmp_path, "clean.py")
    assert draft.empty()


def test_github_contract_test_source_level(tmp_path):
    from integration_rot.fixer import generate_github_contract_test
    path, content = generate_github_contract_test("gh.py")
    assert path == "tests/test_github_auth_header_contract.py"
    assert "access_token=" in content and "Authorization" in content
    compile(content, path, "exec")


# ---------------------------------------------------------------------------
# Salesforce: retired versions -> v59.0
# ---------------------------------------------------------------------------

SF_BEFORE = '''import requests

URL = "https://na1.salesforce.com/services/data/v27.0/sobjects/Account"


def get_accounts(session_id):
    return requests.get(URL, headers={"Authorization": f"Bearer {session_id}"})
'''


def test_salesforce_version_bump(tmp_path):
    from integration_rot.fixer import draft_salesforce_version_fix
    (tmp_path / "sf.py").write_text(SF_BEFORE)
    draft = draft_salesforce_version_fix(tmp_path, "sf.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "/services/data/v59.0/" in new
    assert "v27.0" not in new
    compile(new, "sf.py", "exec")


def test_salesforce_version_kwarg(tmp_path):
    from integration_rot.fixer import draft_salesforce_version_fix
    (tmp_path / "sf.py").write_text("from simple_salesforce import Salesforce\nsf = Salesforce(version='28.0')\n")
    draft = draft_salesforce_version_fix(tmp_path, "sf.py")
    assert not draft.empty()
    assert "version='59.0'" in draft.changes[0].new_content


def test_salesforce_no_match(tmp_path):
    from integration_rot.fixer import draft_salesforce_version_fix
    (tmp_path / "sf.py").write_text('URL = "https://na1.salesforce.com/services/data/v59.0/sobjects/Account"\n')
    draft = draft_salesforce_version_fix(tmp_path, "sf.py")
    assert draft.empty()


def test_salesforce_contract_test(tmp_path):
    from integration_rot.fixer import generate_salesforce_contract_test
    path, content = generate_salesforce_contract_test("sf.py")
    assert path == "tests/test_salesforce_version_contract.py"
    compile(content, path, "exec")


# ---------------------------------------------------------------------------
# Mailchimp: API 2.0 -> 3.0
# ---------------------------------------------------------------------------

MC_BEFORE = '''import requests

API_KEY = "key-us1"


def subscribe(email):
    return requests.post(
        "https://us1.api.mailchimp.com/2.0/lists/subscribe.json",
        data={"apikey": API_KEY, "id": "list1", "email": {"email": email}},
    ).json()
'''


def test_mailchimp_v2_rewrite(tmp_path):
    from integration_rot.fixer import draft_mailchimp_v2_fix
    (tmp_path / "mc.py").write_text(MC_BEFORE)
    draft = draft_mailchimp_v2_fix(tmp_path, "mc.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "api.mailchimp.com/3.0/" in new
    assert "api.mailchimp.com/2.0/" not in new
    assert '"apikey"' not in new
    assert "auth=('', API_KEY)" in new
    compile(new, "mc.py", "exec")
    assert any("operation-specific" in n for n in draft.notes)


def test_mailchimp_no_match(tmp_path):
    from integration_rot.fixer import draft_mailchimp_v2_fix
    (tmp_path / "mc.py").write_text('x = "https://us1.api.mailchimp.com/3.0/lists"\n')
    draft = draft_mailchimp_v2_fix(tmp_path, "mc.py")
    assert draft.empty()


def test_mailchimp_contract_test(tmp_path):
    from integration_rot.fixer import generate_mailchimp_contract_test
    path, content = generate_mailchimp_contract_test("mc.py")
    assert path == "tests/test_mailchimp_v3_contract.py"
    compile(content, path, "exec")


def test_registry_dispatches_new_fixers(tmp_path):
    (tmp_path / "gh.py").write_text(GH_BEFORE)
    (tmp_path / "sf.py").write_text(SF_BEFORE)
    (tmp_path / "mc.py").write_text(MC_BEFORE)
    for entry, fname in [("github-api-query-auth", "gh.py"),
                         ("salesforce-api-v21-v30", "sf.py"),
                         ("mailchimp-api-2-retirement", "mc.py")]:
        draft = draft_fix(entry, tmp_path, fname, module="m")
        assert not draft.empty(), entry
        assert len(draft.tests) == 1, entry


def test_contract_test_is_hermetic_without_third_party_deps(tmp_path):
    """Generated contract tests must pass even when the app's third-party
    deps are not installed: missing imports are stubbed (regression test
    for the agent sandbox collection error)."""
    import subprocess
    import sys as _sys
    from pathlib import Path
    from integration_rot.fixer import (
        draft_fix,
        generate_stripe_contract_test,
        _third_party_imports,
        _hermetic_import_preamble,
    )

    sample_app = Path(__file__).resolve().parent.parent / "demo" / "sample-app" / "app.py"
    assert _third_party_imports(sample_app, "app") == ["requests", "stripe"]
    assert _hermetic_import_preamble([]) == ""
    preamble = _hermetic_import_preamble(["requests", "stripe"])
    assert 'for _pkg in ("requests", "stripe",):' in preamble
    # default keeps old call sites working: no preamble
    _, content = generate_stripe_contract_test("app", "create_charge")
    assert "Hermetic import stubs" not in content
    compile(content, "t.py", "exec")

    # end to end: draft against the sample app, apply the fix, run the
    # generated test with a bare interpreter (no requests/stripe installed)
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "app.py").write_text(
        "import requests\nimport stripe\n\n"
        "def create_charge(a, c, t):\n"
        "    return stripe.Charge.create(amount=a, currency=c, source=t)\n"
    )
    from integration_rot.fixer import write_fix
    draft = draft_fix("stripe-charges-api", repo, "app.py",
                      module="app", func_name="create_charge")
    assert not draft.empty()
    tpath, tcontent = draft.tests[0]
    assert "Hermetic import stubs" in tcontent
    written = write_fix(repo, draft)
    assert "app.py" in written and tpath in written
    proc = subprocess.run(
        [_sys.executable, "-m", "pytest", tpath, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=repo, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-500:]


def test_twilio_sdk_style_rewrite(tmp_path):
    """Twilio SDK style (client.authy.services(...)) migrates to Verify v2,
    keeping the inline service SID (no spurious VERIFY_SERVICE_SID)."""
    (tmp_path / "a.py").write_text(
        'from twilio.rest import Client\nclient = Client("ACx", "tok")\n'
        'def start_verification(to, channel):\n'
        '    return client.authy.services("VAX").verifications.create(to=to, channel=channel)\n'
        'def check_verification(to, code):\n'
        '    return client.authy.services("VAX").verification_checks.create(to=to, code=code)\n')
    from integration_rot.fixer import draft_twilio_authy_fix
    draft = draft_twilio_authy_fix(tmp_path, "a.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert 'client.verify.v2.services("VAX").verifications.create(to=to, channel=channel)' in new
    assert 'client.verify.v2.services("VAX").verification_checks.create(to=to, code=code)' in new
    assert ".authy.services(" not in new
    assert "VERIFY_SERVICE_SID" not in new


def test_plaid_snake_case_rewrite(tmp_path):
    """plaid-python's transactions_get(...) migrates, including the response
    shape: resp["transactions"] -> resp["added"]."""
    (tmp_path / "a.py").write_text(
        'plaid_client = None\ndef sync_transactions(access_token):\n'
        '    resp = plaid_client.transactions_get({"access_token": access_token})\n'
        '    return resp["transactions"]\n')
    from integration_rot.fixer import draft_plaid_transactions_fix
    draft = draft_plaid_transactions_fix(tmp_path, "a.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "plaid_client.transactions_sync(" in new
    assert 'resp["added"]' in new
    assert 'resp["transactions"]' not in new
    assert "cursor = None" in new


def test_slack_sdk_import_and_on_decorator(tmp_path):
    """slack_sdk.rtm imports are rewritten (not just legacy `slack`), and
    @rtm.on(...) decorators don't dangle after the client var is renamed."""
    (tmp_path / "a.py").write_text(
        'from slack_sdk.rtm import RTMClient\nrtm = RTMClient(token="x")\n'
        '@rtm.on("message")\ndef handle(**payload):\n    pass\nrtm.start()\n')
    from integration_rot.fixer import draft_slack_rtm_fix
    draft = draft_slack_rtm_fix(tmp_path, "a.py")
    assert not draft.empty()
    new = draft.changes[0].new_content
    assert "from slack_sdk.socket_mode import SocketModeClient" in new
    assert "from slack_sdk.rtm import RTMClient" not in new
    assert "@rtm.on" not in new
    assert "socket_mode_request_listeners" in new


def _run_generated_test(tmp_path, entry_id, filename, source, **kwargs):
    """Draft, apply, and execute the generated contract test in a bare env."""
    import subprocess
    import sys as _sys
    from integration_rot.fixer import draft_fix, write_fix
    repo = tmp_path / "r"
    repo.mkdir(exist_ok=True)
    (repo / "app.py").write_text(source)
    draft = draft_fix(entry_id, repo, "app.py", module="app", **kwargs)
    assert not draft.empty(), f"no draft for {entry_id}"
    write_fix(repo, draft)
    proc = subprocess.run(
        [_sys.executable, "-m", "pytest", f"tests/{filename}",
         "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=repo, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout[-2000:]


def test_e2e_twilio_sdk_style_contract(tmp_path):
    _run_generated_test(
        tmp_path, "twilio-authy-api", "test_twilio_verify_contract.py",
        'from twilio.rest import Client\nclient = Client("ACx", "tok")\n'
        'def start_verification(to, channel):\n'
        '    return client.authy.services("VAX").verifications.create(to=to, channel=channel)\n'
        'def check_verification(to, code):\n'
        '    return client.authy.services("VAX").verification_checks.create(to=to, code=code)\n')


def test_e2e_plaid_snake_case_contract(tmp_path):
    _run_generated_test(
        tmp_path, "plaid-legacy-transactions", "test_plaid_sync_contract.py",
        'plaid_client = None\ndef sync_transactions(access_token):\n'
        '    resp = plaid_client.transactions_get({"access_token": access_token})\n'
        '    return resp["transactions"]\n',
        func_name="sync_transactions")


def test_e2e_slack_sdk_contract(tmp_path):
    _run_generated_test(
        tmp_path, "slack-rtm-api", "test_slack_socket_mode_contract.py",
        'from slack_sdk.rtm import RTMClient\nrtm = RTMClient(token="xoxb-old")\n'
        '@rtm.on("message")\ndef handle(**payload):\n    pass\nrtm.start()\n')
