"""Core data models shared across scanner, deprecations, analyzer, fixer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Dependency:
    """A third-party package declared by a manifest file."""

    name: str
    version: Optional[str] = None      # pinned/resolved version, if known
    spec: Optional[str] = None         # declared version spec, e.g. "^8.0.0"
    ecosystem: str = ""                # npm | pypi | go | bundler | maven
    manifest: str = ""                 # manifest file path (relative to repo)
    vendor: Optional[str] = None       # canonical vendor name, if mappable


@dataclass
class DeprecationEntry:
    """One known deprecation: an endpoint, SDK major, or API version going away."""

    id: str
    vendor: str
    title: str
    announced: str                     # ISO date, e.g. "2021-03-15"
    sunset: Optional[str]              # ISO date or None if not announced
    severity: str                      # "breaking" | "warning" | "informational"
    packages: list = field(default_factory=list)        # [{ecosystem, name, version_spec}]
    code_patterns: list = field(default_factory=list)  # regex strings matched against source
    migration: str = ""
    source_url: str = ""
    fix_available: bool = False         # whether fixer.py can draft a patch


@dataclass
class DirectCall:
    """A hardcoded call to a vendor API hostname found in source."""

    vendor: str
    host: str
    file: str
    line: int
    snippet: str


@dataclass
class ScanResult:
    repo: str
    dependencies: list = field(default_factory=list)   # list[Dependency]
    direct_calls: list = field(default_factory=list)   # list[DirectCall]
    source_files: list = field(default_factory=list)   # list[str] relative paths


@dataclass
class Finding:
    entry: DeprecationEntry
    dependency: Optional[Dependency]
    evidence: list = field(default_factory=list)  # human-readable evidence strings
    risk: str = "low"                             # critical | high | medium | low
    days_to_sunset: Optional[int] = None


@dataclass
class Report:
    repo: str
    generated: str                        # ISO datetime
    findings: list = field(default_factory=list)  # list[Finding], ranked

    @property
    def counts(self) -> dict:
        out = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for f in self.findings:
            out[f.risk] = out.get(f.risk, 0) + 1
        return out
