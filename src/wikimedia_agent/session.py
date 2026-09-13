"""Multi-turn conversation (§2.5).

Two things carry between turns, and they do different jobs:

* **Message history** is what lets "Where was he born?" resolve. No coreference
  machinery of our own -- the earlier turns go back to the model, which resolves
  pronouns the way it resolves anything else. What that requires from us is that
  earlier turns are never silently dropped: a dropped turn is a pronoun with no
  referent.
* **The article registry** is what keeps citation numbers and quality flags
  stable. An article cited as [1] in turn one is [1] in turn six, and a
  Start-class source stays flagged, because the renderer looks its tier up
  rather than re-deriving or remembering it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from anthropic.types.beta import BetaMessageParam

from .agent import Answer, WikipediaAgent
from .provenance import Grade, Provenance
from .rendering import SourceRegistry

DEFAULT_MAX_HISTORY_TURNS = 20
"""Hard ceiling on exchanges kept (principle #12)."""

DEFAULT_TOKEN_BUDGET = 40_000
"""Approximate ceiling on conversation history, in tokens.

Well under the model's context window: history is only part of what a request
carries, alongside the system prompt, tool definitions and whatever the tools
return this turn. Leaving room for retrieval is the point -- a conversation that
fills the window with its own past cannot read an article."""

COMPACTED_TEMPLATE = "[Earlier answer, shortened to save context{sources}: {opening}...]"
CHARS_PER_TOKEN = 4
"""Rough estimate. Exact counting would cost an API call per turn to save
nothing: the budget is a safety margin, not an accounting figure."""


CITATION_IN_HISTORY = re.compile(r"\[\[([^\]|]+?)\]\]")
"""How the model writes citations (§2.3). History keeps its text verbatim."""


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class RegisteredArticle:
    """What the session remembers about an article, for the whole session."""

    provenance: Provenance
    grade: Grade
    marker: int
    first_turn: int
    sections_fetched: frozenset[str]

    @property
    def is_poor(self) -> bool:
        return self.grade.is_poor


@dataclass
class Session:
    """One conversation.

    Nothing persists across process restarts: cross-session memory is out of
    scope (§1). ``reset`` starts a fresh conversation in the same process.
    """

    agent: WikipediaAgent
    max_turns: int = DEFAULT_MAX_HISTORY_TURNS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    history: list[BetaMessageParam] = field(default_factory=list)
    articles: dict[int, RegisteredArticle] = field(default_factory=dict)
    turn_index: int = 0
    evicted_turns: int = 0
    compacted_turns: int = 0

    # -- asking ------------------------------------------------------------

    def ask(self, question: str) -> Answer:
        """Answer one turn, in the context of everything before it."""
        self.turn_index += 1
        answer = self.agent.ask(question, history=list(self.history), registry=self.registry)

        self._remember(answer)
        self._append_history(question, answer)
        return answer

    def _append_history(self, question: str, answer: Answer) -> None:
        """Record the exchange as plain text.

        Tool calls and their results are deliberately not kept: the history the
        model needs for a follow-up is what was asked and what was answered.
        Retrieved article text would dominate the context within a few turns,
        and re-fetching is served from the client's cache anyway (§2.1).
        """
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer.text or "(no answer)"})
        self._enforce_budget()
        self._enforce_turn_ceiling()

    # -- bounding the context ---------------------------------------------

    @property
    def history_tokens(self) -> int:
        return sum(estimate_tokens(_content_of(message)) for message in self.history)

    def _enforce_budget(self) -> None:
        """Shed bulk from the oldest exchanges until history fits the budget.

        Answers are compacted before exchanges are dropped, and **user turns are
        never compacted**. A user turn is short and is what carries the referent
        for a later pronoun -- shortening "Who was Ben Franklin?" to save a
        handful of tokens would break the follow-up it exists to support (§2.5).
        Answers are the bulk, and a shortened answer still anchors "he".
        """
        for index in range(1, len(self.history), 2):
            if self.history_tokens <= self.token_budget:
                return
            message = self.history[index]
            text = _content_of(message)
            if text.startswith("[Earlier answer"):
                continue
            self.history[index] = {"role": "assistant", "content": _compact(text)}
            self.compacted_turns += 1

        # Still over after compacting everything: drop whole exchanges, oldest
        # first. Registry metadata is untouched, so citations stay correct.
        while self.history_tokens > self.token_budget and len(self.history) > 2:
            del self.history[0:2]
            self.evicted_turns += 1

    def _enforce_turn_ceiling(self) -> None:
        """Drop the oldest exchanges once the ceiling is reached.

        Reported rather than silent: :attr:`evicted_turns` is what the CLI uses
        to tell the user earlier turns are gone (principle #13).
        """
        allowed = self.max_turns * 2
        while len(self.history) > allowed:
            del self.history[0:2]
            self.evicted_turns += 1

    def _remember(self, answer: Answer) -> None:
        """Register everything retrieved this turn, keyed by page id."""
        for provenance in answer.sources:
            grade = answer.grades.get(provenance.page_id, Grade.UNASSESSED)
            citation = self.registry.register(provenance, grade)
            existing = self.articles.get(provenance.page_id)
            sections = set(existing.sections_fetched) if existing else set()
            if provenance.section:
                sections.add(provenance.section)

            self.articles[provenance.page_id] = RegisteredArticle(
                provenance=existing.provenance if existing else provenance,
                # The grade is fixed on first sight and never re-derived, so a
                # warning cannot decay as the conversation grows (§2.3).
                grade=existing.grade if existing else grade,
                marker=citation.number,
                first_turn=existing.first_turn if existing else self.turn_index,
                sections_fetched=frozenset(sections),
            )

    # -- inspection --------------------------------------------------------

    @property
    def known_articles(self) -> list[RegisteredArticle]:
        return sorted(self.articles.values(), key=lambda article: article.marker)

    @property
    def poor_quality_articles(self) -> list[RegisteredArticle]:
        return [article for article in self.known_articles if article.is_poor]

    def marker_for(self, page_id: int) -> int | None:
        article = self.articles.get(page_id)
        return article.marker if article else None

    @property
    def history_note(self) -> str | None:
        """A user-facing note when context has been shed.

        Said out loud rather than done silently: a user who does not know the
        agent has forgotten something cannot tell a lapse from a limit
        (principle #13).
        """
        if not self.evicted_turns and not self.compacted_turns:
            return None

        parts = []
        if self.compacted_turns:
            parts.append(f"{self.compacted_turns} earlier answer(s) shortened")
        if self.evicted_turns:
            parts.append(f"{self.evicted_turns} earliest exchange(s) dropped")
        return (
            f"(Note: this conversation is long, so {' and '.join(parts)} to stay within "
            "context. Sources keep their original numbers, and I can re-read any "
            "article if you want more detail.)"
        )

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Start a fresh conversation: history, registry and numbering."""
        self.history.clear()
        self.articles.clear()
        self.registry = SourceRegistry()
        self.turn_index = 0
        self.evicted_turns = 0
        self.compacted_turns = 0


def _content_of(message: BetaMessageParam) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else str(content)


def _compact(text: str, opening_chars: int = 200) -> str:
    """Shorten an old answer, keeping its opening and the articles it cited.

    History stores the model's own text, where citations are ``[[Title]]`` --
    the rendered ``[1]`` numbering is added afterwards and never fed back, so
    the model always sees its own format. The cited titles are kept because a
    later turn may refer to them, and the opening because it carries the subject
    a follow-up pronoun resolves against.
    """
    titles: list[str] = []
    for match in CITATION_IN_HISTORY.findall(text):
        title = match.split("#", 1)[0].strip()
        if title and title not in titles:
            titles.append(title)

    sources = f" (cited: {', '.join(titles)})" if titles else ""
    opening = " ".join(CITATION_IN_HISTORY.sub("", text[:opening_chars]).split())
    return COMPACTED_TEMPLATE.format(sources=sources, opening=opening)
