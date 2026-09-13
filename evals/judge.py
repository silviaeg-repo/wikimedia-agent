"""The eval judge -- a fixed, versioned instrument (§5, principle #20).

A judge that grades differently between runs makes every comparison meaningless,
so model, prompt, rubric and settings are pinned together as ``JUDGE_VERSION``
and stamped on every report. Scores from different judge versions are never
compared; re-judge the stored transcripts instead.

The judge is configured **separately from the agent** -- Sonnet 5 grading Opus 5,
so it is not marking its own tier's homework -- and its scope is deliberately
narrow: deterministic facts arrive precomputed (§5), so it rules only on what
actually needs judgement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic
from anthropic.types import OutputConfigParam, ThinkingConfigAdaptiveParam

JUDGE_MODEL = "claude-sonnet-5"
JUDGE_EFFORT = "low"
JUDGE_MAX_TOKENS = 2_000
JUDGE_CONTEXT_LIMIT = 900_000
"""Sonnet 5 holds 1M tokens. We stop well short and fail loudly rather than
truncate: silently grading a partial transcript is a wrong score presented as a
right one (principle #13)."""

RUBRIC_VERSION = "1.0.0"
JUDGE_VERSION = f"{JUDGE_MODEL}/{RUBRIC_VERSION}/effort={JUDGE_EFFORT}"

JUDGE_SYSTEM = """\
You grade a Wikipedia question-answering agent. You are a measuring instrument: \
apply the rubric exactly as written, the same way every time.

You will receive, for one entry:
  * CONTEXT -- the question, or the conversation so far
  * TRANSCRIPT -- what the agent did: tool calls, what it retrieved, its answer
  * REFERENCE -- the expected answer and expected source articles
  * SIGNALS -- facts already established in code
  * CRITERIA -- exactly which criteria to judge for this entry

Rules:

1. SIGNALS are established facts. Do not re-derive, re-check or dispute them. \
Whether the agent searched, whether citations resolve, whether sources were \
disclosed -- all already determined. Use them as inputs.
2. Judge ONLY the criteria listed. Ignore everything else, however noteworthy.
3. Grade the answer against the REFERENCE, not against your own knowledge of \
the subject. A correct-sounding answer the reference contradicts fails.
4. An answer may be phrased differently from the reference and still be right. \
Judge substance, not wording.
5. Be consistent. The same answer on a different day must get the same verdict.

Return ONLY a JSON object, no prose around it:

{"verdicts": [{"criterion": "<name>", "passed": true|false,
               "confidence": "high"|"medium"|"low",
               "reason": "<one sentence>"}]}

One entry per criterion given, in the order given.
"""

JUDGEABLE_CRITERIA = {
    "answer_correctness": (
        "Does the answer state what the reference states? Substance, not wording. "
        "If the agent correctly declined because Wikipedia lacks the answer and the "
        "reference says so too, that passes."
    ),
    "follow_up_resolution": (
        "Did the answer address the subject the conversation established, rather than "
        "drifting to a different one?"
    ),
    "weak_source_acknowledged": (
        "SIGNALS lists poor-quality sources used. If a claim's only support is one of "
        "them, does the answer say so? If no poor source was used, this passes."
    ),
    "reading_named": (
        "For an ambiguous subject resolved from context: does the answer say which "
        "reading it chose (e.g. 'taking Mercury as the planet')?"
    ),
}


class JudgeError(RuntimeError):
    """The judge could not be used. Raised rather than scored as zero: a broken
    judge is not a failing agent."""


@dataclass(frozen=True)
class Verdict:
    criterion: str
    passed: bool
    confidence: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "passed": self.passed,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def build_package(
    *,
    context: list[str],
    transcript: dict[str, Any],
    reference_answer: str,
    expected_articles: list[str],
    signals: dict[str, Any],
    criteria: list[str],
) -> str:
    """Assemble the one structured input the judge receives.

    Every part is here for a reason: context (a follow-up cannot be graded
    without what came before), the full trace (what the agent *did*, not just
    what it said), the reference (the target), the signals (what code already
    knows), and the criteria that apply to this entry.
    """
    unknown = set(criteria) - set(JUDGEABLE_CRITERIA)
    if unknown:
        raise JudgeError(f"not judgeable criteria: {sorted(unknown)}")

    rubric = "\n".join(f"- {name}: {JUDGEABLE_CRITERIA[name]}" for name in criteria)
    return "\n\n".join(
        [
            "## CONTEXT",
            "\n".join(f"{index}. {turn}" for index, turn in enumerate(context, start=1)),
            "## TRANSCRIPT",
            json.dumps(transcript, indent=1, default=str),
            "## REFERENCE",
            f"Expected answer: {reference_answer or '(none given)'}\n"
            f"Expected source articles: {expected_articles or '(none given)'}",
            "## SIGNALS (established facts -- do not re-derive)",
            json.dumps(signals, indent=1, default=str),
            "## CRITERIA (judge exactly these, in this order)",
            rubric,
        ]
    )


def parse_verdicts(raw: str, expected: list[str]) -> list[Verdict]:
    """Parse the judge's reply, failing loudly on anything malformed.

    A malformed reply must not silently score zero -- that would look like an
    agent regression when it is a judge fault.
    """
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise JudgeError(f"judge returned no JSON object: {text[:200]!r}")

    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge returned invalid JSON: {exc}") from exc

    entries = payload.get("verdicts")
    if not isinstance(entries, list):
        raise JudgeError("judge reply has no 'verdicts' list")

    verdicts: list[Verdict] = []
    for item in entries:
        if not isinstance(item, dict) or "criterion" not in item or "passed" not in item:
            raise JudgeError(f"malformed verdict: {item!r}")
        verdicts.append(
            Verdict(
                criterion=str(item["criterion"]),
                passed=bool(item["passed"]),
                confidence=str(item.get("confidence", "unknown")),
                reason=str(item.get("reason", "")),
            )
        )

    returned = {verdict.criterion for verdict in verdicts}
    missing = set(expected) - returned
    if missing:
        raise JudgeError(f"judge omitted verdicts for {sorted(missing)}")
    return verdicts


@dataclass
class Judge:
    """The judge, pinned as one versioned unit."""

    client: Anthropic
    model: str = JUDGE_MODEL
    effort: str = JUDGE_EFFORT
    max_tokens: int = JUDGE_MAX_TOKENS
    context_limit: int = JUDGE_CONTEXT_LIMIT

    @property
    def version(self) -> str:
        return f"{self.model}/{RUBRIC_VERSION}/effort={self.effort}"

    def judge(self, package: str, criteria: list[str]) -> list[Verdict]:
        self._check_size(package)
        thinking: ThinkingConfigAdaptiveParam = {"type": "adaptive"}
        output_config: OutputConfigParam = {"effort": self.effort}  # type: ignore[typeddict-item]

        message = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": package}],
            thinking=thinking,
            output_config=output_config,
        )
        if getattr(message, "stop_reason", None) == "refusal":
            raise JudgeError("the judge declined to grade this entry")

        text = "".join(
            str(getattr(block, "text", ""))
            for block in (getattr(message, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        return parse_verdicts(text, criteria)

    def _check_size(self, package: str) -> None:
        """Fail loudly rather than grade a truncated transcript."""
        approx_tokens = len(package) // 4
        if approx_tokens > self.context_limit:
            raise JudgeError(
                f"judge package is roughly {approx_tokens:,} tokens, over the "
                f"{self.context_limit:,} limit for {self.model}. Refusing to truncate: "
                "grading a partial transcript would be a wrong score presented as a "
                "right one."
            )
