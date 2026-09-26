"""Deprecation knowledge base + fetcher architecture.

MVP: a curated static DB (data/deprecations.json). Live sources (changelog
RSS/Atom feeds, GitHub releases) plug in per-vendor via the FeedFetcher
abstraction without changing the analyzer.
"""
from __future__ import annotations

import abc
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
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
    """Poll a vendor changelog RSS/Atom feed and surface deprecation signals.

    Feed items whose title/summary match deprecation keywords become
    `informational` DeprecationEntries flagged for human review — the feed is
    a tripwire, not a source of truth. Verify against vendor docs before
    promoting a candidate into the curated DB.
    """

    def __init__(self, vendor: str, feed_url: str):
        self.vendor = vendor
        self.feed_url = feed_url

    def fetch(self) -> list[DeprecationEntry]:
        out = []
        for d in draft_entries_from_feed(self.vendor, self.feed_url):
            slug = re.sub(r"[^a-z0-9]+", "-",
                          d["candidate_title"].lower()).strip("-")[:60]
            out.append(DeprecationEntry(
                id=f"{self.vendor.lower()}-feed-{slug}",
                vendor=self.vendor,
                title=f"[feed] {d['candidate_title']}",
                announced=d["published"] or "",
                sunset=None,
                severity="informational",
                packages=[],
                code_patterns=[],
                migration="",
                source_url=d["link"],
                fix_available=False,
            ))
        return out


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


# ---------------------------------------------------------------------------
# RSS/Atom feed parsing (stdlib only) + deprecation-signal extraction
# ---------------------------------------------------------------------------

SIGNAL_RE = re.compile(
    r"deprecat|retir|sunset|end[\s-]*of[\s-]*life|breaking[\s-]*change|"
    r"\bremoved?\b|shut\s*down|no longer support|will stop|discontinu",
    re.IGNORECASE,
)


def _local(tag: str) -> str:
    """Strip an XML namespace: '{http://...}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1]


def _child_text(elem: ET.Element, name: str) -> str:
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _link_of(elem: ET.Element) -> str:
    """RSS: <link>text</link>. Atom: <link href='...'/>."""
    for child in elem:
        if _local(child.tag) == "link":
            return child.get("href", "").strip() or (child.text or "").strip()
    return ""


def parse_feed(xml_text: str) -> list[dict]:
    """Parse RSS 2.0 or Atom XML into [{title, link, published, summary}]."""
    root = ET.fromstring(xml_text)
    items = []
    for elem in root.iter():
        if _local(elem.tag) not in ("item", "entry"):
            continue
        title = _child_text(elem, "title")
        link = _link_of(elem)
        published = (_child_text(elem, "pubDate") or _child_text(elem, "published")
                     or _child_text(elem, "updated"))
        summary = (_child_text(elem, "description") or _child_text(elem, "summary")
                   or _child_text(elem, "content"))
        if title:
            items.append({"title": title, "link": link,
                          "published": published, "summary": summary})
    return items


def fetch_feed_items(feed: str) -> list[dict]:
    """Load feed items from an http(s) URL or a local XML file."""
    if feed.startswith(("http://", "https://")):
        with urllib.request.urlopen(feed, timeout=30) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
    else:
        xml_text = Path(feed).read_text()
    return parse_feed(xml_text)


def draft_entries_from_feed(vendor: str, feed: str) -> list[dict]:
    """Extract deprecation-signal candidates from a changelog feed.

    Returns draft dicts for human review — NOT trusted DB entries.
    Each draft carries the matched signal keywords and a REVIEW REQUIRED note.
    """
    drafts = []
    for item in fetch_feed_items(feed):
        haystack = f"{item['title']} {item['summary']}"
        signals = sorted({s.lower() for s in SIGNAL_RE.findall(haystack)})
        if not signals:
            continue
        drafts.append({
            "vendor": vendor,
            "candidate_title": item["title"],
            "link": item["link"],
            "published": item["published"],
            "matched_signals": signals,
            "summary": item["summary"][:500],
            "note": "REVIEW REQUIRED — verify against vendor docs before "
                    "adding to deprecations.json",
        })
    return drafts
