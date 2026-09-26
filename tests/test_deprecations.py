"""Tests for the deprecation DB and fetcher architecture."""
from datetime import date

import pytest

from integration_rot.deprecations import (
    GitHubReleasesFetcher,
    RSSChangelogFetcher,
    StaticDBFetcher,
    fetch_all,
    get_fetcher,
    load_db,
)


def test_db_loads_and_validates():
    entries = load_db()
    assert len(entries) >= 5
    for e in entries:
        assert e.id and e.vendor and e.title and e.source_url.startswith("http")
        assert e.severity in ("breaking", "warning", "informational")
        date.fromisoformat(e.announced)  # must parse
        if e.sunset:
            date.fromisoformat(e.sunset)


def test_static_fetcher_filters_vendor():
    fetcher = StaticDBFetcher("Stripe")
    entries = fetcher.fetch()
    assert entries and all(e.vendor == "Stripe" for e in entries)


def test_get_fetcher_defaults_to_static():
    assert isinstance(get_fetcher("Twilio"), StaticDBFetcher)


def test_live_stubs_raise():
    with pytest.raises(NotImplementedError):
        RSSChangelogFetcher("Slack", "https://example.com/rss").fetch()
    with pytest.raises(NotImplementedError):
        GitHubReleasesFetcher("Stripe", "stripe/stripe-python").fetch()


def test_fetch_all_aggregates():
    entries = fetch_all(["Stripe", "Twilio"])
    vendors = {e.vendor for e in entries}
    assert vendors == {"Stripe", "Twilio"}
