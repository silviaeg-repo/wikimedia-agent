"""Dataset entries, transcripts and scores (§5)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_CATEGORIES = {
    "single-hop",
    "multi-hop",
    "ambiguous-no-context",
    "ambiguous-resolvable",
    "clarification-answered",
    "unambiguous-control",
    "not-in-wikipedia",
    "low-quality-source",
    "competing-sources",
    "recently-changed",
    "injection",
    "follow-up",
}

VALID_CRITERIA = {
    "answer_correctness",
    "citation_validity",
    "refusal_correctness",
    "provenance_integrity",
    "source_disclosure",
    "asks_for_clarification",
    "does_not_ask",
    "injection_resistance",
}


class DatasetError(ValueError):
    """A malformed dataset entry. Raised loudly rather than skipped: a silently
    dropped entry makes a score look better than it is (principle #13)."""


@dataclass(frozen=True)
class EvalEntry:
    """One graded question, or a short conversation scored turn by turn."""

    id: str
    category: str
    turns: tuple[str, ...]
    reference_answer: str = ""
    expected_articles: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    split: str = "train"
    notes: str = ""

    @property
    def question(self) -> str:
        return self.turns[0]

    @property
    def is_conversation(self) -> bool:
        return len(self.turns) > 1

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: str = "<dataset>") -> EvalEntry:
        def require(field_name: str) -> Any:
            if field_name not in raw:
                raise DatasetError(f"{source}: entry missing required field {field_name!r}")
            return raw[field_name]

        entry_id = str(require("id"))
        category = str(require("category"))
        if category not in VALID_CATEGORIES:
            raise DatasetError(
                f"{source}: entry {entry_id!r} has unknown category {category!r}. "
                f"Known: {', '.join(sorted(VALID_CATEGORIES))}"
            )

        turns_raw = raw.get("turns")
        if turns_raw is None:
            turns_raw = [require("question")]
        if not isinstance(turns_raw, list) or not turns_raw:
            raise DatasetError(f"{source}: entry {entry_id!r} has no turns")

        criteria = tuple(str(c) for c in raw.get("criteria", ()))
        unknown = set(criteria) - VALID_CRITERIA
        if unknown:
            raise DatasetError(
                f"{source}: entry {entry_id!r} names unknown criteria {sorted(unknown)}"
            )

        split = str(raw.get("split", "train"))
        if split not in {"train", "validation", "test"}:
            raise DatasetError(f"{source}: entry {entry_id!r} has invalid split {split!r}")

        return cls(
            id=entry_id,
            category=category,
            turns=tuple(str(t) for t in turns_raw),
            reference_answer=str(raw.get("reference_answer", "")),
            expected_articles=tuple(str(a) for a in raw.get("expected_articles", ())),
            criteria=criteria,
            split=split,
            notes=str(raw.get("notes", "")),
        )


def load_dataset(path: Path) -> list[EvalEntry]:
    """Read a JSONL dataset, failing loudly on any malformed entry."""
    if not path.exists():
        raise DatasetError(f"dataset not found: {path}")

    entries: list[EvalEntry] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}:{number}: invalid JSON ({exc})") from exc
        entry = EvalEntry.from_dict(raw, source=f"{path.name}:{number}")
        if entry.id in seen:
            raise DatasetError(f"{path.name}:{number}: duplicate entry id {entry.id!r}")
        seen.add(entry.id)
        entries.append(entry)

    if not entries:
        raise DatasetError(f"{path.name}: contains no entries")
    return entries


def select(
    entries: Iterable[EvalEntry],
    *,
    category: str | None = None,
    split: str | None = None,
    limit: int | None = None,
) -> list[EvalEntry]:
    """Scope a run. Small and named is the default (principle #15)."""
    chosen = list(entries)
    if category:
        chosen = [entry for entry in chosen if entry.category == category]
    if split:
        chosen = [entry for entry in chosen if entry.split == split]
    if limit is not None:
        chosen = chosen[:limit]
    return chosen


@dataclass
class TurnRecord:
    """What the agent did on one turn -- the trace the judge reads."""

    question: str
    answer_text: str
    rendered_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    cited_numbers: list[int] = field(default_factory=list)
    unresolved_citations: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer_text": self.answer_text,
            "rendered_text": self.rendered_text,
            "tool_calls": self.tool_calls,
            "retrieved": self.retrieved,
            "cited_numbers": self.cited_numbers,
            "unresolved_citations": self.unresolved_citations,
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass
class Transcript:
    """A complete run of one dataset entry, stored so scoring can be re-run.

    Deterministic scores can be recomputed from this for free; only answer
    correctness needs another paid call (§5).
    """

    entry_id: str
    category: str
    turns: list[TurnRecord] = field(default_factory=list)
    agent_model: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def final(self) -> TurnRecord:
        return self.turns[-1]

    @property
    def input_tokens(self) -> int:
        return sum(turn.input_tokens for turn in self.turns)

    @property
    def output_tokens(self) -> int:
        return sum(turn.output_tokens for turn in self.turns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "category": self.category,
            "agent_model": self.agent_model,
            "created_at": self.created_at,
            "turns": [turn.to_dict() for turn in self.turns],
        }


@dataclass(frozen=True)
class Score:
    """One criterion's verdict on one entry."""

    criterion: str
    passed: bool
    detail: str = ""
    judged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "passed": self.passed,
            "detail": self.detail,
            "judged": self.judged,
        }


@dataclass
class EntryResult:
    entry: EvalEntry
    transcript: Transcript
    scores: list[Score] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(score.passed for score in self.scores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry.id,
            "category": self.entry.category,
            "split": self.entry.split,
            "passed": self.passed,
            "scores": [score.to_dict() for score in self.scores],
            "transcript": self.transcript.to_dict(),
        }


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            yield json.loads(stripped)
