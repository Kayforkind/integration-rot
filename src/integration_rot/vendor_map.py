"""Maps package names / API hostnames to canonical vendor names.

This is the knowledge base the scanner uses to attribute a dependency
(or a hardcoded API call) to the vendor whose changelog we track.
"""
from __future__ import annotations

# npm / pypi / go / gem / maven package name (lowercased) -> vendor
PACKAGE_TO_VENDOR: dict[str, str] = {
    # Payments
    "stripe": "Stripe",
    "@stripe/stripe-js": "Stripe",
    "braintree": "Braintree",
    "paypal": "PayPal",
    "@paypal/checkout-server-sdk": "PayPal",
    "square": "Square",
    "squareup": "Square",
    "adyen": "Adyen",
    "checkout": "Checkout.com",
    "checkout-sdk-node": "Checkout.com",
    # Comms
    "twilio": "Twilio",
    "@twilio/voice-sdk": "Twilio",
    "@sendgrid/mail": "SendGrid",
    "sendgrid": "SendGrid",
    "postmark": "Postmark",
    "postmark-js": "Postmark",
    "brevo": "Brevo",
    "sib-api-v3-sdk": "Brevo",
    "mailchimp-marketing": "Mailchimp",
    "@mailchimp/mailchimp_marketing": "Mailchimp",
    # CRM / sales
    "salesforce": "Salesforce",
    "jsforce": "Salesforce",
    "simple-salesforce": "Salesforce",
    "hubspot-api-client": "HubSpot",
    "@hubspot/api-client": "HubSpot",
    "zendesk": "Zendesk",
    "node-zendesk": "Zendesk",
    "intercom": "Intercom",
    "intercom-client": "Intercom",
    "pipedrive": "Pipedrive",
    # Identity
    "okta": "Okta",
    "@okta/okta-sdk-nodejs": "Okta",
    "auth0": "Auth0",
    "@auth0/auth0-spa-js": "Auth0",
    # Banking / fintech
    "plaid": "Plaid",
    # AI
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "@anthropic-ai/sdk": "Anthropic",
    # Dev / infra
    "octokit": "GitHub",
    "@octokit/rest": "GitHub",
    "datadog": "Datadog",
    "datadog-api-client": "Datadog",
    "@datadog/datadog-api-client": "Datadog",
    "segment": "Segment",
    "analytics-node": "Segment",
    "amplitude": "Amplitude",
    "@amplitude/analytics-node": "Amplitude",
    "notion": "Notion",
    "@notionhq/client": "Notion",
    "slack-sdk": "Slack",
    "@slack/web-api": "Slack",
    "@slack/bolt": "Slack",
    "shopify": "Shopify",
    "@shopify/shopify-api": "Shopify",
    "shopify-api-node": "Shopify",
    "supabase": "Supabase",
    "@supabase/supabase-js": "Supabase",
    "algolia": "Algolia",
    "algoliasearch": "Algolia",
    "authy": "Twilio",
    "zoom": "Zoom",
    "@zoomus/websdk": "Zoom",
    "discord": "Discord",
    "discord.js": "Discord",
}

# Well-known API hostnames -> vendor (for the direct-call heuristic).
HOST_TO_VENDOR: dict[str, str] = {
    "api.stripe.com": "Stripe",
    "api.twilio.com": "Twilio",
    "api.sendgrid.com": "SendGrid",
    "api.postmarkapp.com": "Postmark",
    "api.mailchimp.com": "Mailchimp",
    "slack.com": "Slack",
    "api.salesforce.com": "Salesforce",
    "api.hubapi.com": "HubSpot",
    "api.plaid.com": "Plaid",
    "api.openai.com": "OpenAI",
    "api.anthropic.com": "Anthropic",
    "api.notion.com": "Notion",
    "api.github.com": "GitHub",
}


def vendor_for_package(package_name: str) -> str | None:
    """Return the canonical vendor for a package name, or None."""
    return PACKAGE_TO_VENDOR.get(package_name.strip().lower())


def vendor_for_host(host: str) -> str | None:
    """Return the canonical vendor for an API hostname, or None."""
    host = host.strip().lower()
    if host in HOST_TO_VENDOR:
        return HOST_TO_VENDOR[host]
    # allow subdomains, e.g. "foo.api.stripe.com" is unlikely; match suffixes
    for known, vendor in HOST_TO_VENDOR.items():
        if host.endswith("." + known):
            return vendor
    return None
