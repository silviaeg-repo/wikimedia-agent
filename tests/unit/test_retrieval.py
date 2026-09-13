"""Retrieval methods and their bounds (§2.1)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers import CONTACT, json_response
from wikimedia_agent.errors import DisambiguationError, PageNotFound, WikipediaAPIError
from wikimedia_agent.wikipedia import (
    MAX_ARTICLE_CHARS,
    MAX_DISAMBIGUATION_OPTIONS,
    MAX_SEARCH_LIMIT,
    MAX_TITLES_PER_REQUEST,
    WikipediaClient,
)


def client_with(handler, clock, **kwargs):
    return WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        min_interval=0.0,
        **kwargs,
    )


def page(**fields):
    base = {"pageid": 1, "title": "Ada Lovelace", "extract": "Lead.", "pageprops": {}}
    base.update(fields)
    return {"query": {"pages": [base]}}


# -- search bounds ---------------------------------------------------------


def search_body(count=3):
    return {
        "query": {
            "search": [
                {"title": f"Result {i}", "pageid": i, "snippet": f"<span>hit</span> {i}",
                 "wordcount": 100 + i}
                for i in range(count)
            ]
        }
    }


def test_search_limit_defaults_to_five(clock):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return json_response(search_body())

    with client_with(handler, clock) as c:
        c.search("anything")
    assert seen["srlimit"] == "5"


@pytest.mark.parametrize("requested,expected", [(1, 1), (10, 10), (20, 20), (500, 20), (9999, 20)])
def test_search_limit_is_clamped_at_the_cap(clock, requested, expected):
    """The cap lives in the client, so no tool argument can raise it (principle #16)."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return json_response(search_body())

    with client_with(handler, clock) as c:
        c.search("anything", limit=requested)
    assert seen["srlimit"] == str(expected)
    assert int(seen["srlimit"]) <= MAX_SEARCH_LIMIT


def test_search_limit_floor_is_one(clock):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return json_response(search_body())

    with client_with(handler, clock) as c:
        c.search("anything", limit=0)
    assert seen["srlimit"] == "1"


def test_search_strips_html_from_snippets(clock):
    with client_with(lambda _r: json_response(search_body(1)), clock) as c:
        results = c.search("anything")
    assert "<span>" not in results[0].snippet
    assert results[0].snippet == "hit 0"


def test_empty_search_query_is_rejected_before_a_request(clock):
    calls = []

    def handler(request):
        calls.append(request)
        return json_response(search_body())

    with client_with(handler, clock) as c:
        with pytest.raises(ValueError, match="must not be empty"):
            c.search("   ")
    assert calls == []


# -- article scoping and truncation ---------------------------------------

ARTICLE = "Lead text.\n\n== Biography ==\nBorn.\n\n== Work ==\nDid things.\n"


def test_section_scoping_returns_only_that_section(clock):
    with client_with(lambda _r: json_response(page(extract=ARTICLE)), clock) as c:
        article = c.get_article("Ada Lovelace", section="Work")
    assert article.text == "Did things."
    assert article.section_title == "Work"
    assert "Born." not in article.text


def test_section_lookup_is_case_insensitive(clock):
    with client_with(lambda _r: json_response(page(extract=ARTICLE)), clock) as c:
        assert c.get_article("Ada Lovelace", section="wOrK").text == "Did things."


def test_unknown_section_reports_what_is_available(clock):
    with client_with(lambda _r: json_response(page(extract=ARTICLE)), clock) as c:
        with pytest.raises(WikipediaAPIError, match="Biography") as excinfo:
            c.get_article("Ada Lovelace", section="Nonexistent")
    assert excinfo.value.code == "nosuchsection"


def test_whole_article_is_truncated_at_the_budget(clock):
    long_extract = "x" * (MAX_ARTICLE_CHARS + 5_000)
    with client_with(lambda _r: json_response(page(extract=long_extract)), clock) as c:
        article = c.get_article("Ada Lovelace")
    assert len(article.text) <= MAX_ARTICLE_CHARS
    assert article.truncated is True


def test_short_article_is_not_marked_truncated(clock):
    with client_with(lambda _r: json_response(page(extract=ARTICLE)), clock) as c:
        assert c.get_article("Ada Lovelace").truncated is False


def test_sections_are_listed_even_when_scoped(clock):
    with client_with(lambda _r: json_response(page(extract=ARTICLE)), clock) as c:
        article = c.get_article("Ada Lovelace", section="Work")
    assert article.section_titles == ("Summary", "Biography", "Work")


# -- redirects, missing pages, disambiguation -----------------------------


def test_redirect_resolves_to_canonical_title(clock):
    body = page(title="Ada Lovelace")
    body["query"]["redirects"] = [{"from": "Ada Byron", "to": "Ada Lovelace"}]
    with client_with(lambda _r: json_response(body), clock) as c:
        summary = c.get_summary("Ada Byron")
    assert summary.title == "Ada Lovelace"
    assert summary.redirected_from == "Ada Byron"
    assert summary.requested_title == "Ada Byron"


def test_normalized_title_is_tracked_as_provenance(clock):
    body = page(title="Ada Lovelace")
    body["query"]["normalized"] = [{"from": "ada_lovelace", "to": "Ada Lovelace"}]
    with client_with(lambda _r: json_response(body), clock) as c:
        assert c.get_summary("ada_lovelace").redirected_from == "ada_lovelace"


def test_normalize_then_redirect_traces_to_the_original(clock):
    """A title that is normalized *and* redirected still traces back one step."""
    body = page(title="Ada Lovelace")
    body["query"]["normalized"] = [{"from": "ada_byron", "to": "Ada Byron"}]
    body["query"]["redirects"] = [{"from": "Ada Byron", "to": "Ada Lovelace"}]
    with client_with(lambda _r: json_response(body), clock) as c:
        assert c.get_summary("ada_byron").redirected_from == "ada_byron"


def test_missing_page_raises_page_not_found(clock):
    body = {"query": {"pages": [{"title": "Nope", "missing": True}]}}
    with client_with(lambda _r: json_response(body), clock) as c:
        with pytest.raises(PageNotFound, match="Nope"):
            c.get_article("Nope")


DISAMBIG_HTML = """
<div class="mw-parser-output">
<ul>
<li><a href="/wiki/Mercury_(planet)" title="x">Mercury (planet)</a>, the closest planet
to the Sun</li>
<li><a href="/wiki/Mercury_(element)">Mercury (element)</a>, a chemical element</li>
<li><a href="/wiki/Special:Search/intitle:Mercury">Search</a></li>
<li>No link in this item</li>
<li><a href="/wiki/Mercury_(planet)">Mercury (planet)</a>, a duplicate</li>
</ul></div>
"""


def disambig_handler(parse_response=None):
    """Serve a disambiguation page, then its parsed option list."""

    def handler(request):
        params = dict(request.url.params)
        if params.get("action") == "parse":
            if parse_response is not None:
                return parse_response
            return json_response({"parse": {"text": DISAMBIG_HTML}})
        return json_response(page(title="Mercury", pageprops={"disambiguation": ""}))

    return handler


def test_disambiguation_raises_with_described_options(clock):
    with client_with(disambig_handler(), clock) as c:
        with pytest.raises(DisambiguationError) as excinfo:
            c.get_article("Mercury")

    options = excinfo.value.options
    assert [o.title for o in options] == ["Mercury (planet)", "Mercury (element)"]
    assert options[0].description == "the closest planet to the Sun"
    assert options[1].description == "a chemical element"
    assert excinfo.value.titles == ["Mercury (planet)", "Mercury (element)"]


def test_disambiguation_options_preserve_page_order(clock):
    """Page order puts the likeliest candidates first.

    An alphabetical list (which `prop=links` would give) buries them: for
    "Mercury" it leads with "Anna Kavan".
    """
    with client_with(disambig_handler(), clock) as c:
        with pytest.raises(DisambiguationError) as excinfo:
            c.get_article("Mercury")
    assert excinfo.value.options[0].title == "Mercury (planet)"


def test_disambiguation_skips_namespaced_links_and_duplicates(clock):
    with client_with(disambig_handler(), clock) as c:
        with pytest.raises(DisambiguationError) as excinfo:
            c.get_article("Mercury")
    titles = excinfo.value.titles
    assert not any(t.startswith("Special:") for t in titles)
    assert len(titles) == len(set(titles))


def test_disambiguation_options_are_capped(clock):
    many = "".join(
        f'<li><a href="/wiki/Option_{i}">Option {i}</a>, number {i}</li>' for i in range(80)
    )
    response = json_response({"parse": {"text": f"<ul>{many}</ul>"}})
    with client_with(disambig_handler(response), clock) as c:
        with pytest.raises(DisambiguationError) as excinfo:
            c.get_article("Mercury")
    assert len(excinfo.value.options) == MAX_DISAMBIGUATION_OPTIONS


def test_disambiguation_without_options_still_raises(clock):
    """Options are best-effort; failing to fetch them must not mask the ambiguity."""
    with client_with(disambig_handler(json_response({}, status_code=404)), clock) as c:
        with pytest.raises(DisambiguationError) as excinfo:
            c.get_article("Mercury")
    assert excinfo.value.options == []
    assert "no candidates listed" in str(excinfo.value)


def test_summary_also_raises_on_disambiguation(clock):
    """Ambiguity must surface on every retrieval path, not just get_article."""
    with client_with(disambig_handler(), clock) as c:
        with pytest.raises(DisambiguationError):
            c.get_summary("Mercury")


# -- batching --------------------------------------------------------------


def test_batching_builds_a_pipe_separated_titles_parameter(clock):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return {"A": 1} and json_response({"query": {"pages": [
            {"pageid": 1, "title": "A", "extract": "a"},
            {"pageid": 2, "title": "B", "extract": "b"},
        ]}})

    with client_with(handler, clock) as c:
        summaries = c.get_summaries(["A", "B"])
    assert seen["titles"] == "A|B"
    assert set(summaries) == {"A", "B"}


def test_batching_is_capped_at_the_documented_limit(clock):
    calls = []

    def handler(request):
        calls.append(request)
        return json_response({"query": {"pages": []}})

    with client_with(handler, clock) as c:
        with pytest.raises(ValueError, match=str(MAX_TITLES_PER_REQUEST)):
            c.get_summaries([f"T{i}" for i in range(MAX_TITLES_PER_REQUEST + 1)])
    assert calls == [], "the cap must be enforced before any request"


def test_batch_skips_missing_pages_without_losing_the_rest(clock):
    body = {"query": {"pages": [
        {"pageid": 1, "title": "Good", "extract": "text"},
        {"title": "Gone", "missing": True},
        {"pageid": 3, "title": "Ambiguous", "pageprops": {"disambiguation": ""}},
    ]}}
    with client_with(lambda _r: json_response(body), clock) as c:
        results = c.get_summaries(["Good", "Gone", "Ambiguous"])
    assert set(results) == {"Good"}


def test_empty_batch_is_rejected(clock):
    with client_with(lambda _r: json_response({}), clock) as c:
        with pytest.raises(ValueError, match="at least one title"):
            c.get_summaries([])
