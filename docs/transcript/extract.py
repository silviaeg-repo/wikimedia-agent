#!/usr/bin/env python3
"""Extract a readable transcript from a Claude Code session JSONL file.

    python3 docs/transcript/extract.py SESSION.jsonl > discussion.md
    python3 docs/transcript/extract.py SESSION.jsonl --prose > discussion.md

Claude Code stores each session as JSONL under
``~/.claude/projects/<project-path-slug>/``. This project's slug is
``-Users-<you>-Documents-Codex-wikimedia-agent``.

Default output keeps the conversation and names the tools that ran, without
their arguments or output. ``--prose`` drops tool mentions entirely, leaving
only what was said.

The raw JSONL also contains file contents, command output and API responses.
This extractor deliberately takes none of that -- only the prose -- so the
result is safe to commit.
"""
from __future__ import annotations

import json
import sys


def text_of(content) -> tuple[str, list[str]]:
    """Return (spoken text, names of tools used) for one message."""
    if isinstance(content, str):
        return content, []
    parts, tools = [], []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            tools.append(block.get("name", "?"))
    return "\n".join(p for p in parts if p).strip(), tools


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = sys.argv[1]
    prose_only = "--prose" in sys.argv

    print("# Project discussion transcript\n")
    with open(path) as handle:
        records = handle.readlines()

    for line in records:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") not in {"user", "assistant"}:
            continue

        message = record.get("message") or {}
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue

        spoken, tools = text_of(message.get("content"))
        # Tool results come back as role=user with no prose; skip those.
        if not spoken and not (tools and not prose_only):
            continue

        print(f"## {'You' if role == 'user' else 'Claude'}\n")
        if spoken:
            print(spoken + "\n")
        if tools and not prose_only:
            unique = sorted(set(tools))
            print(f"*(ran: {', '.join(unique)})*\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Piping into head/less closes the pipe early; that is not an error.
        sys.stderr.close()
        raise SystemExit(0) from None
