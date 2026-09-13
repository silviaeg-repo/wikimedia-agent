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

from dataclasses import dataclass, field

from anthropic.types.beta import BetaMessageParam

from .agent import Answer, WikipediaAgent
from .provenance import Grade, Provenance
from .rendering import SourceRegistry

DEFAULT_MAX_HISTORY_TURNS = 20
"""Turn ceiling per session (principle #12). Phase 9 adds token-budgeted
eviction of article bodies; this is the cruder bound that comes first."""


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
    registry: SourceRegistry = field(default_factory=SourceRegistry)
    history: list[BetaMessageParam] = field(default_factory=list)
    articles: dict[int, RegisteredArticle] = field(default_factory=dict)
    turn_index: int = 0
    evicted_turns: int = 0

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
        self._enforce_turn_ceiling()

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
        """A user-facing note when earlier turns have been dropped."""
        if not self.evicted_turns:
            return None
        return (
            f"(Note: this conversation is long, so the earliest {self.evicted_turns} "
            "exchange(s) are no longer in context. Sources remain cited by their "
            "original numbers.)"
        )

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Start a fresh conversation: history, registry and numbering."""
        self.history.clear()
        self.articles.clear()
        self.registry = SourceRegistry()
        self.turn_index = 0
        self.evicted_turns = 0
