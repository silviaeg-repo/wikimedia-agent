"""Retrieval bounds and the clarifying-question turn (§2.1, §2.4, Phase 10)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers import (
    CONTACT,
    FakeClock,
    RecordedAnthropic,
    assistant_message,
    json_response,
    text_block,
    tool_use_block,
)
from wikimedia_agent.agent import WikipediaAgent
from wikimedia_agent.provenance import PROJECT_INDEPENDENT
from wikimedia_agent.tools import (
    DEFAULT_MAX_RETRIEVALS,
    BudgetExhausted,
    RetrievalBudget,
    WikipediaTools,
)
from wikimedia_agent.wikipedia import WikipediaClient

DISAMBIG_HTML = (
    '<ul>'
    '<li><a href="/wiki/Mercury_(planet)">Mercury (planet)</a>, the closest planet'
    ' to the Sun</li>'
    '<li><a href="/wiki/Mercury_(element)">Mercury (element)</a>, a chemical element</li>'
    '<li><a href="/wiki/Mercury_(mythology)">Mercury (mythology)</a>, a Roman deity</li>'
    '</ul>'
)


def page(title="Ada Lovelace", page_id=974, grade="B", disambiguation=False):
    entry = {
        "pageid": page_id,
        "title": title,
        "extract": "" if disambiguation else f"About {title}.",
        "pageprops": {"disambiguation": ""} if disambiguation else {},
        "revisions": [{"revid": page_id * 10, "timestamp": "2026-01-01T00:00:00Z"}],
        "pageassessments": {PROJECT_INDEPENDENT: {"class": grade}},
    }
    return {"query": {"pages": [entry]}}


def ambiguous_handler(request):
    """Serve a disambiguation page, and its parsed candidate list."""
    params = dict(request.url.params)
    if params.get("action") == "parse":
        return json_response({"parse": {"text": DISAMBIG_HTML}})
    titles = params.get("titles", "")
    if titles.startswith("Mercury") and "(" not in titles:
        return json_response(page("Mercury", 1, disambiguation=True))
    return json_response(page(titles.split("|")[0], 2))


def tools_with(handler, **kwargs):
    client = WikipediaClient(
        contact=CONTACT, transport=httpx.MockTransport(handler), min_interval=0.0
    )
    return WikipediaTools(client=client, **kwargs)


# -- the clarifying-question turn (§2.4) ----------------------------------


def test_an_ambiguous_title_produces_a_question_not_an_error():
    tools = tools_with(ambiguous_handler)
    result = tools.article("Mercury")

    assert "AMBIGUOUS TITLE" in result
    assert "RETRIEVAL FAILED" not in result
    assert "ask the user" in result


def test_the_candidate_descriptions_reach_the_agent():
    """A clarifying question is only as useful as the options in it."""
    tools = tools_with(ambiguous_handler)
    result = tools.article("Mercury")

    assert "Mercury (planet) — the closest planet to the Sun" in result
    assert "Mercury (element) — a chemical element" in result
    assert "Mercury (mythology) — a Roman deity" in result


def test_a_clarification_is_recorded_as_such():
    """Structural, unlike the wording of the question (principle #16)."""
    tools = tools_with(ambiguous_handler)
    tools.article("Mercury")
    assert tools.clarifications == ["Mercury"]


def test_a_clarifying_question_costs_no_retrieval_budget():
    """Asking which subject was meant must not consume the budget for
    answering it (§2.4)."""
    tools = tools_with(ambiguous_handler)
    tools.article("Mercury")
    tools.summary("Mercury")

    assert tools.budget.used == 0
    assert tools.budget.remaining == DEFAULT_MAX_RETRIEVALS


def test_a_disambiguation_page_is_never_recorded_as_a_source():
    """It contains no content to ground an answer in."""
    tools = tools_with(ambiguous_handler)
    tools.article("Mercury")
    assert tools.retrievals == []


def test_resolving_to_a_concrete_title_retrieves_normally():
    """The turn after the user answers: a real article, a real retrieval."""
    tools = tools_with(ambiguous_handler)
    tools.article("Mercury")
    result = tools.article("Mercury (planet)")

    assert "wikipedia-article" in result
    assert [p.title for p in tools.retrievals] == ["Mercury (planet)"]
    assert tools.budget.used == 1


# -- the retrieval budget --------------------------------------------------


def test_the_budget_stops_retrieval_once_spent():
    tools = tools_with(lambda _r: json_response(page()), budget=RetrievalBudget(max_retrievals=2))
    tools.budget.start()

    assert "wikipedia-summary" in tools.summary("A")
    assert "wikipedia-summary" in tools.summary("B")
    third = tools.summary("C")

    assert "RETRIEVAL BUDGET REACHED" in third
    assert "Answer from what you have already read" in third


def test_an_exhausted_budget_issues_no_request():
    calls = []

    def handler(request):
        calls.append(request)
        return json_response(page())

    tools = tools_with(handler, budget=RetrievalBudget(max_retrievals=1))
    tools.budget.start()
    tools.summary("A")
    before = len(calls)
    tools.summary("B")
    assert len(calls) == before, "an exhausted budget must cost nothing further"


def test_the_budget_forbids_answering_from_memory():
    tools = tools_with(lambda _r: json_response(page()), budget=RetrievalBudget(max_retrievals=0))
    tools.budget.start()
    assert "rather than answering from memory" in tools.summary("A")


def test_failed_retrievals_do_not_consume_the_budget():
    """Only successful content counts: a missing page should not cost a slot."""
    missing = {"query": {"pages": [{"title": "Nope", "missing": True}]}}
    tools = tools_with(lambda _r: json_response(missing))
    tools.budget.start()
    tools.summary("Nope")
    assert tools.budget.used == 0


# -- the per-question deadline (§2.1) -------------------------------------


def test_the_deadline_stops_retrieval():
    """Backoff and retries must not compound into an unbounded wait."""
    clock = FakeClock()
    tools = tools_with(
        lambda _r: json_response(page()),
        budget=RetrievalBudget(deadline_seconds=10.0, monotonic=clock.monotonic),
    )
    tools.budget.start()
    assert "wikipedia-summary" in tools.summary("A")

    clock.advance(11.0)
    result = tools.summary("B")
    assert "RETRIEVAL BUDGET REACHED" in result
    assert "time limit" in result


def test_the_deadline_is_reported_with_numbers():
    clock = FakeClock()
    budget = RetrievalBudget(deadline_seconds=30.0, monotonic=clock.monotonic)
    budget.start()
    clock.advance(45.0)
    with pytest.raises(BudgetExhausted, match="45s of 30s"):
        budget.check()


def test_a_fresh_budget_has_no_elapsed_time():
    budget = RetrievalBudget()
    assert budget.elapsed == 0.0
    budget.check()


# -- bounds are per question, not per session -----------------------------


def wiki_handler(request):
    return json_response(page(dict(request.url.params).get("titles", "X").split("|")[0], 5))


def make_agent(api):
    client = WikipediaClient(
        contact=CONTACT, transport=httpx.MockTransport(wiki_handler), min_interval=0.0
    )
    return WikipediaAgent(tools=WikipediaTools(client=client), client=api.client())


def test_each_question_starts_with_a_full_budget():
    """A long conversation must not starve its later turns of retrieval."""
    fetch = assistant_message(
        content=[tool_use_block("get_summary", {"title": "A"}, "t1")], stop_reason="tool_use"
    )
    reply = assistant_message(content=[text_block("An answer. [[A]]")])
    api = RecordedAnthropic(fetch, reply, fetch, reply)

    agent = make_agent(api)
    agent.ask("first")
    assert agent.tools.budget.used == 1
    agent.ask("second")
    assert agent.tools.budget.used == 1, "the budget must reset per question"


def test_clarifications_are_reported_on_the_answer():
    api = RecordedAnthropic(
        assistant_message(content=[text_block("Which did you mean?")]),
    )
    agent = make_agent(api)
    answer = agent.ask("Tell me about Mercury.")
    assert answer.asked_for_clarification is False

    agent.tools.clarifications.append("Mercury")
    answer.clarifications = list(agent.tools.clarifications)
    assert answer.asked_for_clarification is True
