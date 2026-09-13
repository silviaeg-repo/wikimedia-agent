"""Deterministic scorers and the judge contract (§5, Phase 6).

Every test here runs offline and free -- which is the point: only answer
correctness needs a paid call, and even that is contract-tested with a fake.
"""

from __future__ import annotations

import json

import pytest
from evals.judge import (
    JUDGE_MODEL,
    RUBRIC_VERSION,
    Judge,
    JudgeError,
    build_package,
    parse_verdicts,
)
from evals.models import Transcript, TurnRecord
from evals.scorers import (
    deterministic_signals,
    score_asks_for_clarification,
    score_citation_validity,
    score_does_not_ask,
    score_provenance_integrity,
    score_refusal,
    score_retrieved_before_answering,
    score_source_disclosure,
)


def retrieval(title="Ada Lovelace", revision_id=42, grade="B", poor=False, section=None):
    return {
        "title": title,
        "section": section,
        "revision_id": revision_id,
        "article_url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
        "grade": grade,
        "poor_quality": poor,
    }


def transcript(answer_text, *, retrieved=None, tool_calls=None, category="single-hop"):
    turn = TurnRecord(
        question="Who was Ada Lovelace?",
        answer_text=answer_text,
        tool_calls=tool_calls if tool_calls is not None else [{"name": "get_summary"}],
        retrieved=list(retrieved) if retrieved is not None else [retrieval()],
        stop_reason="end_turn",
        input_tokens=100,
        output_tokens=20,
    )
    return Transcript(entry_id="t1", category=category, turns=[turn])


# -- grounding -------------------------------------------------------------


def test_answering_without_retrieving_fails_grounding():
    result = score_retrieved_before_answering(
        transcript("Ada was a mathematician.", retrieved=[], tool_calls=[])
    )
    assert result.passed is False
    assert "no retrieval" in result.detail


def test_retrieving_first_passes_grounding():
    assert score_retrieved_before_answering(transcript("Answer. [1]")).passed is True


# -- citation validity -----------------------------------------------------


def test_a_resolvable_marker_passes():
    assert score_citation_validity(transcript("Ada was a mathematician. [1]")).passed is True


def test_a_dangling_marker_fails():
    """A marker pointing at nothing is a fabricated citation."""
    result = score_citation_validity(transcript("Claim one. [1] Claim two. [7]"))
    assert result.passed is False
    assert "[7]" in result.detail or "7" in result.detail


def test_citing_with_nothing_retrieved_fails():
    result = score_citation_validity(
        transcript("Ada was a mathematician. [1]", retrieved=[], tool_calls=[])
    )
    assert result.passed is False
    assert "nothing retrieved" in result.detail


def test_retrieving_but_citing_nothing_fails():
    result = score_citation_validity(transcript("Ada was a mathematician."))
    assert result.passed is False
    assert "cites none" in result.detail


def test_no_citations_and_no_retrieval_is_consistent():
    result = score_citation_validity(
        transcript("I could not find this.", retrieved=[], tool_calls=[])
    )
    assert result.passed is True


# -- provenance integrity --------------------------------------------------


def test_missing_revision_fails_provenance():
    """A citation with no revision behind it is a bug, not a near miss."""
    result = score_provenance_integrity(
        transcript("Answer. [1]", retrieved=[retrieval(revision_id=0)])
    )
    assert result.passed is False
    assert "no revision_id" in result.detail


def test_complete_provenance_passes():
    assert score_provenance_integrity(transcript("Answer. [1]")).passed is True


# -- source disclosure -----------------------------------------------------


def test_an_undisclosed_article_fails():
    result = score_source_disclosure(
        transcript(
            "Ada was a mathematician. [1]\n\n[1] Ada Lovelace",
            retrieved=[retrieval(), retrieval(title="Charles Babbage")],
        )
    )
    assert result.passed is False
    assert "Charles Babbage" in result.detail


def test_naming_every_article_passes():
    result = score_source_disclosure(
        transcript("Answer. [1]\n\n[1] Ada Lovelace", retrieved=[retrieval()])
    )
    assert result.passed is True


# -- refusal and clarification --------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["I could not find this in Wikipedia.", "Wikipedia does not appear to cover this.",
     "I was unable to find any information."],
)
def test_honest_refusals_pass(text):
    assert score_refusal(transcript(text, retrieved=[], tool_calls=[])).passed is True


def test_an_invented_answer_fails_refusal():
    result = score_refusal(transcript("Your neighbour had toast. [1]"))
    assert result.passed is False


def test_asking_which_subject_passes():
    result = score_asks_for_clarification(
        transcript("Mercury could mean several things. Which did you mean?")
    )
    assert result.passed is True


def test_answering_an_ambiguous_question_fails_to_ask():
    result = score_asks_for_clarification(transcript("Mercury is the closest planet. [1]"))
    assert result.passed is False


def test_a_needless_clarifying_question_fails_the_control():
    """An agent that asks about everything is as useless as one that guesses."""
    result = score_does_not_ask(transcript("Did you mean the mathematician or someone else?"))
    assert result.passed is False


def test_answering_directly_passes_the_control():
    assert score_does_not_ask(transcript("Ada Lovelace was a mathematician. [1]")).passed is True


# -- the signals handed to the judge --------------------------------------


def test_signals_report_what_code_already_knows():
    signals = deterministic_signals(
        transcript(
            "Answer. [1]",
            retrieved=[retrieval(), retrieval(title="Stub Article", grade="Stub", poor=True)],
        )
    )
    assert signals["searched_wikipedia"] is True
    assert signals["retrieval_count"] == 2
    assert signals["citation_markers_used"] == [1]
    assert signals["poor_quality_sources_used"] == ["Stub Article"]
    assert "get_summary" in signals["tool_calls_made"]


def test_signals_report_answering_without_retrieval():
    signals = deterministic_signals(transcript("From memory.", retrieved=[], tool_calls=[]))
    assert signals["searched_wikipedia"] is False
    assert signals["retrieval_count"] == 0


# -- the judge package -----------------------------------------------------


def package_for(criteria=("answer_correctness",)):
    return build_package(
        context=["Who was Ada Lovelace?"],
        transcript=transcript("Answer. [1]").to_dict(),
        reference_answer="An English mathematician.",
        expected_articles=["Ada Lovelace"],
        signals=deterministic_signals(transcript("Answer. [1]")),
        criteria=list(criteria),
    )


def test_the_package_carries_every_required_part():
    package = package_for()
    for section in ("## CONTEXT", "## TRANSCRIPT", "## REFERENCE", "## SIGNALS", "## CRITERIA"):
        assert section in package


def test_the_package_marks_signals_as_established_facts():
    """The judge must not re-derive what the trace already proves."""
    assert "do not re-derive" in package_for()


def test_the_package_names_only_the_enabled_criteria():
    package = package_for(("answer_correctness",))
    assert "answer_correctness" in package
    assert "reading_named" not in package


def test_unjudgeable_criteria_are_rejected():
    with pytest.raises(JudgeError, match="not judgeable"):
        build_package(
            context=["q"], transcript={}, reference_answer="", expected_articles=[],
            signals={}, criteria=["citation_validity"],
        )


# -- judge output parsing --------------------------------------------------


def test_well_formed_verdicts_parse():
    raw = json.dumps({"verdicts": [
        {"criterion": "answer_correctness", "passed": True, "confidence": "high",
         "reason": "Matches the reference."}
    ]})
    verdicts = parse_verdicts(raw, ["answer_correctness"])
    assert verdicts[0].passed is True
    assert verdicts[0].confidence == "high"


def test_verdicts_wrapped_in_prose_still_parse():
    raw = 'Here is my assessment:\n{"verdicts": [{"criterion": "answer_correctness", ' \
          '"passed": false, "reason": "Wrong date."}]}\nHope that helps.'
    assert parse_verdicts(raw, ["answer_correctness"])[0].passed is False


@pytest.mark.parametrize("raw", ["", "no json here", "{broken", '{"verdicts": "not a list"}'])
def test_malformed_judge_output_fails_loudly(raw):
    """A malformed reply must not silently score zero -- that looks like an
    agent regression when it is a judge fault."""
    with pytest.raises(JudgeError):
        parse_verdicts(raw, ["answer_correctness"])


def test_an_omitted_verdict_fails_loudly():
    raw = json.dumps({"verdicts": []})
    with pytest.raises(JudgeError, match="omitted verdicts"):
        parse_verdicts(raw, ["answer_correctness"])


# -- judge versioning and bounds ------------------------------------------


def test_the_judge_version_pins_model_rubric_and_effort():
    judge = Judge(client=None)  # type: ignore[arg-type]
    assert JUDGE_MODEL in judge.version
    assert RUBRIC_VERSION in judge.version
    assert "effort=low" in judge.version


def test_the_judge_is_a_different_model_from_the_agent():
    """Sonnet 5 grading Opus 5: not marking its own tier's homework."""
    from wikimedia_agent.agent import DEFAULT_MODEL

    assert JUDGE_MODEL != DEFAULT_MODEL
    assert JUDGE_MODEL.startswith("claude-")


def test_an_oversized_package_fails_rather_than_truncating():
    """Grading a partial transcript is a wrong score presented as a right one."""
    judge = Judge(client=None, context_limit=100)  # type: ignore[arg-type]
    with pytest.raises(JudgeError, match="Refusing to truncate"):
        judge.judge("x" * 100_000, ["answer_correctness"])


# -- judge usage is recorded, not discarded -------------------------------


class FakeJudgeClient:
    """A stand-in Anthropic client returning one scripted judgement."""

    def __init__(self, input_tokens=5000, output_tokens=150):
        self.messages = self
        self._input = input_tokens
        self._output = output_tokens

    def create(self, **_kwargs):
        import types

        block = types.SimpleNamespace(type="text", text=json.dumps({
            "verdicts": [{"criterion": "answer_correctness", "passed": True,
                          "confidence": "high", "reason": "Matches."}]
        }))
        return types.SimpleNamespace(
            content=[block],
            stop_reason="end_turn",
            usage=types.SimpleNamespace(
                input_tokens=self._input, output_tokens=self._output
            ),
        )


def test_judging_returns_its_token_usage():
    """A run reporting $0.00 for a judge that plainly ran is an under-reported
    bill -- §5 requires spend to be observed, not discovered later."""
    judge = Judge(client=FakeJudgeClient())  # type: ignore[arg-type]
    result = judge.judge(package_for(), ["answer_correctness"])

    assert result.verdicts[0].passed is True
    assert result.input_tokens == 5000
    assert result.output_tokens == 150


def test_a_judged_run_records_nonzero_judge_cost():
    """Regression: judge usage was defined on the tracker but never recorded,
    so reports showed judge spend as zero."""
    from evals.cost import CostTracker
    from evals.models import EvalEntry
    from evals.run import score_with_judge

    tracker = CostTracker(agent_model="claude-opus-5", judge_model=JUDGE_MODEL)
    entry = EvalEntry(
        id="x",
        category="single-hop",
        turns=("Who was Ada Lovelace?",),
        reference_answer="An English mathematician.",
        criteria=("answer_correctness",),
    )

    scores = score_with_judge(
        Judge(client=FakeJudgeClient()),  # type: ignore[arg-type]
        entry,
        transcript("Answer. [1]"),
        tracker,
    )

    assert [score.criterion for score in scores] == ["answer_correctness"]
    assert tracker.judge_input == 5000
    assert tracker.judge_output == 150
    assert tracker.judge_cost > 0
    assert tracker.total > 0


def test_an_unjudged_entry_costs_no_judge_tokens():
    from evals.cost import CostTracker
    from evals.models import EvalEntry
    from evals.run import score_with_judge

    tracker = CostTracker(agent_model="claude-opus-5", judge_model=JUDGE_MODEL)
    entry = EvalEntry(id="x", category="single-hop", turns=("q",), criteria=())

    assert score_with_judge(
        Judge(client=FakeJudgeClient()),  # type: ignore[arg-type]
        entry,
        transcript("Answer. [1]"),
        tracker,
    ) == []
    assert tracker.judge_input == 0
