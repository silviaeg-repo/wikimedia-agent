"""Run the agent from the command line.

Two modes::

    python -m wikimedia_agent "Who was Ada Lovelace?"   # one question
    python -m wikimedia_agent                            # interactive loop

A deliberate stopgap. The loop asks **independent** questions: there is no
conversation memory yet, so a follow-up like "where was he born?" has nothing to
resolve "he" against. Session history and the article registry arrive in Phase 8,
and the full conversational CLI in Phase 12 (§2.5, §4).
"""

from __future__ import annotations

import sys

from .agent import build_agent
from .errors import ConfigurationError

EXIT_COMMANDS = {"/exit", "/quit", "/q", "exit", "quit"}

BANNER = """\
wikimedia-agent — answers grounded in live Wikipedia, with cited sources.

Each question is answered INDEPENDENTLY: there is no conversation memory yet, so
follow-ups like "where was he born?" will not resolve. That arrives in Phase 8.

Type a question, or /exit to leave. Each question costs roughly a cent.
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
    """A loop of independent questions.

    Deliberately not called a conversation: without the session registry (§2.5)
    nothing carries between turns, and implying otherwise would be worse than
    offering no loop at all.
    """
    print(BANNER)
    while True:
        try:
            question = input("? ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not question:
            continue
        if question.lower() in EXIT_COMMANDS:
            return 0

        _ask_once(agent, question)
        print()


def _ask_once(agent: object, question: str) -> int:
    try:
        answer = agent.ask(question)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 -- a CLI reports, it does not traceback
        print(f"Failed to answer: {exc}", file=sys.stderr)
        return 1

    print(answer.text)

    if answer.sources:
        print("\nSources")
        for index, source in enumerate(answer.sources, start=1):
            grade = answer.grades.get(source.page_id)
            label = grade.label if grade else "Unassessed"
            flag = "  [!] low-quality source" if grade and grade.is_poor else ""
            where = source.title
            if source.section:
                where = f"{where} (section: {source.section})"
            print(f"  [{index}] {where} — {label}{flag}")
            print(f"      {source.article_url}")
    else:
        print("\n(No Wikipedia articles were retrieved for this answer.)")

    print(
        f"\n{answer.input_tokens} input / {answer.output_tokens} output tokens"
        f" · stopped: {answer.stop_reason}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
