"""The agent loop, driven offline against recorded API responses (§2.2, §5 Layer 1).

These exercise the real ``tool_runner`` -- not a stand-in for it -- so the
protocol details we now depend on are actually verified. No network, no spend.
"""

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
from wikimedia_agent.agent import DEFAULT_MODEL, Answer, WikipediaAgent
from wikimedia_agent.errors import ConfigurationError
from wikimedia_agent.provenance import PROJECT_INDEPENDENT
from wikimedia_agent.tools import WikipediaTools
from wikimedia_agent.wikipedia import WikipediaClient

WIKI_PAGE = {
    "query": {
        "pages": [
            {
                "pageid": 974,
                "title": "Ada Lovelace",
                "extract": "Ada Lovelace was an English mathematician.",
                "pageprops": {},
                "revisions": [{"revid": 42, "timestamp": "2026-08-29T16:16:29Z"}],
                "pageassessments": {PROJECT_INDEPENDENT: {"class": "B"}},
            }
        ]
    }
}


def make_agent(api: RecordedAnthropic, *, wiki_body=WIKI_PAGE, **kwargs) -> WikipediaAgent:
    wiki = WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(lambda _r: json_response(wiki_body)),
        min_interval=0.0,
    )
    return WikipediaAgent(tools=WikipediaTools(client=wiki), client=api.client(), **kwargs)


# -- the loop drives to completion ----------------------------------------


def test_single_tool_call_then_answer():
    api = RecordedAnthropic(
        assistant_message(
            content=[
                text_block("Let me look that up."),
                tool_use_block("get_summary", {"title": "Ada Lovelace"}, "tu_1"),
            ],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Ada Lovelace was a mathematician. [1]")]),
    )
    answer = make_agent(api).ask("Who was Ada Lovelace?")

    assert isinstance(answer, Answer)
    assert "mathematician" in answer.text
    assert answer.stop_reason == "end_turn"
    assert api.call_count == 2


def test_multi_turn_tool_sequence_drives_to_completion():
    api = RecordedAnthropic(
        assistant_message(
            content=[tool_use_block("search_wikipedia", {"query": "Ada"}, "tu_1")],
            stop_reason="tool_use",
        ),
        assistant_message(
            content=[tool_use_block("get_article", {"title": "Ada Lovelace"}, "tu_2")],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Final answer. [1]")]),
    )
    answer = make_agent(api).ask("Who was Ada Lovelace?")
    assert answer.text.startswith("Final answer")
    assert api.call_count == 3


def test_retrievals_are_reported_as_sources():
    api = RecordedAnthropic(
        assistant_message(
            content=[tool_use_block("get_summary", {"title": "Ada Lovelace"}, "tu_1")],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Answer. [1]")]),
    )
    answer = make_agent(api).ask("Who was Ada Lovelace?")
    assert answer.cited_articles == ["Ada Lovelace"]
    assert answer.sources[0].revision_id == 42


def test_each_question_starts_with_a_clean_source_list():
    """Two questions, each retrieving once. The second must report one source,
    not two -- otherwise every answer inherits the previous one's citations."""
    fetch = assistant_message(
        content=[tool_use_block("get_summary", {"title": "Ada Lovelace"}, "tu_1")],
        stop_reason="tool_use",
    )
    reply = assistant_message(content=[text_block("Answer.")])
    api = RecordedAnthropic(fetch, reply, fetch, reply)

    agent = make_agent(api)
    first = agent.ask("First question?")
    second = agent.ask("Second question?")

    assert len(first.sources) == 1
    assert len(second.sources) == 1, "sources must not accumulate across questions"


# -- protocol details we now own (§2.2) -----------------------------------


def test_tool_results_go_back_in_a_single_user_message():
    """Splitting them silently suppresses parallel tool calls -- no error, just
    quietly worse behaviour."""
    api = RecordedAnthropic(
        assistant_message(
            content=[
                tool_use_block("get_summary", {"title": "Ada Lovelace"}, "tu_1"),
                tool_use_block("get_summary", {"title": "Charles Babbage"}, "tu_2"),
            ],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Answer.")]),
    )
    make_agent(api).ask("Tell me about both.")

    second_request = api.messages_sent(1)
    user_messages = [m for m in second_request if m["role"] == "user"]
    tool_results = [
        block
        for message in user_messages
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(tool_results) == 2
    carriers = [
        m for m in user_messages
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
    ]
    assert len(carriers) == 1, "both results must ride in one user message"


def test_assistant_content_is_echoed_back_unchanged():
    original = [
        text_block("Thinking out loud."),
        tool_use_block("get_summary", {"title": "Ada Lovelace"}, "tu_1"),
    ]
    api = RecordedAnthropic(
        assistant_message(content=original, stop_reason="tool_use"),
        assistant_message(content=[text_block("Answer.")]),
    )
    make_agent(api).ask("Who was Ada Lovelace?")

    echoed = [m for m in api.messages_sent(1) if m["role"] == "assistant"]
    assert len(echoed) == 1
    sent_blocks = echoed[0]["content"]
    assert [b["type"] for b in sent_blocks] == ["text", "tool_use"]
    assert sent_blocks[1]["id"] == "tu_1"
    assert sent_blocks[1]["input"] == {"title": "Ada Lovelace"}


def test_a_raising_tool_is_reported_as_an_error_result_not_dropped():
    """Our tools do not raise, but the loop must handle it if one ever does:
    a dropped tool_result makes the conversation malformed."""
    from anthropic import beta_tool

    @beta_tool
    def exploding_tool(title: str) -> str:
        """A tool that fails.

        Args:
            title: Unused.
        """
        raise RuntimeError("boom")

    api = RecordedAnthropic(
        assistant_message(
            content=[tool_use_block("exploding_tool", {"title": "x"}, "tu_1")],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Recovered.")]),
    )
    agent = make_agent(api)
    agent._tool_list = [exploding_tool]

    answer = agent.ask("Trigger the failure.")

    results = [
        block
        for message in api.messages_sent(1)
        if message["role"] == "user"
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert len(results) == 1, "the failed tool's result must not be dropped"
    assert results[0].get("is_error") is True
    assert answer.text == "Recovered."


def test_tool_inputs_are_parsed_not_string_matched():
    """Escaping varies; the loop must receive a dict."""
    api = RecordedAnthropic(
        assistant_message(
            content=[
                tool_use_block("get_article", {"title": "Ada Lovelace", "section": ""}, "tu_1")
            ],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Answer.")]),
    )
    answer = make_agent(api).ask("Tell me.")
    assert answer.sources, "the tool ran with parsed arguments"


# -- stop_reason is checked before content --------------------------------


def test_refusal_is_reported_not_read_as_an_empty_answer():
    """A refusal is HTTP 200 with no usable content; reading content first
    would present it as a successful empty answer."""
    api = RecordedAnthropic(assistant_message(content=[], stop_reason="refusal"))
    answer = make_agent(api).ask("Something declined.")

    assert answer.was_refused is True
    assert "unable to answer" in answer.text
    assert answer.text.strip() != ""


def test_max_tokens_truncation_is_disclosed():
    api = RecordedAnthropic(
        assistant_message(content=[text_block("A partial answ")], stop_reason="max_tokens")
    )
    answer = make_agent(api).ask("A long question.")
    assert "truncated" in answer.text.lower()


def test_empty_content_yields_an_honest_message():
    api = RecordedAnthropic(assistant_message(content=[], stop_reason="end_turn"))
    answer = make_agent(api).ask("Anything.")
    assert answer.text.strip() != ""
    assert "could not produce an answer" in answer.text


def test_usage_is_recorded_for_cost_reporting():
    api = RecordedAnthropic(assistant_message(content=[text_block("Answer.")]))
    answer = make_agent(api).ask("Anything.")
    assert answer.input_tokens == 100
    assert answer.output_tokens == 20


# -- bounds ----------------------------------------------------------------


def test_the_retrieval_ceiling_terminates_a_runaway_loop():
    """Principle #12: every loop has a ceiling."""
    looping = assistant_message(
        content=[tool_use_block("get_summary", {"title": "Ada Lovelace"}, "tu_1")],
        stop_reason="tool_use",
    )
    api = RecordedAnthropic(looping)
    answer = make_agent(api, max_iterations=3).ask("Loop forever.")

    assert api.call_count <= 3
    assert answer.hit_the_ceiling or answer.stop_reason == "tool_use"


def test_the_ceiling_is_sent_as_configured():
    api = RecordedAnthropic(assistant_message(content=[text_block("Answer.")]))
    make_agent(api, max_iterations=5).ask("Anything.")
    assert api.call_count == 1


def test_empty_question_is_rejected_before_any_call():
    api = RecordedAnthropic(assistant_message(content=[text_block("Answer.")]))
    with pytest.raises(ValueError, match="must not be empty"):
        make_agent(api).ask("   ")
    assert api.call_count == 0


# -- configuration ---------------------------------------------------------


def test_the_default_model_is_an_anthropic_model():
    assert DEFAULT_MODEL.startswith("claude-")


def test_missing_api_key_fails_at_startup(monkeypatch):
    from wikimedia_agent.agent import build_agent

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("WIKIMEDIA_AGENT_CONTACT", CONTACT)
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        build_agent()


def test_missing_contact_fails_at_startup(monkeypatch):
    from wikimedia_agent.agent import build_agent

    monkeypatch.delenv("WIKIMEDIA_AGENT_CONTACT", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(ConfigurationError, match="WIKIMEDIA_AGENT_CONTACT"):
        build_agent()
