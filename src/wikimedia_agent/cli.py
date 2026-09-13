"""Command-line interface.

    wikimedia-agent "Who was Ada Lovelace?"   # one question
    wikimedia-agent                            # a conversation

Both forms are also reachable as ``python -m wikimedia_agent``.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .agent import DEFAULT_MAX_ITERATIONS, DEFAULT_MODEL, build_agent
from .errors import ConfigurationError
from .session import DEFAULT_MAX_HISTORY_TURNS, DEFAULT_TOKEN_BUDGET, Session
from .tools import DEFAULT_DEADLINE_SECONDS, DEFAULT_MAX_RETRIEVALS

PROMPT = "> "

EXIT_COMMANDS = {"/exit", "/quit", "/q", "exit", "quit"}
RESET_COMMANDS = {"/new", "/reset", "/clear"}
SOURCE_COMMANDS = {"/sources", "/cited"}
HELP_COMMANDS = {"/help", "/?"}

BANNER = """\
wikimedia-agent — answers grounded in live Wikipedia, with cited sources.

Follow-up questions resolve against earlier turns, so "Who was Ben Franklin?"
then "Where was he born?" works. Sources keep their numbers for the session.

  /sources   list every article read so far, with its quality rating
  /new       start a fresh conversation
  /help      show these commands
  /exit      leave

Each question costs roughly a cent.
"""

COMMAND_HELP = """\
  /sources   every article read so far, with its quality rating (free)
  /new       start a fresh conversation; source numbering restarts
  /help      this list
  /exit      leave (Ctrl-D and Ctrl-C also work)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wikimedia-agent",
        description=(
            "Answer questions from live Wikipedia, with cited, quality-graded sources. "
            "With no question, starts an interactive conversation."
        ),
        epilog=(
            "Requires WIKIMEDIA_AGENT_CONTACT (a real address, required by Wikimedia "
            "policy) and ANTHROPIC_API_KEY."
        ),
    )
    parser.add_argument("question", nargs="*", help="the question to answer")
    parser.add_argument("--version", action="version", version=f"wikimedia-agent {__version__}")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Anthropic model for the agent (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--contact", default=None,
        help="contact address for the Wikipedia User-Agent (default: $WIKIMEDIA_AGENT_CONTACT)",
    )
    parser.add_argument(
        "--max-retrievals", type=int, default=DEFAULT_MAX_RETRIEVALS,
        help=f"articles one question may read (default: {DEFAULT_MAX_RETRIEVALS})",
    )
    parser.add_argument(
        "--deadline", type=float, default=DEFAULT_DEADLINE_SECONDS,
        help=f"seconds of retrieval per question (default: {DEFAULT_DEADLINE_SECONDS:.0f})",
    )
    parser.add_argument(
        "--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET,
        help=f"approximate conversation history ceiling (default: {DEFAULT_TOKEN_BUDGET})",
    )
    parser.add_argument(
        "--max-turns", type=int, default=DEFAULT_MAX_HISTORY_TURNS,
        help=f"exchanges kept in a conversation (default: {DEFAULT_MAX_HISTORY_TURNS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        agent = build_agent(
            contact=args.contact,
            model=args.model,
            max_iterations=DEFAULT_MAX_ITERATIONS,
        )
    except ConfigurationError as exc:
        # A misconfiguration is an operator mistake, reported plainly at startup
        # rather than surfacing as a traceback mid-question (principle #13).
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    agent.tools.budget.max_retrievals = args.max_retrievals
    agent.tools.budget.deadline_seconds = args.deadline

    question = " ".join(args.question).strip()
    if question:
        return ask_once(agent, question)

    session = Session(
        agent=agent, max_turns=args.max_turns, token_budget=args.token_budget
    )
    return interactive(session)


def interactive(session: Session) -> int:
    """A conversation. Earlier turns carry, so follow-ups resolve (§2.5)."""
    print(BANNER)
    while True:
        try:
            line = input(PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        lowered = line.lower()
        if lowered in EXIT_COMMANDS:
            return 0
        if lowered in HELP_COMMANDS:
            print(COMMAND_HELP)
            continue
        if lowered in SOURCE_COMMANDS:
            print_sources(session)
            continue
        if lowered in RESET_COMMANDS:
            session.reset()
            print("Started a fresh conversation. Source numbering restarts.\n")
            continue

        ask_once(session, line)

        note = session.history_note
        if note:
            print(f"\n{note}")
        print()


def print_sources(session: Session) -> None:
    """Everything the conversation has read, from the session registry (§2.5)."""
    articles = session.known_articles
    if not articles:
        print("No articles have been read in this conversation yet.\n")
        return

    print("Articles used in this conversation:")
    for article in articles:
        flag = "  [!] low-quality source" if article.is_poor else ""
        print(f"  [{article.marker}] {article.provenance.title} — "
              f"{article.grade.label}-class{flag}")
        print(f"      {article.provenance.article_url}")
    print()


def ask_once(asker: object, question: str) -> int:
    """Ask one question of an agent or a session; both expose ``ask``."""
    try:
        answer = asker.ask(question)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 -- a CLI reports, it does not traceback
        print(f"Failed to answer: {exc}", file=sys.stderr)
        return 1

    # The renderer owns the source list and the quality flags (§2.3), so the CLI
    # prints what it produced rather than assembling its own.
    print(answer.display_text)

    # Based on what the answer actually shows, not on this turn's retrievals: a
    # follow-up may legitimately cite an article read earlier without fetching
    # anything, and printing "nothing retrieved" above a source list is a
    # contradiction the reader has to resolve.
    if answer.rendered is not None and not answer.rendered.citations:
        print("\n(No Wikipedia articles were used for this answer.)")

    print(
        f"\n{answer.input_tokens} input / {answer.output_tokens} output tokens"
        f" · stopped: {answer.stop_reason}"
    )
    return 0
