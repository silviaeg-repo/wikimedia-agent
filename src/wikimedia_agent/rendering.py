"""Answer rendering: source lists and quality flags (§2.3).

**Warnings are the renderer's job, not the model's.** If flagging a weak source
depended on the model remembering, it would be forgotten -- precisely on the
long multi-source answers where it matters most. So the model cites by article
title, and this module assigns the numbers, decorates markers whose source is
poor-quality, and builds the source list from what was *actually retrieved*.

A mis-flagged source is then a failing unit test rather than a behaviour
regression only an eval run would notice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .provenance import Grade, Provenance

CITATION = re.compile(r"\[\[([^\]|]+?)\]\]")
"""How the model cites: ``[[Ada Lovelace]]`` or ``[[Ada Lovelace#Death]]``."""

POOR_MARKER = "⚠"  # warning sign
FOOTER_NOTE = (
    f"{POOR_MARKER} Sources marked {POOR_MARKER} are rated below Wikipedia's B-class "
    "standard. Claims drawn from them may be incomplete or inadequately sourced."
)
UNKNOWN_MARKER = "[?]"


@dataclass
class Citation:
    """One numbered source in a rendered answer."""

    number: int
    provenance: Provenance
    grade: Grade
    cited: bool = False

    @property
    def is_poor(self) -> bool:
        return self.grade.is_poor

    def marker(self) -> str:
        if self.is_poor:
            return f"[{self.number} {POOR_MARKER} {self.grade.label}-class]"
        return f"[{self.number}]"

    def line(self) -> str:
        where = self.provenance.title
        if self.provenance.section:
            where = f"{where} § {self.provenance.section}"
        flag = f"  {POOR_MARKER} low-quality source" if self.is_poor else ""
        status = "" if self.cited else "  (consulted, not cited)"
        return (
            f"  [{self.number}] {where} — {self.grade.label}-class{flag}{status}\n"
            f"      {self.provenance.article_url}"
        )


@dataclass
class RenderedAnswer:
    """An answer with its markers decorated and its sources listed."""

    text: str
    citations: list[Citation] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def has_poor_sources(self) -> bool:
        return any(citation.is_poor for citation in self.citations)

    @property
    def cited_numbers(self) -> list[int]:
        return [c.number for c in self.citations if c.cited]

    def __str__(self) -> str:
        return self.text


class SourceRegistry:
    """Assigns stable citation numbers to retrieved articles.

    Numbers are assigned on first retrieval and never reused or renumbered, so a
    later turn can refer back to "[1]" and mean the same article (§2.5). Phase 8
    keeps one of these per session; Phase 7 builds one per answer.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[int, str | None], Citation] = {}
        self._next_number = 1

    def register(self, provenance: Provenance, grade: Grade) -> Citation:
        key = (provenance.page_id, provenance.section)
        existing = self._by_key.get(key)
        if existing is not None:
            return existing

        # An article already registered under a different section keeps its
        # number: a citation refers to the article, not the section.
        for (page_id, _section), citation in self._by_key.items():
            if page_id == provenance.page_id:
                shared = Citation(citation.number, provenance, grade)
                self._by_key[key] = shared
                return shared

        citation = Citation(self._next_number, provenance, grade)
        self._by_key[key] = citation
        self._next_number += 1
        return citation

    def find(self, title: str) -> Citation | None:
        """Look up by title, ignoring any ``#section`` suffix and case."""
        wanted = title.split("#", 1)[0].strip().casefold()
        for citation in self._by_key.values():
            if citation.provenance.title.casefold() == wanted:
                return citation
        return None

    def all(self) -> list[Citation]:
        seen: dict[int, Citation] = {}
        for citation in self._by_key.values():
            seen.setdefault(citation.number, citation)
        return [seen[number] for number in sorted(seen)]


def render(
    answer_text: str,
    retrievals: list[Provenance],
    grades: dict[int, Grade],
    *,
    registry: SourceRegistry | None = None,
) -> RenderedAnswer:
    """Decorate citations and append the source list.

    Everything an answer says about source quality comes from here, derived from
    the grade recorded at retrieval time -- never from the model's own account
    of it.
    """
    registry = registry or SourceRegistry()
    for provenance in retrievals:
        registry.register(provenance, grades.get(provenance.page_id, Grade.UNASSESSED))

    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        title = match.group(1).strip()
        citation = registry.find(title)
        if citation is None:
            # The model cited something it never retrieved: a fabricated
            # citation. Marked rather than silently dropped, so it is visible in
            # the answer and catchable by the eval scorers (§5).
            unresolved.append(title)
            return UNKNOWN_MARKER
        citation.cited = True
        return citation.marker()

    body = CITATION.sub(replace, answer_text).strip()
    citations = registry.all()

    if not citations:
        return RenderedAnswer(text=body, citations=[], unresolved=unresolved)

    parts = [body, "", "Sources"]
    parts.extend(citation.line() for citation in citations)

    if any(citation.is_poor for citation in citations):
        parts.extend(["", FOOTER_NOTE])
    if unresolved:
        parts.extend(
            [
                "",
                f"{UNKNOWN_MARKER} This answer referred to "
                f"{', '.join(repr(title) for title in unresolved)}, which was not among "
                "the articles retrieved. Treat those claims as unsupported.",
            ]
        )

    return RenderedAnswer(text="\n".join(parts), citations=citations, unresolved=unresolved)
