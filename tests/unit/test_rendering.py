"""Source rendering and quality flags (§2.3, Phase 7).

The point of these: a mis-flagged source is a failing unit test here, not a
behaviour regression that only an eval run would notice.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wikimedia_agent.provenance import Grade, Provenance
from wikimedia_agent.rendering import (
    FOOTER_NOTE,
    POOR_MARKER,
    UNKNOWN_MARKER,
    SourceRegistry,
    render,
)


def prov(title="Ada Lovelace", page_id=974, revision_id=42, section=None):
    return Provenance(
        title=title,
        page_id=page_id,
        revision_id=revision_id,
        article_url=f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
        permalink=f"https://en.wikipedia.org/w/index.php?oldid={revision_id}",
        retrieved_at=datetime.now(timezone.utc),
        section=section,
    )


# -- marker decoration -----------------------------------------------------


@pytest.mark.parametrize("grade", [Grade.FA, Grade.FL, Grade.A, Grade.GA, Grade.B, Grade.C])
def test_adequate_and_strong_sources_render_a_bare_marker(grade):
    out = render("A claim. [[Ada Lovelace]]", [prov()], {974: grade})
    assert "[1]" in out.text
    assert POOR_MARKER not in out.text.split("Sources")[0]


@pytest.mark.parametrize("grade", [Grade.START, Grade.STUB, Grade.UNASSESSED])
def test_poor_sources_decorate_the_inline_marker(grade):
    """Inline, at the point of the claim -- a warning only in the source list
    is easy to read past on a multi-claim answer."""
    out = render("A claim. [[Ada Lovelace]]", [prov()], {974: grade})
    body = out.text.split("Sources")[0]
    assert f"[1 {POOR_MARKER} {grade.short_label}]" in body


@pytest.mark.parametrize("grade", list(Grade))
def test_no_wikipedia_grade_jargon_reaches_the_reader(grade):
    """"Start-class" means nothing to most people (§2.3)."""
    out = render("A claim. [[Ada Lovelace]]", [prov()], {974: grade})
    assert "-class" not in out.text
    for jargon in ("FA", "GA", "Stub-", "Start-"):
        assert f"{jargon}-class" not in out.text


def test_an_unassessed_source_counts_as_poor():
    """An ungraded article is an unknown, and an unknown is not an endorsement."""
    out = render("A claim. [[Ada Lovelace]]", [prov()], {})
    assert POOR_MARKER in out.text
    assert out.has_poor_sources is True


def test_only_poor_sources_are_decorated_in_a_mixed_answer():
    """If every citation carried a marker, the marker would stop meaning anything."""
    out = render(
        "Good. [[Ada Lovelace]] Weak. [[Gerald J. Ford]]",
        [prov(), prov("Gerald J. Ford", 7, 99)],
        {974: Grade.GA, 7: Grade.START},
    )
    body = out.text.split("Sources")[0]
    assert "[1]" in body
    assert f"[2 {POOR_MARKER} {Grade.START.short_label}]" in body


# -- the footer note -------------------------------------------------------


def test_the_footer_renders_once_when_a_poor_source_is_present():
    out = render(
        "A. [[Ada Lovelace]] B. [[Gerald J. Ford]]",
        [prov(), prov("Gerald J. Ford", 7, 99)],
        {974: Grade.START, 7: Grade.STUB},
    )
    assert out.text.count(FOOTER_NOTE) == 1


def test_no_footer_when_every_source_is_adequate():
    out = render("A claim. [[Ada Lovelace]]", [prov()], {974: Grade.B})
    assert FOOTER_NOTE not in out.text
    assert out.has_poor_sources is False


# -- numbering -------------------------------------------------------------


def test_numbers_follow_retrieval_order():
    out = render(
        "Second first. [[Charles Babbage]] Then. [[Ada Lovelace]]",
        [prov(), prov("Charles Babbage", 5, 13)],
        {974: Grade.B, 5: Grade.GA},
    )
    assert "[1] Ada Lovelace" in out.text
    assert "[2] Charles Babbage" in out.text


def test_the_same_article_keeps_one_number_however_often_cited():
    out = render(
        "A. [[Ada Lovelace]] B. [[Ada Lovelace]] C. [[Ada Lovelace]]",
        [prov()],
        {974: Grade.B},
    )
    assert out.text.split("Sources")[0].count("[1]") == 3
    assert len(out.citations) == 1


def test_sections_of_one_article_share_its_number():
    """A citation refers to the article, not the section."""
    out = render(
        "A. [[Ada Lovelace]]",
        [prov(section="Death"), prov(section="Work")],
        {974: Grade.B},
    )
    assert len(out.citations) == 1


def test_citation_lookup_ignores_a_section_suffix_and_case():
    out = render("A. [[ada lovelace#Death]]", [prov()], {974: Grade.B})
    assert "[1]" in out.text
    assert out.unresolved == []


# -- the source list -------------------------------------------------------


def test_the_source_list_shows_article_urls_not_revisions():
    """Readers want the live article; the revision is for verification only."""
    out = render("A. [[Ada Lovelace]]", [prov()], {974: Grade.B})
    assert "https://en.wikipedia.org/wiki/Ada_Lovelace" in out.text
    assert "oldid=" not in out.text
    assert "42" not in out.text.split("Sources")[1]


def test_every_retrieved_article_is_listed_even_if_not_cited():
    """An article that informed the answer cannot go unlisted."""
    out = render(
        "Only one cited. [[Ada Lovelace]]",
        [prov(), prov("Charles Babbage", 5, 13)],
        {974: Grade.B, 5: Grade.GA},
    )
    assert "Charles Babbage" in out.text
    assert "consulted, not cited" in out.text


def test_a_cited_article_is_not_marked_as_merely_consulted():
    out = render("A. [[Ada Lovelace]]", [prov()], {974: Grade.B})
    assert "consulted, not cited" not in out.text


def test_the_source_list_names_the_section_when_scoped():
    out = render("A. [[Ada Lovelace]]", [prov(section="Death")], {974: Grade.B})
    assert "Ada Lovelace § Death" in out.text


def test_every_source_line_describes_its_reliability_in_plain_words():
    out = render(
        "A. [[Ada Lovelace]]",
        [prov(), prov("Gerald J. Ford", 7, 99)],
        {974: Grade.B, 7: Grade.START},
    )
    assert Grade.B.reliability in out.text
    assert Grade.START.reliability in out.text
    assert "well developed" in out.text
    assert "may be incomplete" in out.text


# -- fabricated citations --------------------------------------------------


def test_citing_an_article_never_retrieved_is_marked_not_dropped():
    """Silently dropping it would hide a fabricated citation."""
    out = render("A claim. [[Invented Article]]", [prov()], {974: Grade.B})
    assert UNKNOWN_MARKER in out.text
    assert out.unresolved == ["Invented Article"]
    assert "not among the articles retrieved" in out.text


def test_a_resolved_citation_leaves_no_unresolved_entry():
    out = render("A. [[Ada Lovelace]]", [prov()], {974: Grade.B})
    assert out.unresolved == []


# -- edge cases ------------------------------------------------------------


def test_an_answer_with_no_retrieval_renders_without_a_source_list():
    out = render("I could not find this in Wikipedia.", [], {})
    assert "Sources" not in out.text
    assert out.citations == []


def test_an_uncited_answer_still_lists_what_was_retrieved():
    out = render("An answer with no citations.", [prov()], {974: Grade.B})
    assert "Sources" in out.text
    assert out.cited_numbers == []


def test_titles_needing_escaping_produce_valid_urls():
    out = render("A. [[Mercury (planet)]]", [prov("Mercury (planet)", 3, 5)], {3: Grade.B})
    assert "https://en.wikipedia.org/wiki/Mercury_(planet)" in out.text
    assert " " not in out.text.split("https://")[1].split()[0]


def test_a_registry_assigns_numbers_once_and_never_renumbers():
    """Phase 8 depends on this: an article keeps its number for the session."""
    registry = SourceRegistry()
    first = registry.register(prov(), Grade.B)
    second = registry.register(prov("Charles Babbage", 5, 13), Grade.GA)
    again = registry.register(prov(), Grade.B)

    assert (first.number, second.number) == (1, 2)
    assert again.number == 1


# -- the source list is scoped to the turn (§2.5) -------------------------
#
# Regression: a session-wide registry was listed in full under every answer, so
# a question about churches was footnoted with articles about extraterrestrial
# life read two turns earlier.


def test_earlier_turns_articles_are_not_listed_under_a_later_answer():
    registry = SourceRegistry()
    render("Aliens. [[Search for extraterrestrial intelligence]]",
           [prov("Search for extraterrestrial intelligence", 11, 1)],
           {11: Grade.B}, registry=registry)

    later = render("Churches are ambiguous. Which did you mean?", [], {}, registry=registry)

    assert later.citations == []
    assert "Sources" not in later.text
    assert "extraterrestrial" not in later.text


def test_a_later_turn_lists_only_what_it_used():
    registry = SourceRegistry()
    render("First. [[Ada Lovelace]]", [prov()], {974: Grade.B}, registry=registry)

    second = render("Second. [[Charles Babbage]]",
                    [prov("Charles Babbage", 5, 13)], {5: Grade.GA}, registry=registry)

    assert [c.provenance.title for c in second.citations] == ["Charles Babbage"]
    assert "Ada Lovelace" not in second.text


def test_citing_an_earlier_article_lists_it_without_refetching():
    """A follow-up may legitimately refer back to [1] with no new retrieval."""
    registry = SourceRegistry()
    render("First. [[Ada Lovelace]]", [prov()], {974: Grade.B}, registry=registry)

    second = render("As noted earlier. [[Ada Lovelace]]", [], {}, registry=registry)

    assert [c.number for c in second.citations] == [1]
    assert "[1]" in second.text
    assert second.cited_numbers == [1]


def test_numbering_still_spans_the_session():
    """Scoping the list must not reset the numbers."""
    registry = SourceRegistry()
    render("First. [[Ada Lovelace]]", [prov()], {974: Grade.B}, registry=registry)
    second = render("Second. [[Charles Babbage]]",
                    [prov("Charles Babbage", 5, 13)], {5: Grade.GA}, registry=registry)

    assert second.citations[0].number == 2, "the second article keeps number 2"


def test_the_cited_flag_does_not_persist_across_turns():
    """Once-cited must not mean always-cited: it would suppress the
    'consulted, not cited' note on a later turn."""
    registry = SourceRegistry()
    render("Cited here. [[Ada Lovelace]]", [prov()], {974: Grade.B}, registry=registry)

    second = render("Not cited this time.", [prov()], {974: Grade.B}, registry=registry)

    assert second.citations[0].cited is False
    assert "consulted, not cited" in second.text


def test_an_unresolved_citation_is_reported_even_with_no_sources():
    """The warning must survive when there is no source list to append it to."""
    out = render("It is ambiguous. [[St. Mary's Church]]", [], {})
    assert out.unresolved == ["St. Mary's Church"]
    assert UNKNOWN_MARKER in out.text
    assert "not among the articles retrieved" in out.text


# -- ambiguous titles are not fabricated citations (§2.4) -----------------
#
# Regression: naming the disambiguation page it had just been told about got
# marked "[?] ... Treat those claims as unsupported", which reads as an accuracy
# warning when the agent is simply asking which subject was meant.


def test_naming_an_ambiguous_title_is_not_flagged_as_unsupported():
    out = render(
        "\"St. Mary's Church\" is shared by many churches. [[St. Mary's Church]] "
        "Which one did you mean?",
        [], {},
        ambiguous_titles=["St. Mary's Church"],
    )
    assert UNKNOWN_MARKER not in out.text
    assert "unsupported" not in out.text
    assert out.unresolved == []


def test_removing_the_marker_leaves_clean_prose():
    out = render(
        "It is ambiguous. [[St. Mary's Church]] Which one did you mean?",
        [], {}, ambiguous_titles=["St. Mary's Church"],
    )
    assert "It is ambiguous. Which one did you mean?" in out.text
    assert "  " not in out.text


def test_a_marker_before_punctuation_does_not_leave_a_gap():
    out = render(
        "That name is ambiguous [[St. Mary's Church]].",
        [], {}, ambiguous_titles=["St. Mary's Church"],
    )
    assert "ambiguous." in out.text
    assert " ." not in out.text


def test_ambiguity_matching_ignores_case_and_sections():
    out = render(
        "Ambiguous. [[st. mary's church#Notable]]",
        [], {}, ambiguous_titles=["St. Mary's Church"],
    )
    assert out.unresolved == []


def test_a_genuinely_fabricated_citation_is_still_flagged():
    """Only titles the tools reported as ambiguous get the pass."""
    out = render(
        "A claim. [[Some Article I Never Read]]",
        [], {}, ambiguous_titles=["St. Mary's Church"],
    )
    assert UNKNOWN_MARKER in out.text
    assert out.unresolved == ["Some Article I Never Read"]


def test_an_ambiguous_mention_alongside_real_sources():
    """The ambiguity note disappears; the real citations stay."""
    out = render(
        "None in South America. [[St. Mary's Church]] The closest is in Stanley. "
        "[[Stanley, Falkland Islands]]",
        [prov("Stanley, Falkland Islands", 20, 7)], {20: Grade.C},
        ambiguous_titles=["St. Mary's Church"],
    )
    assert UNKNOWN_MARKER not in out.text
    assert "[1]" in out.text
    assert "Stanley, Falkland Islands" in out.text
    assert out.unresolved == []
