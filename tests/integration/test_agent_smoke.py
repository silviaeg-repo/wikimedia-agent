"""Phase 5 acceptance check (§5, Layer 3): the first paid call.

One question, end to end, against the live Anthropic and Wikipedia APIs. Costs
roughly a cent. Marked ``eval`` so it is excluded from the default ``pytest``
run and from CI -- paid calls are a deliberate act (principle #15).

Run it explicitly::

    ANTHROPIC_API_KEY=sk-... WIKIMEDIA_AGENT_CONTACT=you@example.org \\
        pytest -m eval -s
"""

from __future__ import annotations

import os

import pytest

from wikimedia_agent.agent import build_agent

pytestmark = pytest.mark.eval

HAVE_KEYS = bool(
    os.environ.get("ANTHROPIC_API_KEY", "").strip()
    and os.environ.get("WIKIMEDIA_AGENT_CONTACT", "").strip()
)

requires_keys = pytest.mark.skipif(
    not HAVE_KEYS,
    reason="Set ANTHROPIC_API_KEY and WIKIMEDIA_AGENT_CONTACT to run the paid smoke check.",
)


@requires_keys
def test_answers_a_single_hop_question_with_a_citation():
    agent = build_agent()
    answer = agent.ask("Who was Ada Lovelace, and what is she known for?")

    print("\n--- answer ---")
    print(answer.text)
    print("--- sources ---")
    for provenance in answer.sources:
        grade = answer.grades.get(provenance.page_id)
        label = grade.label if grade else "?"
        print(f"  {provenance.title} [{label}] rev {provenance.revision_id}")
    print(f"--- tokens: {answer.input_tokens} in / {answer.output_tokens} out ---")

    assert answer.was_refused is False
    assert answer.text.strip()
    # Grounded: it actually retrieved something, and said something about it.
    assert answer.sources, "the agent must retrieve before answering"
    assert any("Lovelace" in p.title for p in answer.sources)
    assert "[1]" in answer.text, "the answer should carry numbered citation markers"


@requires_keys
def test_declines_when_wikipedia_cannot_support_an_answer():
    """Honest refusal, rather than an invented answer."""
    agent = build_agent()
    answer = agent.ask(
        "According to Wikipedia, what did my next-door neighbour eat for breakfast?"
    )

    print("\n--- refusal answer ---")
    print(answer.text)

    lowered = answer.text.lower()
    assert any(
        phrase in lowered
        for phrase in ("could not find", "couldn't find", "does not", "doesn't",
                       "no information", "not covered", "unable")
    ), f"expected an honest refusal, got: {answer.text[:200]}"
