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
