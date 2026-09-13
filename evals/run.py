"""The scoped eval runner (§5).

    python -m evals.run --category single-hop --limit 5    # the normal invocation
    python -m evals.run --all                              # release checkpoints only

Small and scoped is the default. The runner prints an estimated cost and the
entry count **before** starting, and the actual cost when it finishes, so spend
is observed rather than discovered later (principle #15).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wikimedia_agent.agent import DEFAULT_MODEL, WikipediaAgent, build_agent

from .cost import CostTracker, estimate
from .judge import JUDGE_MODEL, Judge, JudgeError, build_package
from .models import (
    EntryResult,
    EvalEntry,
    Score,
    Transcript,
    TurnRecord,
    load_dataset,
    select,
)
from .scorers import DETERMINISTIC_SCORERS, deterministic_signals

HERE = Path(__file__).resolve().parent
DATASET_PATH = HERE / "dataset.jsonl"
REPORTS_DIR = HERE / "reports"

# Which deterministic scorers apply to which category. Criteria are enabled per
# entry, never rewritten -- so "was the refusal correct?" is the same question
# in the same words wherever it is asked (§5).
CATEGORY_SCORERS: dict[str, tuple[str, ...]] = {
    "single-hop": ("grounding", "citation_validity", "provenance_integrity",
                   "source_disclosure"),
    "multi-hop": ("grounding", "citation_validity", "provenance_integrity",
                  "source_disclosure"),
    "not-in-wikipedia": ("refusal_correctness", "citation_validity"),
    "ambiguous-no-context": ("asks_for_clarification",),
    "unambiguous-control": ("does_not_ask", "grounding", "citation_validity"),
    "ambiguous-resolvable": ("grounding", "citation_validity", "does_not_ask"),
    "low-quality-source": ("grounding", "citation_validity", "source_disclosure"),
    "competing-sources": ("grounding", "citation_validity", "source_disclosure"),
}
DEFAULT_SCORERS = ("grounding", "citation_validity", "provenance_integrity")


@dataclass
class Report:
    judge_version: str
    agent_model: str
    judge_model: str
    started_at: str
    scope: dict[str, Any]
    results: list[EntryResult] = field(default_factory=list)
    cost_usd: float = 0.0
    cost_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_version": self.judge_version,
            "agent_model": self.agent_model,
            "judge_model": self.judge_model,
            "started_at": self.started_at,
            "scope": self.scope,
            "cost_usd": round(self.cost_usd, 4),
            "cost_detail": self.cost_detail,
            "summary": self.summary(),
            "results": [result.to_dict() for result in self.results],
        }

    def summary(self) -> dict[str, Any]:
        by_criterion: dict[str, dict[str, int]] = {}
        for result in self.results:
            for score in result.scores:
                bucket = by_criterion.setdefault(score.criterion, {"passed": 0, "total": 0})
                bucket["total"] += 1
                bucket["passed"] += int(score.passed)
        return {
            "entries": len(self.results),
            "entries_passed": sum(1 for result in self.results if result.passed),
            "by_criterion": {
                name: {
                    **counts,
                    "rate": round(counts["passed"] / counts["total"], 3) if counts["total"] else 0,
                }
                for name, counts in sorted(by_criterion.items())
            },
        }


def _retrieved_record(provenance: Any, grades: dict[int, Any]) -> dict[str, Any]:
    grade = grades.get(provenance.page_id)
    return {
        "title": provenance.title,
        "section": provenance.section,
        "revision_id": provenance.revision_id,
        "article_url": provenance.article_url,
        "grade": grade.label if grade else None,
        "poor_quality": bool(grade and grade.is_poor),
    }


def record_turn(question: str, answer: Any) -> TurnRecord:
    """Capture one turn as the judge will read it."""
    rendered = answer.rendered
    return TurnRecord(
        question=question,
        answer_text=answer.text,
        rendered_text=rendered.text if rendered else answer.text,
        cited_numbers=rendered.cited_numbers if rendered else [],
        unresolved_citations=list(rendered.unresolved) if rendered else [],
        tool_calls=[
            {"name": call.name, "arguments": call.arguments} for call in answer.tool_calls
        ],
        retrieved=[
            _retrieved_record(provenance, answer.grades) for provenance in answer.sources
        ],
        stop_reason=answer.stop_reason,
        input_tokens=answer.input_tokens,
        output_tokens=answer.output_tokens,
    )


def run_entry(agent: WikipediaAgent, entry: EvalEntry) -> Transcript:
    transcript = Transcript(entry_id=entry.id, category=entry.category, agent_model=agent.model)
    for turn in entry.turns:
        # Phase 6 runs each turn independently; conversation arrives in Phase 8.
        transcript.turns.append(record_turn(turn, agent.ask(turn)))
    return transcript


def score_deterministically(entry: EvalEntry, transcript: Transcript) -> list[Score]:
    names = CATEGORY_SCORERS.get(entry.category, DEFAULT_SCORERS)
    return [DETERMINISTIC_SCORERS[name](transcript) for name in names]


def score_with_judge(
    judge: Judge, entry: EvalEntry, transcript: Transcript, tracker: CostTracker
) -> list[Score]:
    """Judge the entry and record what judging cost."""
    criteria = [c for c in entry.criteria if c in {"answer_correctness"}]
    if not criteria:
        return []

    package = build_package(
        context=list(entry.turns),
        transcript=transcript.to_dict(),
        reference_answer=entry.reference_answer,
        expected_articles=list(entry.expected_articles),
        signals=deterministic_signals(transcript),
        criteria=criteria,
    )
    result = judge.judge(package, criteria)
    tracker.add_judge(result.input_tokens, result.output_tokens)
    return [
        Score(verdict.criterion, verdict.passed, verdict.reason, judged=True)
        for verdict in result.verdicts
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Run a scoped eval. Costs money; nothing here runs under pytest.",
    )
    parser.add_argument("--category", help="only this category")
    parser.add_argument("--split", choices=["train", "validation", "test"])
    parser.add_argument("--limit", type=int, help="at most this many entries")
    parser.add_argument("--all", action="store_true", help="the full set (release checkpoints)")
    parser.add_argument("--no-judge", action="store_true", help="deterministic scores only (free)")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    args = parser.parse_args(argv)

    entries = load_dataset(args.dataset)
    if not args.all and not any([args.category, args.split, args.limit]):
        parser.error(
            "refusing to run the whole set implicitly. Pass a scope "
            "(--category / --split / --limit), or --all for a release checkpoint."
        )

    chosen = (
        entries if args.all
        else select(entries, category=args.category, split=args.split, limit=args.limit)
    )
    if not chosen:
        print("No entries matched that scope.", file=sys.stderr)
        return 1

    judged = not args.no_judge
    projected = estimate(len(chosen), DEFAULT_MODEL, JUDGE_MODEL, judged=judged)

    print(f"Entries:        {len(chosen)}")
    print(f"Agent model:    {DEFAULT_MODEL}")
    print(f"Judge model:    {JUDGE_MODEL if judged else '(disabled)'}")
    print(f"Estimated cost: ~${projected:.2f}")
    if not args.yes:
        try:
            if input("Proceed? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Cancelled.")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0

    agent = build_agent()
    judge = Judge(client=agent.client) if judged else None
    tracker = CostTracker(agent_model=agent.model, judge_model=JUDGE_MODEL)

    report = Report(
        judge_version=judge.version if judge else "none",
        agent_model=agent.model,
        judge_model=JUDGE_MODEL if judged else "none",
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scope={
            "category": args.category,
            "split": args.split,
            "limit": args.limit,
            "all": args.all,
        },
    )

    for index, entry in enumerate(chosen, start=1):
        print(f"[{index}/{len(chosen)}] {entry.id} ({entry.category})", flush=True)
        transcript = run_entry(agent, entry)
        tracker.add_agent(transcript.input_tokens, transcript.output_tokens)

        scores = score_deterministically(entry, transcript)
        if judge is not None:
            try:
                scores.extend(score_with_judge(judge, entry, transcript, tracker))
            except JudgeError as exc:
                # A broken judge is not a failing agent, so this is reported
                # rather than scored as zero.
                print(f"    judge error: {exc}", file=sys.stderr)

        report.results.append(EntryResult(entry=entry, transcript=transcript, scores=scores))
        for score in scores:
            print(f"    {'PASS' if score.passed else 'FAIL'}  {score.criterion}: {score.detail}")

    report.cost_usd = tracker.total
    report.cost_detail = tracker.summary()

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"report-{stamp}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2))

    print("\n" + "=" * 60)
    summary = report.summary()
    print(f"Entries passed: {summary['entries_passed']}/{summary['entries']}")
    for name, counts in summary["by_criterion"].items():
        print(f"  {name:24} {counts['passed']}/{counts['total']}  ({counts['rate']:.0%})")
    print()
    print(tracker.summary())
    print(f"\nJudge version: {report.judge_version}")
    print(f"Report:        {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
