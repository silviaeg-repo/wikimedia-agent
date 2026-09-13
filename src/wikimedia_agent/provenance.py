"""Provenance records and Wikipedia quality grades (§2.3).

Two jobs, both of which exist so an answer can be audited:

* **Provenance** pins every retrieval to an exact revision, so a citation means
  "this text, in this revision" rather than "this article, whatever it says
  now".
* **Quality** attaches each article's `Wikipedia assessment grade
  <https://en.wikipedia.org/wiki/Wikipedia:Content_assessment>`_, so a claim
  resting on a Stub is visibly different from one resting on a Featured Article.

Both are derived from API response metadata **only**. Article text claiming a
revision number or a Featured rating changes neither.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

PROJECT_INDEPENDENT = "Project-independent assessment"
"""The canonical cross-project grade, when a page has one."""


class Tier(Enum):
    """How much weight a source's grade earns.

    Collapsing ten grades to three keeps the rendered warning meaningful: it
    fires on genuinely weak sourcing rather than on every citation.
    """

    STRONG = "strong"
    ADEQUATE = "adequate"
    POOR = "poor"

    @property
    def is_poor(self) -> bool:
        return self is Tier.POOR


class Grade(Enum):
    """A Wikipedia content-assessment class, best to worst.

    The enum *value* is the rank used for comparison, so "the lowest grade among
    these projects" is just ``max``.
    """

    FA = 0
    FL = 1
    A = 2
    GA = 3
    B = 4
    C = 5
    LIST = 6
    START = 7
    STUB = 8
    UNASSESSED = 9

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def tier(self) -> Tier:
        if self in (Grade.FA, Grade.FL, Grade.A, Grade.GA):
            return Tier.STRONG
        if self in (Grade.B, Grade.C, Grade.LIST):
            return Tier.ADEQUATE
        return Tier.POOR

    @property
    def is_poor(self) -> bool:
        """Start, Stub and Unassessed. These get flagged in answers (§2.3)."""
        return self.tier.is_poor

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self]


_LABELS = {
    Grade.FA: "FA",
    Grade.FL: "FL",
    Grade.A: "A",
    Grade.GA: "GA",
    Grade.B: "B",
    Grade.C: "C",
    Grade.LIST: "List",
    Grade.START: "Start",
    Grade.STUB: "Stub",
    Grade.UNASSESSED: "Unassessed",
}

_DESCRIPTIONS = {
    Grade.FA: "Featured Article -- Wikipedia's best work",
    Grade.FL: "Featured List -- meets the featured criteria for lists",
    Grade.A: "Well organized and essentially complete",
    Grade.GA: "Good Article -- formally reviewed",
    Grade.B: "Mostly complete with solid references",
    Grade.C: "Substantial but missing important elements",
    Grade.LIST: "Stand-alone list or set index article",
    Grade.START: "Developing but quite incomplete",
    Grade.STUB: "Very basic; minimal meaningful content",
    Grade.UNASSESSED: "No grade recorded",
}

_BY_NAME = {
    "fa": Grade.FA,
    "fl": Grade.FL,
    "a": Grade.A,
    "ga": Grade.GA,
    "b": Grade.B,
    "c": Grade.C,
    "list": Grade.LIST,
    "start": Grade.START,
    "stub": Grade.STUB,
}


def parse_grade(raw: str) -> Grade | None:
    """Map one API ``class`` value to a :class:`Grade`.

    Returns ``None`` for anything unrecognised -- the API also reports
    non-article classes such as ``NA``, ``Disambig`` and ``Category``, which say
    nothing about article quality and must not be mistaken for a grade.
    """
    return _BY_NAME.get(raw.strip().casefold())


def resolve_grade(assessments: Mapping[str, Any]) -> Grade:
    """Reduce a page's per-WikiProject assessments to one grade.

    Projects can and do disagree -- Barack Obama carries sixteen entries -- so
    the rule is deterministic (principle #15):

    1. the ``Project-independent assessment`` when present (it almost always is),
    2. otherwise the **lowest** grade among the projects, because erring
       pessimistic is the honest direction for a quality signal,
    3. otherwise ``UNASSESSED``. Never guessed, never inferred from article
       length or prose style.
    """
    if not assessments:
        return Grade.UNASSESSED

    canonical = assessments.get(PROJECT_INDEPENDENT)
    if isinstance(canonical, Mapping):
        grade = parse_grade(str(canonical.get("class", "")))
        if grade is not None:
            return grade

    grades = [
        grade
        for entry in assessments.values()
        if isinstance(entry, Mapping)
        for grade in [parse_grade(str(entry.get("class", "")))]
        if grade is not None
    ]
    if not grades:
        return Grade.UNASSESSED
    return max(grades, key=lambda g: g.value)


def collect_importance(assessments: Mapping[str, Any]) -> dict[str, str]:
    """Per-project importance ratings.

    Recorded for completeness and **never** used as quality: importance says how
    central a topic is to a project, not how good the article is, and it is
    frequently an empty string.
    """
    return {
        str(project): str(entry.get("importance", ""))
        for project, entry in assessments.items()
        if isinstance(entry, Mapping) and entry.get("importance")
    }


def _site_base(api_url: str) -> str:
    parts = urllib.parse.urlsplit(api_url)
    return f"{parts.scheme}://{parts.netloc}"


def article_url(api_url: str, title: str) -> str:
    """The canonical, human-facing article URL -- what an answer displays."""
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="/:()_,.'-")
    return f"{_site_base(api_url)}/wiki/{slug}"


def permalink(api_url: str, revision_id: int) -> str:
    """A URL resolving to the exact revision we read.

    Not displayed in answers -- it is what citation verification and eval
    re-runs check against (§2.3).
    """
    return f"{_site_base(api_url)}/w/index.php?oldid={revision_id}"


@dataclass(frozen=True)
class Provenance:
    """Where a piece of retrieved text came from, exactly."""

    title: str
    page_id: int
    revision_id: int
    article_url: str
    permalink: str
    retrieved_at: datetime
    revision_timestamp: datetime | None = None
    requested_title: str | None = None
    redirected_from: str | None = None
    section: str | None = None

    @property
    def is_complete(self) -> bool:
        """Whether this record can support a verifiable citation."""
        return bool(self.title) and self.page_id > 0 and self.revision_id > 0

    def cite(self) -> str:
        """One-line human-readable source reference."""
        where = f"{self.title}#{self.section}" if self.section else self.title
        return f"{where} ({self.article_url})"


def parse_timestamp(raw: str) -> datetime | None:
    """Parse MediaWiki's ISO-8601 ``2026-08-29T16:16:29Z`` timestamps."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
