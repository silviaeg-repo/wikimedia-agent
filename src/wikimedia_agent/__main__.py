"""``python -m wikimedia_agent`` -- the same entry point as the installed
``wikimedia-agent`` command."""

from __future__ import annotations

from .cli import main

__all__ = ["main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
