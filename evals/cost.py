"""Token pricing and cost estimation (§5).

Every run reports what it will cost before starting and what it cost on
finishing, so spend is observed rather than discovered on a bill.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICING_USD_PER_MTOK = {
    # Reference rates (§2). Update alongside the model configuration.
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Calibrated against the first real run (2026-09-13): single-hop entries used
# 2,960-5,496 input and 220-519 output tokens each, growing with the number of
# retrievals. These sit above that range, since multi-hop and conversation
# entries retrieve more -- an estimate should err high, but not so high that it
# stops being informative.
TYPICAL_INPUT_TOKENS_PER_ENTRY = 8_000
TYPICAL_OUTPUT_TOKENS_PER_ENTRY = 600
TYPICAL_JUDGE_INPUT_TOKENS = 6_000
TYPICAL_JUDGE_OUTPUT_TOKENS = 250


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    """Dollar cost of one call. Unknown models cost 0 and are reported as such."""
    rates = PRICING_USD_PER_MTOK.get(model)
    if rates is None:
        return 0.0
    input_rate, output_rate = rates
    return (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate


@dataclass
class CostTracker:
    """Accumulates real spend across a run."""

    agent_model: str
    judge_model: str
    agent_input: int = 0
    agent_output: int = 0
    judge_input: int = 0
    judge_output: int = 0

    def add_agent(self, input_tokens: int, output_tokens: int) -> None:
        self.agent_input += input_tokens
        self.agent_output += output_tokens

    def add_judge(self, input_tokens: int, output_tokens: int) -> None:
        self.judge_input += input_tokens
        self.judge_output += output_tokens

    @property
    def agent_cost(self) -> float:
        return price(self.agent_model, self.agent_input, self.agent_output)

    @property
    def judge_cost(self) -> float:
        return price(self.judge_model, self.judge_input, self.judge_output)

    @property
    def total(self) -> float:
        return self.agent_cost + self.judge_cost

    def summary(self) -> str:
        return (
            f"agent {self.agent_input:,} in / {self.agent_output:,} out = "
            f"${self.agent_cost:.4f}\n"
            f"judge {self.judge_input:,} in / {self.judge_output:,} out = "
            f"${self.judge_cost:.4f}\n"
            f"TOTAL ${self.total:.4f}"
        )


def estimate(entry_count: int, agent_model: str, judge_model: str, *, judged: bool) -> float:
    """Up-front estimate, shown before a run starts."""
    agent = price(
        agent_model,
        entry_count * TYPICAL_INPUT_TOKENS_PER_ENTRY,
        entry_count * TYPICAL_OUTPUT_TOKENS_PER_ENTRY,
    )
    if not judged:
        return agent
    judge = price(
        judge_model,
        entry_count * TYPICAL_JUDGE_INPUT_TOKENS,
        entry_count * TYPICAL_JUDGE_OUTPUT_TOKENS,
    )
    return agent + judge
