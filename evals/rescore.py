"""Re-score stored transcripts, for free (§5).

    python -m evals.rescore                       # every stored report
    python -m evals.rescore evals/reports/X.json  # one report

Deterministic scores are computed from the transcript, so they can be recomputed
whenever the scorers change without calling any model. That is the point of
storing transcripts: a scorer fix should not cost another eval run.

What this cannot recompute is anything judged -- answer correctness, refusal,
framing. Those came from a paid call and are carried through unchanged, labelled
with the judge version that produced them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .models import EvalEntry, Score, Transcript, TurnRecord, load_dataset
from .run import DATASET_PATH, REPORTS_DIR, score_deterministically

CITATION_DEPENDENT = {"citation_validity", "injection_resistance", "did_not_commit_to_a_reading"}
"""Scorers that read the renderer's resolved citations.

Transcripts written before the renderer existed (Phase 7) carry no such field,
so these cannot be recomputed against them -- and reporting FAIL would be a
false alarm on data that simply predates the format (principle #16)."""


def predates_rendered_citations(transcript: Transcript) -> bool:
    """Whether this transcript is too old for the citation-dependent scorers."""
    return any(
        turn.retrieved and not turn.rendered_text and not turn.cited_numbers
        for turn in transcript.turns
    )


def transcript_from(payload: dict[str, Any]) -> Transcript:
    """Rebuild a Transcript from stored JSON.

    Tolerant of older reports: fields added after a report was written default
    to empty rather than failing, so early runs stay re-scorable.
    """
    transcript = Transcript(
        entry_id=str(payload.get("entry_id", "?")),
        category=str(payload.get("category", "")),
        agent_model=str(payload.get("agent_model", "")),
    )
    for turn in payload.get("turns", []) or []:
        transcript.turns.append(
            TurnRecord(
                question=str(turn.get("question", "")),
                answer_text=str(turn.get("answer_text", "")),
                rendered_text=str(turn.get("rendered_text", "")),
                tool_calls=list(turn.get("tool_calls", []) or []),
                retrieved=list(turn.get("retrieved", []) or []),
                cited_numbers=list(turn.get("cited_numbers", []) or []),
                unresolved_citations=list(turn.get("unresolved_citations", []) or []),
                clarifications=list(turn.get("clarifications", []) or []),
                stop_reason=turn.get("stop_reason"),
                input_tokens=int(turn.get("input_tokens", 0) or 0),
                output_tokens=int(turn.get("output_tokens", 0) or 0),
                latency_seconds=float(turn.get("latency_seconds", 0) or 0),
            )
        )
    return transcript


def rescore_report(path: Path, entries: dict[str, EvalEntry]) -> dict[str, Any]:
    report = json.loads(path.read_text())
    rows: list[dict[str, Any]] = []

    for result in report.get("results", []) or []:
        entry_id = str(result.get("entry_id", "?"))
        transcript = transcript_from(result.get("transcript", {}))
        entry = entries.get(entry_id)
        if entry is None:
            # The dataset entry has since been renamed or removed.
            rows.append({"entry_id": entry_id, "status": "no longer in the dataset"})
            continue

        # score_deterministically already appends forbidden_content when the
        # entry declares it; adding it here too would double-count.
        fresh: list[Score] = score_deterministically(entry, transcript)
        skipped: list[str] = []
        if predates_rendered_citations(transcript):
            skipped = [s.criterion for s in fresh if s.criterion in CITATION_DEPENDENT]
            fresh = [s for s in fresh if s.criterion not in CITATION_DEPENDENT]

        previous = {
            score["criterion"]: score["passed"]
            for score in result.get("scores", []) or []
            if not score.get("judged")
        }
        carried = [
            score for score in result.get("scores", []) or [] if score.get("judged")
        ]

        rows.append({
            "entry_id": entry_id,
            "category": entry.category,
            "scores": [score.to_dict() for score in fresh],
            "skipped": skipped,
            "changed": [
                score.criterion for score in fresh
                if score.criterion in previous and previous[score.criterion] != score.passed
            ],
            "new": [score.criterion for score in fresh if score.criterion not in previous],
            "judged_carried_over": [score["criterion"] for score in carried],
        })

    return {
        "source_report": path.name,
        "judge_version_of_carried_scores": report.get("judge_version"),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.rescore",
        description="Recompute deterministic scores from stored transcripts. Costs nothing.",
    )
    parser.add_argument("reports", nargs="*", type=Path, help="report files (default: all)")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--quiet", action="store_true", help="only show changes")
    args = parser.parse_args(argv)

    paths = args.reports or sorted(REPORTS_DIR.glob("*.json"))
    if not paths:
        print(f"No reports found in {REPORTS_DIR}.", file=sys.stderr)
        return 1

    entries = {entry.id: entry for entry in load_dataset(args.dataset)}
    total_changed = total_new = 0

    for path in paths:
        result = rescore_report(path, entries)
        print(f"\n{path.name}  (judged scores from {result['judge_version_of_carried_scores']})")

        for row in result["rows"]:
            if row.get("status"):
                print(f"  {row['entry_id']:12} — {row['status']}")
                continue

            failed = [s for s in row["scores"] if not s["passed"]]
            total_changed += len(row["changed"])
            total_new += len(row["new"])

            if args.quiet and not (failed or row["changed"] or row["new"] or row["skipped"]):
                continue

            print(f"  {row['entry_id']:12} {row['category']}")
            for criterion in row.get("skipped", []):
                print(f"      ----  {criterion}: not comparable — this transcript "
                      "predates rendered citations")
            for score in row["scores"]:
                mark = "PASS" if score["passed"] else "FAIL"
                tag = ""
                if score["criterion"] in row["changed"]:
                    tag = "  <- CHANGED since the run"
                elif score["criterion"] in row["new"]:
                    tag = "  <- new scorer, never run before"
                print(f"      {mark}  {score['criterion']}: {score['detail']}{tag}")

    print(f"\n{'=' * 60}")
    print(f"Re-scored {len(paths)} report(s) at no cost.")
    print(f"Verdicts changed by scorer fixes: {total_changed}")
    print(f"Criteria scored that did not exist at run time: {total_new}")
    print("Judged criteria are carried over unchanged -- re-judging needs a paid run.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
