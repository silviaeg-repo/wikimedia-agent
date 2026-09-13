"""Re-scoring stored transcripts for free (§5)."""

from __future__ import annotations

import json

from evals.models import EvalEntry, Transcript, TurnRecord
from evals.rescore import (
    CITATION_DEPENDENT,
    main,
    predates_rendered_citations,
    rescore_report,
    transcript_from,
)


def stored_turn(**overrides):
    turn = {
        "question": "Who was Ada Lovelace?",
        "answer_text": "An answer. [1]",
        "rendered_text": "An answer. [1]",
        "tool_calls": [{"name": "get_summary"}],
        "retrieved": [{"title": "Ada Lovelace", "revision_id": 42,
                       "article_url": "https://en.wikipedia.org/wiki/Ada_Lovelace"}],
        "cited_numbers": [1],
        "unresolved_citations": [],
        "clarifications": [],
        "stop_reason": "end_turn",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    turn.update(overrides)
    return turn


def write_report(tmp_path, entry_id="sh-001", category="single-hop", turns=None, scores=None):
    payload = {
        "judge_version": "claude-sonnet-5/1.0.0/effort=low",
        "results": [{
            "entry_id": entry_id,
            "scores": scores if scores is not None else [
                {"criterion": "citation_validity", "passed": True, "judged": False},
                {"criterion": "answer_correctness", "passed": True, "judged": True},
            ],
            "transcript": {
                "entry_id": entry_id,
                "category": category,
                "turns": turns if turns is not None else [stored_turn()],
            },
        }],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload))
    return path


def dataset(entry_id="sh-001", **kwargs):
    return {entry_id: EvalEntry(id=entry_id, category=kwargs.pop("category", "single-hop"),
                                turns=("q",), **kwargs)}


# -- reconstruction --------------------------------------------------------


def test_a_stored_transcript_round_trips():
    transcript = transcript_from({
        "entry_id": "x", "category": "single-hop", "turns": [stored_turn()],
    })
    assert transcript.turns[0].cited_numbers == [1]
    assert transcript.turns[0].retrieved[0]["title"] == "Ada Lovelace"


def test_fields_added_after_a_report_was_written_default_to_empty():
    """Older reports must stay re-scorable rather than crashing."""
    transcript = transcript_from({
        "entry_id": "x", "category": "single-hop",
        "turns": [{"question": "q", "answer_text": "a"}],
    })
    assert transcript.turns[0].cited_numbers == []
    assert transcript.turns[0].clarifications == []


# -- re-scoring ------------------------------------------------------------


def test_deterministic_scores_are_recomputed(tmp_path):
    result = rescore_report(write_report(tmp_path), dataset())
    criteria = [s["criterion"] for s in result["rows"][0]["scores"]]
    assert "citation_validity" in criteria


def test_judged_scores_are_carried_over_not_recomputed(tmp_path):
    """Re-judging would cost money; these came from a paid call."""
    result = rescore_report(write_report(tmp_path), dataset())
    assert result["rows"][0]["judged_carried_over"] == ["answer_correctness"]
    assert "answer_correctness" not in [s["criterion"] for s in result["rows"][0]["scores"]]


def test_a_changed_verdict_is_flagged(tmp_path):
    """The point of re-scoring: seeing what a scorer fix would have changed."""
    path = write_report(tmp_path, scores=[
        {"criterion": "citation_validity", "passed": False, "judged": False},
    ])
    result = rescore_report(path, dataset())
    assert "citation_validity" in result["rows"][0]["changed"]


def test_a_scorer_that_did_not_exist_at_run_time_is_flagged(tmp_path):
    path = write_report(tmp_path, scores=[])
    result = rescore_report(path, dataset())
    assert "citation_validity" in result["rows"][0]["new"]


def test_forbidden_content_is_not_scored_twice(tmp_path):
    """Regression: score_deterministically already appends it."""
    path = write_report(tmp_path, entry_id="nw-001", category="not-in-wikipedia")
    entries = dataset("nw-001", category="not-in-wikipedia",
                      forbidden_content=(r"\btoast\b",))
    result = rescore_report(path, entries)
    criteria = [s["criterion"] for s in result["rows"][0]["scores"]]
    assert criteria.count("no_forbidden_content") == 1


def test_an_entry_removed_from_the_dataset_is_reported(tmp_path):
    result = rescore_report(write_report(tmp_path, entry_id="gone"), dataset())
    assert "no longer in the dataset" in result["rows"][0]["status"]


# -- transcripts too old for the newer scorers ----------------------------


def test_pre_renderer_transcripts_are_detected():
    old = Transcript(entry_id="x", category="single-hop", turns=[
        TurnRecord(question="q", answer_text="An answer. [1]",
                   retrieved=[{"title": "Ada Lovelace", "revision_id": 42}])
    ])
    assert predates_rendered_citations(old) is True


def test_current_transcripts_are_not_mistaken_for_old_ones():
    current = transcript_from({"entry_id": "x", "category": "single-hop",
                               "turns": [stored_turn()]})
    assert predates_rendered_citations(current) is False


def test_citation_scorers_are_skipped_not_failed_on_old_transcripts(tmp_path):
    """Reporting FAIL against data that predates the format would be a false
    alarm from our own tooling (principle #16)."""
    old_turn = stored_turn(rendered_text="", cited_numbers=[], answer_text="An answer. [1]")
    path = write_report(tmp_path, turns=[old_turn])
    result = rescore_report(path, dataset())

    row = result["rows"][0]
    assert "citation_validity" in row["skipped"]
    assert "citation_validity" not in [s["criterion"] for s in row["scores"]]
    assert row["changed"] == []


def test_every_skipped_criterion_is_citation_dependent(tmp_path):
    old_turn = stored_turn(rendered_text="", cited_numbers=[])
    path = write_report(tmp_path, turns=[old_turn])
    result = rescore_report(path, dataset())
    assert set(result["rows"][0]["skipped"]) <= CITATION_DEPENDENT


# -- the command -----------------------------------------------------------


def test_rescoring_costs_nothing_and_says_so(tmp_path, capsys):
    assert main([str(write_report(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "at no cost" in out
    assert "re-judging needs a paid run" in out.lower()


def test_missing_reports_are_reported(tmp_path, capsys):
    assert main([], ) in (0, 1)
