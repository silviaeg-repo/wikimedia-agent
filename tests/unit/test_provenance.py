"""Provenance records and quality-grade resolution (§2.3)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from wikimedia_agent.provenance import (
    PROJECT_INDEPENDENT,
    Grade,
    Provenance,
    Tier,
    article_url,
    collect_importance,
    parse_grade,
    parse_timestamp,
    permalink,
    resolve_grade,
)

# -- the grading scale -----------------------------------------------------


@pytest.mark.parametrize(
    "grade,tier",
    [
        (Grade.FA, Tier.STRONG),
        (Grade.FL, Tier.STRONG),
        (Grade.A, Tier.STRONG),
        (Grade.GA, Tier.STRONG),
        (Grade.B, Tier.ADEQUATE),
        (Grade.C, Tier.ADEQUATE),
        (Grade.LIST, Tier.ADEQUATE),
        (Grade.START, Tier.POOR),
        (Grade.STUB, Tier.POOR),
        (Grade.UNASSESSED, Tier.POOR),
    ],
)
def test_every_grade_maps_to_the_documented_tier(grade, tier):
    assert grade.tier is tier


def test_only_start_stub_and_unassessed_are_poor():
    poor = {grade for grade in Grade if grade.is_poor}
    assert poor == {Grade.START, Grade.STUB, Grade.UNASSESSED}


def test_grades_are_ordered_best_to_worst():
    assert Grade.FA.value < Grade.GA.value < Grade.B.value < Grade.STUB.value


@pytest.mark.parametrize(
    "raw,expected",
    [("FA", Grade.FA), ("ga", Grade.GA), (" Start ", Grade.START), ("STUB", Grade.STUB),
     ("List", Grade.LIST)],
)
def test_grade_parsing_is_case_and_space_insensitive(raw, expected):
    assert parse_grade(raw) is expected


@pytest.mark.parametrize("raw", ["NA", "Disambig", "Category", "Template", "Redirect", ""])
def test_non_article_classes_are_not_grades(raw):
    """The API reports these too. They say nothing about article quality."""
    assert parse_grade(raw) is None


# -- resolution across disagreeing projects --------------------------------


def test_project_independent_assessment_wins():
    assessments = {
        PROJECT_INDEPENDENT: {"class": "B"},
        "Biography": {"class": "Stub"},
        "Physics": {"class": "FA"},
    }
    assert resolve_grade(assessments) is Grade.B


def test_lowest_grade_wins_without_a_project_independent_entry():
    """Erring pessimistic is the honest direction for a quality signal."""
    assessments = {"Physics": {"class": "GA"}, "Biography": {"class": "Start"}}
    assert resolve_grade(assessments) is Grade.START


def test_unassessed_when_there_are_no_assessments():
    assert resolve_grade({}) is Grade.UNASSESSED


def test_unassessed_when_no_entry_is_a_real_grade():
    assert resolve_grade({"X": {"class": "Disambig"}}) is Grade.UNASSESSED


def test_unparseable_project_independent_falls_back_to_projects():
    assessments = {PROJECT_INDEPENDENT: {"class": "NA"}, "Physics": {"class": "GA"}}
    assert resolve_grade(assessments) is Grade.GA


def test_grade_is_never_inferred_from_absence():
    """An ungraded article is an unknown, and an unknown is not an endorsement."""
    assert resolve_grade({}).tier is Tier.POOR


# -- importance is recorded, never used as quality -------------------------


def test_importance_never_influences_the_grade():
    high = {"Physics": {"class": "Stub", "importance": "Top"}}
    low = {"Physics": {"class": "Stub", "importance": "Low"}}
    assert resolve_grade(high) is resolve_grade(low) is Grade.STUB


def test_empty_importance_values_are_dropped():
    """The API frequently returns an empty string here."""
    assessments = {"A": {"class": "B", "importance": ""}, "B": {"class": "B", "importance": "Mid"}}
    assert collect_importance(assessments) == {"B": "Mid"}


# -- URLs and timestamps ---------------------------------------------------


def test_article_url_is_the_canonical_human_facing_link():
    url = article_url("https://en.wikipedia.org/w/api.php", "Gerald J. Ford")
    assert url == "https://en.wikipedia.org/wiki/Gerald_J._Ford"


def test_article_url_escapes_titles_that_need_it():
    url = article_url("https://en.wikipedia.org/w/api.php", "Mercury & Co / Ltd")
    assert " " not in url
    assert url.startswith("https://en.wikipedia.org/wiki/")


def test_permalink_points_at_the_exact_revision():
    link = permalink("https://en.wikipedia.org/w/api.php", 1350392216)
    assert link.endswith("oldid=1350392216")


def test_timestamp_parsing_is_utc():
    parsed = parse_timestamp("2026-08-29T16:16:29Z")
    assert parsed == datetime(2026, 8, 29, 16, 16, 29, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", ["", "not a date", "2026-13-45"])
def test_bad_timestamps_are_none_not_an_exception(raw):
    assert parse_timestamp(raw) is None


# -- the record itself -----------------------------------------------------


def make_provenance(**overrides: object) -> Provenance:
    fields: dict[str, Any] = {
        "title": "Ada Lovelace",
        "page_id": 974,
        "revision_id": 1371961179,
        "article_url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
        "permalink": "https://en.wikipedia.org/w/index.php?oldid=1371961179",
        "retrieved_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return Provenance(**fields)


def test_complete_record_can_support_a_citation():
    assert make_provenance().is_complete is True


@pytest.mark.parametrize("missing", [{"revision_id": 0}, {"page_id": 0}, {"title": ""}])
def test_incomplete_record_is_reported_as_such(missing):
    """A citation with no revision behind it is a bug, not a near miss (§5)."""
    assert make_provenance(**missing).is_complete is False


def test_citation_includes_the_section_when_scoped():
    assert "Ada Lovelace#Death" in make_provenance(section="Death").cite()
