"""The command-line interface (§4, Phase 12).

Reachable both as ``wikimedia-agent`` and ``python -m wikimedia_agent``.
"""

from __future__ import annotations

import httpx
import pytest

import wikimedia_agent.cli as entry
from tests.helpers import (
    CONTACT,
    RecordedAnthropic,
    assistant_message,
    json_response,
    text_block,
    tool_use_block,
)
from wikimedia_agent.agent import WikipediaAgent
from wikimedia_agent.provenance import PROJECT_INDEPENDENT
from wikimedia_agent.tools import WikipediaTools
from wikimedia_agent.wikipedia import WikipediaClient


def wiki_body(grade="Start"):
    return {
        "query": {
            "pages": [
                {
                    "pageid": 7,
                    "title": "Gerald J. Ford",
                    "extract": "An American businessman.",
                    "pageprops": {},
                    "revisions": [{"revid": 99, "timestamp": "2026-01-01T00:00:00Z"}],
                    "pageassessments": {PROJECT_INDEPENDENT: {"class": grade}},
                }
            ]
        }
    }


def patched_agent(monkeypatch, api, grade="Start"):
    wiki = WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(lambda _r: json_response(wiki_body(grade))),
        min_interval=0.0,
    )
    agent = WikipediaAgent(tools=WikipediaTools(client=wiki), client=api.client())
    monkeypatch.setattr(entry, "build_agent", lambda **_kwargs: agent)
    return agent


def answering_api(grade_text="Answer. [1]"):
    return RecordedAnthropic(
        assistant_message(
            content=[tool_use_block("get_summary", {"title": "Gerald J. Ford"}, "t1")],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block(grade_text)]),
    )


def test_answer_and_sources_are_printed(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api())
    assert entry.main(["Who", "is", "Gerald", "J.", "Ford?"]) == 0

    out = capsys.readouterr().out
    assert "Answer. [1]" in out
    assert "Gerald J. Ford" in out
    assert "https://en.wikipedia.org/wiki/Gerald_J._Ford" in out
    assert "tokens" in out


def test_low_quality_sources_are_flagged(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api(), grade="Start")
    entry.main(["question"])
    out = capsys.readouterr().out
    assert "may be incomplete" in out
    assert "\u26a0" in out


def test_good_quality_sources_are_not_flagged(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api(), grade="GA")
    entry.main(["question"])
    out = capsys.readouterr().out
    assert "reviewed by Wikipedia editors" in out
    assert "\u26a0" not in out


def test_the_cli_prints_no_grade_jargon(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api(), grade="Start")
    entry.main(["question"])
    assert "-class" not in capsys.readouterr().out


def test_no_retrieval_is_stated_rather_than_left_blank(monkeypatch, capsys):
    api = RecordedAnthropic(assistant_message(content=[text_block("No lookup needed.")]))
    patched_agent(monkeypatch, api)
    entry.main(["question"])
    assert "No Wikipedia articles were used" in capsys.readouterr().out


def test_no_arguments_enters_the_interactive_loop(monkeypatch, capsys):
    """No arguments means the loop, not an error -- `--help` documents usage."""
    patched_agent(monkeypatch, answering_api())
    monkeypatch.setattr("builtins.input", lambda _p="": (_ for _ in ()).throw(EOFError))
    assert entry.main([]) == 0
    assert "wikimedia-agent" in capsys.readouterr().out


def test_configuration_error_is_reported_not_tracebacked(monkeypatch, capsys):
    from wikimedia_agent.errors import ConfigurationError

    def boom(**_kwargs):
        raise ConfigurationError("no contact configured")

    monkeypatch.setattr(entry, "build_agent", boom)
    assert entry.main(["question"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_runtime_failure_is_reported_not_tracebacked(monkeypatch, capsys):
    api = answering_api()
    agent = patched_agent(monkeypatch, api)

    def explode(_question):
        raise RuntimeError("upstream on fire")

    monkeypatch.setattr(agent, "ask", explode)
    assert entry.main(["question"]) == 1
    assert "Failed to answer" in capsys.readouterr().err


# -- interactive loop ------------------------------------------------------


def feed(monkeypatch, lines):
    """Feed scripted input to the loop's prompt."""
    remaining = list(lines)

    def fake_input(_prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def test_loop_answers_several_questions(monkeypatch, capsys):
    fetch = assistant_message(
        content=[tool_use_block("get_summary", {"title": "Gerald J. Ford"}, "t1")],
        stop_reason="tool_use",
    )
    api = RecordedAnthropic(
        fetch,
        assistant_message(content=[text_block("First answer. [1]")]),
        fetch,
        assistant_message(content=[text_block("Second answer. [1]")]),
    )
    patched_agent(monkeypatch, api)
    feed(monkeypatch, ["first question", "second question"])

    assert entry.main([]) == 0
    out = capsys.readouterr().out
    assert "First answer" in out
    assert "Second answer" in out


def test_the_banner_documents_the_session_commands(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api())
    feed(monkeypatch, [])
    entry.main([])
    out = capsys.readouterr().out
    assert "/new" in out
    assert "/exit" in out
    assert "Follow-up questions resolve" in out


def test_follow_ups_carry_earlier_turns(monkeypatch):
    """The loop is a conversation: turn two must see turn one (§2.5)."""
    fetch = assistant_message(
        content=[tool_use_block("get_summary", {"title": "Gerald J. Ford"}, "t1")],
        stop_reason="tool_use",
    )
    api = RecordedAnthropic(
        fetch, assistant_message(content=[text_block("A businessman. [[Gerald J. Ford]]")]),
        fetch, assistant_message(content=[text_block("More detail. [[Gerald J. Ford]]")]),
    )
    patched_agent(monkeypatch, api)
    feed(monkeypatch, ["Who is Gerald J. Ford?", "Tell me more about him"])
    entry.main([])

    contents = [
        message["content"]
        for message in api.messages_sent(2)
        if isinstance(message["content"], str)
    ]
    assert any("Who is Gerald J. Ford?" in c for c in contents)
    assert any("Tell me more about him" in c for c in contents)


@pytest.mark.parametrize("command", ["/new", "/reset", "/clear"])
def test_reset_starts_a_fresh_conversation(monkeypatch, capsys, command):
    api = RecordedAnthropic(
        assistant_message(content=[text_block("First.")]),
        assistant_message(content=[text_block("Second.")]),
    )
    patched_agent(monkeypatch, api)
    feed(monkeypatch, ["first question", command, "second question"])
    entry.main([])

    assert "fresh conversation" in capsys.readouterr().out
    contents = [
        message["content"]
        for message in api.messages_sent(1)
        if isinstance(message["content"], str)
    ]
    assert not any("first question" in c for c in contents)


@pytest.mark.parametrize("command", ["/exit", "/quit", "exit", "QUIT"])
def test_exit_commands_leave_cleanly(monkeypatch, capsys, command):
    patched_agent(monkeypatch, answering_api())
    feed(monkeypatch, [command, "never reached"])
    assert entry.main([]) == 0
    assert "Answer" not in capsys.readouterr().out


def test_blank_input_is_ignored(monkeypatch, capsys):
    api = RecordedAnthropic(
        assistant_message(
            content=[tool_use_block("get_summary", {"title": "Gerald J. Ford"}, "t1")],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("Answer. [1]")]),
    )
    patched_agent(monkeypatch, api)
    feed(monkeypatch, ["", "   ", "a real question"])
    assert entry.main([]) == 0
    assert api.call_count == 2, "blank lines must not cost an API call"


def test_end_of_input_exits(monkeypatch):
    patched_agent(monkeypatch, answering_api())
    feed(monkeypatch, [])
    assert entry.main([]) == 0


def test_a_failed_question_does_not_end_the_loop(monkeypatch, capsys):
    api = answering_api()
    agent = patched_agent(monkeypatch, api)

    calls = {"n": 0}
    original = agent.ask

    def flaky(question, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient upstream failure")
        return original(question, **kwargs)

    monkeypatch.setattr(agent, "ask", flaky)
    feed(monkeypatch, ["first", "second"])

    assert entry.main([]) == 0
    captured = capsys.readouterr()
    assert "Failed to answer" in captured.err
    assert "Answer. [1]" in captured.out


# -- /sources ---------------------------------------------------------------


def test_sources_command_lists_the_session_registry(monkeypatch, capsys):
    api = RecordedAnthropic(
        assistant_message(
            content=[tool_use_block("get_summary", {"title": "Gerald J. Ford"}, "t1")],
            stop_reason="tool_use",
        ),
        assistant_message(content=[text_block("An answer. [[Gerald J. Ford]]")]),
    )
    patched_agent(monkeypatch, api, grade="Start")
    feed(monkeypatch, ["a question", "/sources"])
    entry.main([])

    out = capsys.readouterr().out
    assert "Articles used in this conversation" in out
    assert "Gerald J. Ford" in out
    assert "may be incomplete" in out
    assert "-class" not in out


def test_sources_command_before_any_question(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api())
    feed(monkeypatch, ["/sources"])
    entry.main([])
    assert "No articles have been read" in capsys.readouterr().out


def test_sources_command_costs_no_api_call(monkeypatch):
    api = answering_api()
    patched_agent(monkeypatch, api)
    feed(monkeypatch, ["/sources"])
    entry.main([])
    assert api.call_count == 0


# -- argument parsing and configuration (Phase 12) ------------------------


def test_the_console_script_entry_point_is_importable():
    """pyproject declares `wikimedia-agent = wikimedia_agent.cli:main`.

    Regression: it was declared in Phase 0 and the module did not exist until
    Phase 12, so the installed command failed with ImportError.
    """
    from wikimedia_agent.cli import main as cli_main

    assert callable(cli_main)


def test_python_m_and_the_console_script_share_an_entry_point():
    import wikimedia_agent.__main__ as module
    from wikimedia_agent.cli import main as cli_main

    assert module.main is cli_main


def test_version_flag_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--version"])
    assert excinfo.value.code == 0
    assert "wikimedia-agent" in capsys.readouterr().out


def test_help_flag_exits_cleanly():
    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--help"])
    assert excinfo.value.code == 0


def test_retrieval_bounds_are_configurable(monkeypatch):
    agent = patched_agent(monkeypatch, answering_api())
    feed(monkeypatch, [])

    entry.main(["--max-retrievals", "3", "--deadline", "12"])

    assert agent.tools.budget.max_retrievals == 3
    assert agent.tools.budget.deadline_seconds == 12.0


def test_session_bounds_are_configurable(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api())

    captured = {}
    real_interactive = entry.interactive

    def spy(session):
        captured["max_turns"] = session.max_turns
        captured["token_budget"] = session.token_budget
        return real_interactive(session)

    monkeypatch.setattr(entry, "interactive", spy)
    feed(monkeypatch, [])
    entry.main(["--max-turns", "5", "--token-budget", "1234"])

    assert captured == {"max_turns": 5, "token_budget": 1234}


def test_a_question_argument_answers_once_without_a_session(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api())

    assert entry.main(["Who", "is", "Gerald", "J.", "Ford?"]) == 0
    assert "Answer. [1]" in capsys.readouterr().out


def test_help_command_lists_the_session_commands(monkeypatch, capsys):
    patched_agent(monkeypatch, answering_api())
    feed(monkeypatch, ["/help"])
    entry.main([])
    out = capsys.readouterr().out
    assert "/sources" in out
    assert "/new" in out


def test_the_interactive_prompt_is_an_angle_bracket(monkeypatch):
    """A familiar shell-style prompt rather than a question mark."""
    seen = []

    def capture(prompt=""):
        seen.append(prompt)
        raise EOFError

    patched_agent(monkeypatch, answering_api())
    monkeypatch.setattr("builtins.input", capture)
    entry.main([])

    assert seen == ["> "]
