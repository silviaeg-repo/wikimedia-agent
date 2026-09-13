"""Ask one question from the command line.

A deliberate stopgap: the conversational CLI with session handling arrives in
Phase 12 (§4). This exists so the agent can be run and seen working today::

    python -m wikimedia_agent "Who was Ada Lovelace?"
"""

from __future__ import annotations

import sys

from .agent import build_agent
from .errors import ConfigurationError


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0 if args else 2

    question = " ".join(args).strip()
    if not question:
        print("Ask a question, e.g. python -m wikimedia_agent \"Who was Ada Lovelace?\"")
        return 2

    try:
        agent = build_agent()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        answer = agent.ask(question)
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
