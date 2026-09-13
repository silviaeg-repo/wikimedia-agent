"""Typed results returned across the Wikipedia client boundary (§2.1).

These are the only shapes callers see. No ``httpx`` objects, no raw JSON dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(?P<marks>={2,6})\s*(?P<title>.+?)\s*(?P=marks)$", re.MULTILINE)

LEAD_SECTION = "Summary"
"""Name given to the text before the first heading."""


@dataclass(frozen=True)
class SearchResult:
    """One hit from a Wikipedia search."""

    title: str
    page_id: int
    snippet: str
    word_count: int


@dataclass(frozen=True)
class Section:
    """One section of an article, split from the plain-text extract."""

    title: str
    level: int
    text: str


@dataclass(frozen=True)
class Article:
    """An article, or one section of one.

    ``requested_title`` and ``redirected_from`` preserve how we got here: a
    redirect followed is itself provenance (§2.3).
    """

    title: str
    page_id: int
    text: str
    sections: tuple[Section, ...]
    requested_title: str
    redirected_from: str | None = None
    section_title: str | None = None
    truncated: bool = False

    @property
    def section_titles(self) -> tuple[str, ...]:
        return tuple(section.title for section in self.sections)

    def find_section(self, name: str) -> Section | None:
        """Look up a section by name, case-insensitively."""
        wanted = name.strip().casefold()
        for section in self.sections:
            if section.title.casefold() == wanted:
                return section
        return None


@dataclass(frozen=True)
class Summary:
    """An article's lead extract -- the cheap way to disambiguate before
    spending context on a full article."""

    title: str
    page_id: int
    extract: str
    requested_title: str
    redirected_from: str | None = None


def split_sections(extract: str) -> tuple[Section, ...]:
    """Split a plain-text extract into sections on its ``== Heading ==`` lines.

    Done locally rather than with a second API call: the heading markers are
    already in the extract, so parsing them costs nothing and keeps us inside
    the serial-request discipline (§2.1).
    """
    if not extract.strip():
        return ()

    sections: list[Section] = []
    matches = list(_HEADING.finditer(extract))

    lead = extract[: matches[0].start()] if matches else extract
    if lead.strip():
        sections.append(Section(title=LEAD_SECTION, level=1, text=lead.strip()))

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(extract)
        body = extract[start:end].strip()
        sections.append(
            Section(
                title=match.group("title"),
                level=len(match.group("marks")),
                text=body,
            )
        )
    return tuple(sections)


_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Search snippets arrive with ``<span class="searchmatch">`` markup."""
    import html

    return _WHITESPACE.sub(" ", html.unescape(_TAG.sub("", text))).strip()
