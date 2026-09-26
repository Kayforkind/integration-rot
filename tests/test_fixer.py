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
        "https://api.sendgrid.com/v2/mail/send",
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
    assert "api.sendgrid.com/v2" not in new
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
