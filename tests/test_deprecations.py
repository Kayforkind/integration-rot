"""Tests for the deprecation DB and fetcher architecture."""
import pytest

from integration_rot.analyzer import _parse_date
from integration_rot.deprecations import (
    GitHubReleasesFetcher,
    RSSChangelogFetcher,
    StaticDBFetcher,
    draft_entries_from_feed,
    fetch_all,
    get_fetcher,
    load_db,
    parse_feed,
)

RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Vendor Changelog</title>
<item><title>Authy API deprecation timeline</title>
<link>https://example.com/authy-deprecation</link>
<pubDate>Mon, 01 Feb 2021 00:00:00 GMT</pubDate>
<description>The Authy API is deprecated and will sunset next year. Migrate to Verify.</description></item>
<item><title>New dashboard widgets</title>
<link>https://example.com/widgets</link>
<pubDate>Tue, 02 Feb 2021 00:00:00 GMT</pubDate>
<description>We added new dashboard widgets. No action needed.</description></item>
</channel></rss>"""

ATOM_FIXTURE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Vendor Changelog</title>
<entry><title>Legacy API removal</title>
<link href="https://example.com/legacy-removal"/>
<updated>2021-03-01T00:00:00Z</updated>
<summary>The legacy API will be removed. Breaking change for v1 clients.</summary></entry>
</feed>"""


def test_db_loads_and_validates():
    entries = load_db()
    assert len(entries) >= 5
    for e in entries:
        assert e.id and e.vendor and e.title and e.source_url.startswith("http")
        assert e.severity in ("breaking", "warning", "informational")
        assert _parse_date(e.announced) is not None  # must parse (YYYY-MM allowed)
        if e.sunset:
            assert _parse_date(e.sunset) is not None


def test_static_fetcher_filters_vendor():
    fetcher = StaticDBFetcher("Stripe")
    entries = fetcher.fetch()
    assert entries and all(e.vendor == "Stripe" for e in entries)


def test_get_fetcher_defaults_to_static():
    assert isinstance(get_fetcher("Twilio"), StaticDBFetcher)


def test_github_releases_stub_still_raises():
    with pytest.raises(NotImplementedError):
        GitHubReleasesFetcher("Stripe", "stripe/stripe-python").fetch()


def test_parse_feed_rss():
    items = parse_feed(RSS_FIXTURE)
    assert len(items) == 2
    assert items[0]["title"] == "Authy API deprecation timeline"
    assert items[0]["link"] == "https://example.com/authy-deprecation"
    assert "2021" in items[0]["published"]
    assert "deprecated" in items[0]["summary"]


def test_parse_feed_atom_with_namespace():
    items = parse_feed(ATOM_FIXTURE)
    assert len(items) == 1
    assert items[0]["title"] == "Legacy API removal"
    assert items[0]["link"] == "https://example.com/legacy-removal"


def test_draft_entries_filters_signals(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(RSS_FIXTURE)
    drafts = draft_entries_from_feed("Twilio", str(feed))
    assert len(drafts) == 1
    d = drafts[0]
    assert d["vendor"] == "Twilio"
    assert "deprecation" in d["candidate_title"].lower()
    assert "deprecat" in d["matched_signals"]
    assert d["link"] == "https://example.com/authy-deprecation"
    assert "REVIEW REQUIRED" in d["note"]


def test_draft_entries_no_signals(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(ATOM_FIXTURE.replace(
        "Legacy API removal", "Faster API responses").replace(
        "The legacy API will be removed. Breaking change for v1 clients.",
        "The API is now faster for all clients."))
    drafts = draft_entries_from_feed("Acme", str(feed))
    assert drafts == []


def test_rss_fetcher_returns_informational_entries(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(RSS_FIXTURE)
    entries = RSSChangelogFetcher("Twilio", str(feed)).fetch()
    assert len(entries) == 1
    e = entries[0]
    assert e.vendor == "Twilio"
    assert e.severity == "informational"
    assert e.sunset is None
    assert not e.fix_available
    assert e.source_url == "https://example.com/authy-deprecation"


def test_fetch_all_aggregates():
    entries = fetch_all(["Stripe", "Twilio"])
    vendors = {e.vendor for e in entries}
    assert vendors == {"Stripe", "Twilio"}
