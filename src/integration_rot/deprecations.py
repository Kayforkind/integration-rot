"""Deprecation knowledge base + fetcher architecture.

MVP: a curated static DB (data/deprecations.json). The fetcher abstraction
means live sources (changelog RSS, GitHub releases, vendor status APIs) can
be plugged in per-vendor later without changing the analyzer.
"""
from __future__ import annotations

import abc
import json
from pathlib import Path

from .models import DeprecationEntry

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "deprecations.json"


def load_db(path: str | Path = DEFAULT_DB_PATH) -> list[DeprecationEntry]:
    """Load the curated deprecation DB from JSON."""
    raw = json.loads(Path(path).read_text())
    entries = []
    for item in raw["deprecations"]:
        entries.append(DeprecationEntry(**item))
    return entries


# ---------------------------------------------------------------------------
# Fetcher architecture (v2: live feeds)
# ---------------------------------------------------------------------------

class FeedFetcher(abc.ABC):
    """Abstract source of deprecation entries for one vendor."""

    vendor: str

    @abc.abstractmethod
    def fetch(self) -> list[DeprecationEntry]:
        """Return fresh deprecation entries for this vendor."""
        raise NotImplementedError


class StaticDBFetcher(FeedFetcher):
    """MVP fetcher: serves the curated static DB."""

    def __init__(self, vendor: str, db_path: str | Path = DEFAULT_DB_PATH):
        self.vendor = vendor
        self.db_path = db_path

    def fetch(self) -> list[DeprecationEntry]:
        return [e for e in load_db(self.db_path)
                if e.vendor.lower() == self.vendor.lower()]


class RSSChangelogFetcher(FeedFetcher):
    """STUB (v2): poll a vendor changelog RSS/Atom feed and parse entries.

    Intended sources, e.g.:
      - Stripe:  https://docs.stripe.com/changelog  (no public RSS; needs scraping/API)
      - Twilio:  https://www.twilio.com/en-us/changelog (RSS available)
      - Slack:   https://api.slack.com/changelog (RSS available)
    """

    def __init__(self, vendor: str, feed_url: str):
        self.vendor = vendor
        self.feed_url = feed_url

    def fetch(self) -> list[DeprecationEntry]:
        raise NotImplementedError(
            f"Live RSS fetching for {self.vendor} is a v2 stub — "
            "wire feedparser + an LLM extractor here."
        )


class GitHubReleasesFetcher(FeedFetcher):
    """STUB (v2): watch a vendor SDK repo's releases for breaking-change notes."""

    def __init__(self, vendor: str, repo: str):
        self.vendor = vendor
        self.repo = repo  # e.g. "stripe/stripe-python"

    def fetch(self) -> list[DeprecationEntry]:
        raise NotImplementedError(
            f"Live GitHub release watching for {self.vendor} is a v2 stub."
        )


# Registry: vendor -> fetcher instance. Swap StaticDBFetcher for a live
# fetcher per vendor as feeds come online; analyzer code stays the same.
FETCHERS: dict[str, FeedFetcher] = {}


def get_fetcher(vendor: str, db_path: str | Path = DEFAULT_DB_PATH) -> FeedFetcher:
    """Return the configured fetcher for a vendor (static DB by default)."""
    if vendor.lower() in FETCHERS:
        return FETCHERS[vendor.lower()]
    return StaticDBFetcher(vendor, db_path)


def fetch_all(vendors: list[str], db_path: str | Path = DEFAULT_DB_PATH) -> list[DeprecationEntry]:
    """Fetch deprecation entries for all vendors of interest."""
    entries: list[DeprecationEntry] = []
    for vendor in vendors:
        entries.extend(get_fetcher(vendor, db_path).fetch())
    return entries
