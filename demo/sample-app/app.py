"""Sample app with INTENTIONALLY outdated third-party API usage.

Used by `integration-rot demo` to show the full pipeline:
  1. stripe.Charge.create  -> legacy Charges API (fix_available)
  2. api.sendgrid.com/v2   -> retired SendGrid v2 API (already sunset)
"""
import requests
import stripe

stripe.api_key = "sk_test_123"  # NEVER commit a real key
stripe.api_version = "2019-12-03"  # pinned old API version


def create_charge(amount_cents, currency, token, description=""):
    """Charge a card using the legacy Charges API (deprecated pattern)."""
    charge = stripe.Charge.create(
        amount=amount_cents,
        currency=currency,
        source=token,
        description=description,
    )
    return charge


def send_receipt(to_email, amount_cents):
    """Send a receipt via the retired SendGrid v2 endpoint (deprecated)."""
    resp = requests.post(
        "https://api.sendgrid.com/v2/mail/send",
        json={
            "to": to_email,
            "subject": "Your receipt",
            "text": f"Charged ${amount_cents / 100:.2f}",
        },
        headers={"Authorization": "Bearer SG.sample"},
        timeout=10,
    )
    return resp.status_code
