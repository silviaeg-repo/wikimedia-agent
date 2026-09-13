"""The agent loop (§2.2).

Thin by design: the SDK's ``tool_runner`` drives the request -> execute -> loop
cycle, and this module supplies the model configuration, the system prompt, and
the bounds. Everything the agent knows about Wikipedia arrives through the tools
in :mod:`wikimedia_agent.tools`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic
from anthropic.types.beta import BetaMessageParam

from .errors import ConfigurationError
from .prompts import SYSTEM_PROMPT
from .provenance import Grade, Provenance
from .rendering import RenderedAnswer, SourceRegistry, render
from .tools import ToolCall, WikipediaTools, build_tools

DEFAULT_MODEL = "claude-opus-5"
"""C1: an Anthropic model via the Anthropic API. Agent and judge are configured
separately (§2.2) -- this is the agent's, and nothing defaults to it."""

DEFAULT_MAX_TOKENS = 16_000
DEFAULT_MAX_ITERATIONS = 8
"""Retrieval-call ceiling per question (principle #12). Enough for a two-hop
question with a false start; few enough that a runaway loop stops."""


@dataclass
class Answer:
    """One answer, with everything needed to audit it."""

    text: str
    sources: list[Provenance]
    grades: dict[int, Grade]
    stop_reason: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    rendered: RenderedAnswer | None = None

    @property
    def display_text(self) -> str:
        """The answer as a reader should see it: markers decorated, sources
        listed, weak sources flagged (§2.3)."""
        return self.rendered.text if self.rendered is not None else self.text

    @property
    def searched(self) -> bool:
        """Whether the agent retrieved anything at all before answering."""
        return bool(self.tool_calls)

    @property
    def cited_articles(self) -> list[str]:
        seen: list[str] = []
        for provenance in self.sources:
            if provenance.title not in seen:
                seen.append(provenance.title)
        return seen

    @property
    def hit_the_ceiling(self) -> bool:
        """Whether the loop stopped because it ran out of iterations."""
        return self.stop_reason == "max_iterations"

    @property
    def was_refused(self) -> bool:
        return self.stop_reason == "refusal"


@dataclass
class WikipediaAgent:
    """Answers a question from live Wikipedia, with cited sources."""

    tools: WikipediaTools
    client: Anthropic
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    system_prompt: str = SYSTEM_PROMPT
    _tool_list: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self._tool_list:
            self._tool_list = self.tools.as_list()

    def ask(
        self,
        question: str,
        *,
        history: list[BetaMessageParam] | None = None,
        registry: SourceRegistry | None = None,
    ) -> Answer:
        """Answer one question.

        ``history`` carries earlier turns so follow-ups resolve against them
        (§2.5); ``registry`` keeps citation numbers stable across a session.
        Both default to empty, which is a single independent question.
        """
        if not question.strip():
            raise ValueError("question must not be empty")

        self.tools.retrievals.clear()
        self.tools.grades.clear()
        self.tools.calls.clear()

        messages: list[BetaMessageParam] = [
            *(history or []),
            {"role": "user", "content": question},
        ]

        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            tools=self._tool_list,
            messages=messages,
            # Multi-hop retrieval is exactly the kind of decision thinking helps
            # with, and adaptive lets the model spend it where it is needed.
            thinking={"type": "adaptive"},
            max_iterations=self.max_iterations,
        )
        message = runner.until_done()
        return self._to_answer(message, registry=registry)

    def _to_answer(self, message: Any, *, registry: SourceRegistry | None = None) -> Answer:
        """Read the final message, checking stop_reason before content.

        A refusal returns HTTP 200 with ``stop_reason == "refusal"`` and no
        usable content, so reading ``content`` first would surface an empty
        answer as a successful one.
        """
        stop_reason = getattr(message, "stop_reason", None)
        usage = getattr(message, "usage", None)

        answer = Answer(
            text="",
            sources=list(self.tools.retrievals),
            grades=dict(self.tools.grades),
            stop_reason=stop_reason,
            tool_calls=list(self.tools.calls),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

        if stop_reason == "refusal":
            answer.text = (
                "I was unable to answer this question. The request was declined "
                "before an answer could be produced."
            )
            answer.rendered = render(
                answer.text, answer.sources, answer.grades, registry=registry
            )
            return answer

        answer.text = _text_of(message)

        if stop_reason == "max_tokens" and answer.text:
            answer.text += "\n\n[Answer truncated: the response hit its length limit.]"
        elif not answer.text:
            answer.text = (
                "I could not produce an answer from Wikipedia for this question."
            )

        answer.rendered = render(
            answer.text, answer.sources, answer.grades, registry=registry
        )
        return answer


def _text_of(message: Any) -> str:
    """Join the text blocks of a message, ignoring thinking and tool blocks."""
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    return "\n".join(part for part in parts if part).strip()


def build_agent(
    *,
    contact: str | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    **client_kwargs: object,
) -> WikipediaAgent:
    """Construct an agent from the environment.

    Fails at startup on missing configuration rather than at question time
    (principle #13).
    """
    resolved_contact = contact or os.environ.get("WIKIMEDIA_AGENT_CONTACT", "").strip()
    if not resolved_contact:
        raise ConfigurationError(
            "Set WIKIMEDIA_AGENT_CONTACT to a real email address or project URL. "
            "The Wikimedia API policy requires a contact in the User-Agent, and "
            "blocks non-compliant clients without notice."
        )

    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not resolved_key:
        raise ConfigurationError(
            "Set ANTHROPIC_API_KEY to call the Anthropic API. This is the first "
            "phase that spends money; everything before it runs offline."
        )

    return WikipediaAgent(
        tools=build_tools(resolved_contact, **client_kwargs),
        client=Anthropic(api_key=resolved_key),
        model=model,
        max_iterations=max_iterations,
    )
