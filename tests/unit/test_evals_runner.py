"""The scoped runner and cost reporting (§5, Phase 6)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from evals.cost import CostTracker, estimate, price
from evals.models import EntryResult, EvalEntry, Score, Transcript, TurnRecord
from evals.run import CATEGORY_SCORERS, Report, main, score_deterministically

# -- cost ------------------------------------------------------------------


def test_price_uses_the_published_rates():
    # Opus 5: $5 per MTok in, $25 out.
    assert price("claude-opus-5", 1_000_000, 0) == pytest.approx(5.0)
    assert price("claude-opus-5", 0, 1_000_000) == pytest.approx(25.0)


def test_the_judge_is_cheaper_than_the_agent_per_token():
    assert price("claude-sonnet-5", 1_000_000, 0) < price("claude-opus-5", 1_000_000, 0)


def test_an_unknown_model_costs_zero_rather_than_guessing():
    assert price("some-future-model", 1_000_000, 1_000_000) == 0.0


def test_tracker_separates_agent_and_judge_spend():
    tracker = CostTracker(agent_model="claude-opus-5", judge_model="claude-sonnet-5")
    tracker.add_agent(100_000, 10_000)
    tracker.add_judge(50_000, 2_000)
    assert tracker.agent_cost > 0
    assert tracker.judge_cost > 0
    assert tracker.total == pytest.approx(tracker.agent_cost + tracker.judge_cost)
    assert "TOTAL" in tracker.summary()


def test_estimate_scales_with_entry_count():
    one = estimate(1, "claude-opus-5", "claude-sonnet-5", judged=True)
    ten = estimate(10, "claude-opus-5", "claude-sonnet-5", judged=True)
    assert ten == pytest.approx(one * 10)


def test_disabling_the_judge_lowers_the_estimate():
    judged = estimate(5, "claude-opus-5", "claude-sonnet-5", judged=True)
    unjudged = estimate(5, "claude-opus-5", "claude-sonnet-5", judged=False)
    assert unjudged < judged


# -- category scorers are enabled, not rewritten --------------------------


def test_each_category_enables_its_own_criteria():
    assert "refusal_correctness" in CATEGORY_SCORERS["not-in-wikipedia"]
    assert "asks_for_clarification" in CATEGORY_SCORERS["ambiguous-no-context"]
    assert "does_not_ask" in CATEGORY_SCORERS["unambiguous-control"]


def test_the_same_criterion_name_is_reused_across_categories():
    """Criteria are enabled per entry, never rewritten -- so the same question
    is asked in the same words wherever it applies."""
    single = set(CATEGORY_SCORERS["single-hop"])
    multi = set(CATEGORY_SCORERS["multi-hop"])
    assert "citation_validity" in single & multi


def test_scoring_applies_the_category_scorers():
    entry = EvalEntry(id="x", category="not-in-wikipedia", turns=("q",))
    transcript = Transcript(
        entry_id="x",
        category="not-in-wikipedia",
        turns=[TurnRecord(question="q", answer_text="I could not find this.")],
    )
    names = [score.criterion for score in score_deterministically(entry, transcript)]
    assert names == list(CATEGORY_SCORERS["not-in-wikipedia"])


# -- reports ---------------------------------------------------------------


def make_report(**overrides: Any) -> Report:
    fields: dict[str, Any] = {
        "judge_version": "claude-sonnet-5/1.0.0/effort=low",
        "agent_model": "claude-opus-5",
        "judge_model": "claude-sonnet-5",
        "started_at": "2026-09-13T00:00:00+00:00",
        "scope": {"category": "single-hop", "limit": 2},
    }
    fields.update(overrides)
    return Report(**fields)


def test_a_report_stamps_the_judge_version():
    """Scores from different judge versions must never be compared."""
    assert "judge_version" in make_report().to_dict()


def test_a_report_summarises_per_criterion():
    entry = EvalEntry(id="x", category="single-hop", turns=("q",))
    transcript = Transcript(entry_id="x", category="single-hop", turns=[])
    report = make_report()
    report.results = [
        EntryResult(entry, transcript, [Score("citation_validity", True)]),
        EntryResult(entry, transcript, [Score("citation_validity", False)]),
    ]
    summary = report.summary()
    assert summary["entries"] == 2
    assert summary["entries_passed"] == 1
    assert summary["by_criterion"]["citation_validity"] == {
        "passed": 1, "total": 2, "rate": 0.5
    }


def test_a_report_serialises_to_json():
    json.dumps(make_report().to_dict())


# -- the scope guard -------------------------------------------------------


def test_running_without_a_scope_is_refused(capsys):
    """Small and scoped is the default; the full set is a deliberate act."""
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code != 0
    assert "refusing to run the whole set" in capsys.readouterr().err


def test_a_scope_is_accepted_and_cost_is_shown_before_running(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p="": "n")
    assert main(["--category", "single-hop", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "Estimated cost" in out
    assert "Cancelled" in out


def test_declining_the_cost_prompt_runs_nothing(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p="": "")
    main(["--all"])
    assert "Cancelled" in capsys.readouterr().out


def test_an_empty_scope_reports_rather_than_running(capsys):
    assert main(["--category", "injection"]) == 1
    assert "No entries matched" in capsys.readouterr().err


def test_the_estimate_stays_in_the_right_order_of_magnitude():
    """Calibrated against a real run: 2 single-hop entries cost ~$0.06 for the
    agent. An estimate should err high, but a 3x overshoot stops being useful."""
    projected = estimate(2, "claude-opus-5", "claude-sonnet-5", judged=False)
    assert 0.05 <= projected <= 0.15, projected
