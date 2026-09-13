"""Typed errors for the Wikipedia client boundary (§2.1).

Nothing above ``wikipedia.py`` should ever see an ``httpx`` exception. Every
failure leaves the client as one of these, so callers can branch on a type
rather than inspect a status code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import DisambiguationOption


class ConfigurationError(Exception):
    """Raised at startup when the client is misconfigured.

    Deliberately *not* a :class:`WikipediaError`: this is a programmer/operator
    mistake that must crash before any request is made, not a runtime condition
    the agent can report to a user (principle #12).
    """


class WikipediaError(Exception):
    """Base class for every runtime failure reaching a caller."""


class PageNotFound(WikipediaError):
    """No article exists with the requested title."""

    def __init__(self, title: str) -> None:
        super().__init__(f"No Wikipedia article found for {title!r}")
        self.title = title


class DisambiguationError(WikipediaError):
    """The title resolves to a disambiguation page.

    Carries ``options``, each with a human-readable description, so the agent
    can either resolve the ambiguity from conversation context or **ask the user
    which one they meant** -- never guess (§2.3).
    """

    def __init__(self, title: str, options: list[DisambiguationOption]) -> None:
        shown = ", ".join(o.title for o in options[:5]) if options else "no candidates listed"
        super().__init__(f"{title!r} is a disambiguation page ({shown})")
        self.title = title
        self.options = options

    @property
    def titles(self) -> list[str]:
        """Just the candidate titles, for callers that do not need descriptions."""
        return [option.title for option in self.options]


class WikipediaTimeout(WikipediaError):
    """A request exceeded its connect or read timeout."""

    def __init__(self, url: str, elapsed: float) -> None:
        super().__init__(f"Request to {url} timed out after {elapsed:.2f}s")
        self.url = url
        self.elapsed = elapsed


class RateLimited(WikipediaError):
    """Rate limited, or retries exhausted against a failing upstream."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class WikipediaAPIError(WikipediaError):
    """MediaWiki reported an application-level error.

    MediaWiki signals these via the ``MediaWiki-API-Error`` response header and
    an ``error`` object in the body -- often with HTTP 200 -- so status codes
    alone are not enough to detect them.
    """

    def __init__(self, code: str, info: str = "") -> None:
        super().__init__(f"MediaWiki API error {code!r}" + (f": {info}" if info else ""))
        self.code = code
        self.info = info
