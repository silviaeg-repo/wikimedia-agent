"""Multi-turn conversation (§2.5, Phase 8)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers import (
    CONTACT,
    RecordedAnthropic,
    assistant_message,
    json_response,
    text_block,
    tool_use_block,
)
from wikimedia_agent.agent import WikipediaAgent
from wikimedia_agent.provenance import PROJECT_INDEPENDENT, Grade
from wikimedia_agent.session import Session
from wikimedia_agent.tools import WikipediaTools
from wikimedia_agent.wikipedia import WikipediaClient


def page(title, page_id, grade="B", revid=None):
    return {
        "pageid": page_id,
        "title": title,
        "extract": f"About {title}.",
        "pageprops": {},
        "revisions": [{"revid": revid or page_id * 10, "timestamp": "2026-01-01T00:00:00Z"}],
        "pageassessments": {PROJECT_INDEPENDENT: {"class": grade}},
    }


PAGES = {
    "Benjamin Franklin": page("Benjamin Franklin", 1, "GA"),
    "Boston": page("Boston", 2, "B"),
    "Gerald J. Ford": page("Gerald J. Ford", 3, "Start"),
}


def wiki_handler(request):
    titles = dict(request.url.params).get("titles", "")
    wanted = titles.split("|")[0]
    return json_response({"query": {"pages": [PAGES.get(wanted, page(wanted, 99))]}})


def make_session(api: RecordedAnthropic, **kwargs) -> Session:
    wiki = WikipediaClient(
        contact=CONTACT, transport=httpx.MockTransport(wiki_handler), min_interval=0.0
    )
    agent = WikipediaAgent(tools=WikipediaTools(client=wiki), client=api.client())
    return Session(agent=agent, **kwargs)


def fetch(title, tool_id="t1"):
    return assistant_message(
        content=[tool_use_block("get_summary", {"title": title}, tool_id)],
        stop_reason="tool_use",
    )


def reply(text):
    return assistant_message(content=[text_block(text)])


# -- history carries between turns ----------------------------------------


def test_the_second_turn_sees_the_first():
    """"Where was he born?" needs the earlier turn to resolve "he"."""
    api = RecordedAnthropic(
        fetch("Benjamin Franklin"), reply("A founding father. [[Benjamin Franklin]]"),
        fetch("Boston"), reply("He was born in Boston. [[Boston]]"),
    )
    session = make_session(api)
    session.ask("Who was Ben Franklin?")
    session.ask("Where was he born?")

    # The second exchange's request must carry the first exchange.
    second_run = api.messages_sent(2)
    contents = [m["content"] for m in second_run if isinstance(m["content"], str)]
    assert any("Who was Ben Franklin?" in c for c in contents)
    assert any("founding father" in c for c in contents)
    assert any("Where was he born?" in c for c in contents)


def test_history_alternates_user_and_assistant():
    api = RecordedAnthropic(fetch("Benjamin Franklin"), reply("An answer."))
    session = make_session(api)
    session.ask("A question?")
    assert [m["role"] for m in session.history] == ["user", "assistant"]


def test_an_empty_answer_still_records_a_turn():
    """A dropped turn is a pronoun with no referent."""
    api = RecordedAnthropic(assistant_message(content=[], stop_reason="end_turn"))
    session = make_session(api)
    session.ask("A question?")
    assert len(session.history) == 2
    assert session.history[1]["content"]


# -- the article registry --------------------------------------------------


def test_articles_persist_across_turns():
    api = RecordedAnthropic(
        fetch("Benjamin Franklin"), reply("A. [[Benjamin Franklin]]"),
        fetch("Boston"), reply("B. [[Boston]]"),
    )
    session = make_session(api)
    session.ask("Who was Ben Franklin?")
    session.ask("Where was he born?")

    titles = [article.provenance.title for article in session.known_articles]
    assert titles == ["Benjamin Franklin", "Boston"]


def test_markers_stay_stable_as_articles_accumulate():
    """[1] in turn one must still be [1] in turn three."""
    api = RecordedAnthropic(
        fetch("Benjamin Franklin"), reply("A. [[Benjamin Franklin]]"),
        fetch("Boston"), reply("B. [[Boston]]"),
        fetch("Benjamin Franklin", "t3"), reply("C. [[Benjamin Franklin]]"),
    )
    session = make_session(api)
    session.ask("Who was Ben Franklin?")
    assert session.marker_for(1) == 1

    session.ask("Where was he born?")
    assert session.marker_for(1) == 1
    assert session.marker_for(2) == 2

    third = session.ask("Tell me more about him.")
    assert session.marker_for(1) == 1, "an article must keep its number"
    assert "[1]" in third.display_text


def test_a_new_article_takes_the_next_free_number():
    api = RecordedAnthropic(
        fetch("Benjamin Franklin"), reply("A. [[Benjamin Franklin]]"),
        fetch("Boston"), reply("B. [[Boston]]"),
    )
    session = make_session(api)
    session.ask("one")
    session.ask("two")
    assert sorted(a.marker for a in session.known_articles) == [1, 2]


def test_a_quality_flag_does_not_decay_over_turns():
    """A Start-class source cited in turn one is still flagged in turn three."""
    api = RecordedAnthropic(
        fetch("Gerald J. Ford"), reply("A. [[Gerald J. Ford]]"),
        reply("An answer with no retrieval."),
        fetch("Gerald J. Ford", "t3"), reply("C. [[Gerald J. Ford]]"),
    )
    session = make_session(api)
    first = session.ask("Who is Gerald J. Ford?")
    assert "⚠" in first.display_text

    session.ask("Interesting.")
    third = session.ask("Tell me more.")

    assert "⚠" in third.display_text
    assert session.articles[3].grade is Grade.START
    assert session.poor_quality_articles


def test_the_grade_is_fixed_on_first_sight():
    """Re-derivation is what would let a warning decay; there is none."""
    api = RecordedAnthropic(
        fetch("Gerald J. Ford"), reply("A. [[Gerald J. Ford]]"),
        fetch("Gerald J. Ford", "t2"), reply("B. [[Gerald J. Ford]]"),
    )
    session = make_session(api)
    session.ask("one")
    first_grade = session.articles[3].grade
    session.ask("two")
    assert session.articles[3].grade is first_grade


def test_the_first_turn_an_article_appeared_is_recorded():
    api = RecordedAnthropic(
        reply("No retrieval."),
        fetch("Boston"), reply("B. [[Boston]]"),
    )
    session = make_session(api)
    session.ask("one")
    session.ask("two")
    assert session.articles[2].first_turn == 2


def test_sections_are_accumulated_per_article():
    api = RecordedAnthropic(
        assistant_message(
            content=[tool_use_block(
                "get_article", {"title": "Benjamin Franklin", "section": ""}, "t1"
            )],
            stop_reason="tool_use",
        ),
        reply("A. [[Benjamin Franklin]]"),
    )
    session = make_session(api)
    session.ask("one")
    assert 1 in session.articles


# -- bounds ----------------------------------------------------------------


def test_the_turn_ceiling_drops_the_oldest_exchanges():
    api = RecordedAnthropic(reply("An answer."))
    session = make_session(api, max_turns=2)
    for index in range(4):
        session.ask(f"question {index}")

    assert len(session.history) == 4
    assert session.evicted_turns == 2


def test_eviction_is_reported_rather_than_silent():
    api = RecordedAnthropic(reply("An answer."))
    session = make_session(api, max_turns=1)
    session.ask("one")
    session.ask("two")
    note = session.history_note
    assert note is not None
    assert "no longer in context" in note


def test_no_eviction_note_before_anything_is_dropped():
    api = RecordedAnthropic(reply("An answer."))
    session = make_session(api, max_turns=5)
    session.ask("one")
    assert session.history_note is None


def test_markers_survive_history_eviction():
    """Registry metadata is tiny and always kept, so a citation stays correct
    even after its turn has been dropped from history."""
    api = RecordedAnthropic(
        fetch("Benjamin Franklin"), reply("A. [[Benjamin Franklin]]"),
        reply("B."), reply("C."), reply("D."),
    )
    session = make_session(api, max_turns=1)
    session.ask("one")
    for _ in range(3):
        session.ask("more")

    assert session.evicted_turns >= 1
    assert session.marker_for(1) == 1


# -- reset -----------------------------------------------------------------


def test_reset_clears_history_registry_and_numbering():
    api = RecordedAnthropic(
        fetch("Benjamin Franklin"), reply("A. [[Benjamin Franklin]]"),
        fetch("Boston"), reply("B. [[Boston]]"),
    )
    session = make_session(api)
    session.ask("one")
    session.reset()

    assert session.history == []
    assert session.known_articles == []
    assert session.turn_index == 0
    assert session.evicted_turns == 0

    session.ask("two")
    assert session.marker_for(2) == 1, "numbering restarts after a reset"


def test_a_reset_session_does_not_leak_earlier_context():
    api = RecordedAnthropic(reply("A."), reply("B."))
    session = make_session(api)
    session.ask("first question")
    session.reset()
    session.ask("second question")

    contents = [m["content"] for m in api.messages_sent(1) if isinstance(m["content"], str)]
    assert not any("first question" in c for c in contents)


# -- independence from single-shot use ------------------------------------


def test_the_agent_still_works_without_a_session():
    api = RecordedAnthropic(fetch("Benjamin Franklin"), reply("A. [[Benjamin Franklin]]"))
    wiki = WikipediaClient(
        contact=CONTACT, transport=httpx.MockTransport(wiki_handler), min_interval=0.0
    )
    agent = WikipediaAgent(tools=WikipediaTools(client=wiki), client=api.client())
    answer = agent.ask("Who was Ben Franklin?")
    assert "[1]" in answer.display_text


@pytest.mark.parametrize("question", ["", "   "])
def test_an_empty_question_is_rejected(question):
    api = RecordedAnthropic(reply("A."))
    session = make_session(api)
    with pytest.raises(ValueError):
        session.ask(question)
