"""Run the agent from the command line.

Two modes::

    python -m wikimedia_agent "Who was Ada Lovelace?"   # one question
    python -m wikimedia_agent                            # interactive loop

The loop is a conversation: follow-ups resolve against earlier turns, and an
article keeps its citation number for the whole session (§2.5). ``/new`` starts
a fresh one.
"""

from __future__ import annotations

import sys

from .agent import build_agent
from .errors import ConfigurationError

EXIT_COMMANDS = {"/exit", "/quit", "/q", "exit", "quit"}
RESET_COMMANDS = {"/new", "/reset", "/clear"}

BANNER = """\
wikimedia-agent — answers grounded in live Wikipedia, with cited sources.

Follow-up questions resolve against earlier turns, so "Who was Ben Franklin?"
then "Where was he born?" works. Sources keep their numbers for the session.

  /new    start a fresh conversation
  /exit   leave

Each question costs roughly a cent.
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0

    try:
        agent = build_agent()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not args:
        return _interactive(agent)

    question = " ".join(args).strip()
    if not question:
        print("Ask a question, e.g. python -m wikimedia_agent \"Who was Ada Lovelace?\"")
        return 2
    return _ask_once(agent, question)


def _interactive(agent: object) -> int:
    """A conversation. Earlier turns carry, so follow-ups resolve (§2.5)."""
    from .session import Session

    session = Session(agent=agent)  # type: ignore[arg-type]
    print(BANNER)

    while True:
        try:
            question = input("? ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not question:
            continue
        lowered = question.lower()
        if lowered in EXIT_COMMANDS:
            return 0
        if lowered in RESET_COMMANDS:
            session.reset()
            print("Started a fresh conversation. Source numbering restarts.\n")
            continue

        _ask_once(session, question)

        note = session.history_note
        if note:
            print(f"\n{note}")
        print()


def _ask_once(asker: object, question: str) -> int:
    """Ask one question of an agent or a session; both expose ``ask``."""
    try:
        answer = asker.ask(question)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 -- a CLI reports, it does not traceback
        print(f"Failed to answer: {exc}", file=sys.stderr)
        return 1

    # The renderer owns the source list and the quality flags (§2.3), so the
    # CLI prints what it produced rather than assembling its own.
    print(answer.display_text)

    if not answer.sources:
        print("\n(No Wikipedia articles were retrieved for this answer.)")

    print(
        f"\n{answer.input_tokens} input / {answer.output_tokens} output tokens"
        f" · stopped: {answer.stop_reason}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
