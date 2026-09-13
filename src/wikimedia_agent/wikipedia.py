"""The Wikipedia client boundary (§2.1).

All Wikipedia access goes through this module. Nothing above it knows HTTP
exists: callers get typed results and typed exceptions, never a ``Response``, a
status code, or a raw JSON dict.

Phase 1 establishes the request discipline the MediaWiki API asks of clients
(https://www.mediawiki.org/wiki/API:Etiquette):

* a descriptive ``User-Agent`` naming the client and a contact address,
* **serial** requests -- the guidance is explicit that clients should wait for
  one request to finish before sending the next, so there is no concurrency
  here by design,
* explicit connect and read timeouts,
* exponential backoff with jitter, honouring ``Retry-After``,
* MediaWiki's application-level errors mapped to typed exceptions.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable

import httpx

from . import PROJECT_URL, USER_AGENT_NAME, __version__
from .errors import (
    ConfigurationError,
    RateLimited,
    WikipediaAPIError,
    WikipediaTimeout,
)

DEFAULT_API_URL = "https://en.wikipedia.org/w/api.php"

DEFAULT_MIN_INTERVAL = 1.0
"""Seconds between requests. Serial access plus a floor keeps us well inside a
'safe request rate' without needing a number the API does not publish."""

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0
MAX_BACKOFF = 30.0

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_PLACEHOLDER_CONTACT_MARKERS = (
    "example.com",
    "example.org",
    "your-email",
    "youremail",
    "changeme",
    "todo",
    "none",
)


def build_user_agent(contact: str) -> str:
    """Build a policy-compliant User-Agent string.

    The documented shape is
    ``clientname/version (contact information) framework/version``.

    Raises:
        ConfigurationError: if ``contact`` is missing or obviously a placeholder.
            A placeholder contact violates the policy exactly as much as an
            absent one, so this fails at startup rather than at request time.
    """
    cleaned = (contact or "").strip()
    if not cleaned:
        raise ConfigurationError(
            "A contact address is required in the User-Agent. Wikimedia blocks "
            "non-compliant clients by IP without notice -- set WIKIMEDIA_AGENT_CONTACT "
            "to an email address or a project URL you monitor."
        )
    lowered = cleaned.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_CONTACT_MARKERS):
        raise ConfigurationError(
            f"Contact {cleaned!r} looks like a placeholder. Use a real address or URL: "
            "a placeholder User-Agent violates the API policy just as much as none."
        )
    return (
        f"{USER_AGENT_NAME}/{__version__} "
        f"({PROJECT_URL}; {cleaned}) "
        f"httpx/{httpx.__version__}"
    )


class WikipediaClient:
    """A deliberately small, deliberately serial MediaWiki API client.

    The clock and jitter source are injectable so the throttling and backoff
    behaviour can be tested without real sleeps (principle #14).
    """

    def __init__(
        self,
        *,
        contact: str,
        api_url: str = DEFAULT_API_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.user_agent = build_user_agent(contact)
        self.api_url = api_url
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base

        self._sleep = sleep
        self._monotonic = monotonic
        self._jitter = jitter

        # Serial by policy: one lock, held for the whole request, so two threads
        # cannot overlap calls even if a caller tries.
        self._lock = threading.Lock()
        self._last_request_at: float | None = None

        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout,
                                  write=read_timeout, pool=connect_timeout),
            transport=transport,
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WikipediaClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- request path ------------------------------------------------------

    def _wait_for_slot(self) -> None:
        """Enforce the minimum interval between requests."""
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        remaining = self.min_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Exponential backoff with jitter, or ``Retry-After`` when given."""
        if retry_after is not None:
            return min(retry_after, MAX_BACKOFF)
        delay: float = self.backoff_base * (2.0**attempt)
        return min(delay + self._jitter(0.0, self.backoff_base), MAX_BACKOFF)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            # The header also permits an HTTP-date. We do not parse it; falling
            # back to normal backoff is safe and keeps this path simple.
            return None

    def request(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Issue one API request and return the decoded body.

        Raises:
            WikipediaTimeout: connect or read timeout, after retries.
            RateLimited: rate limited, or retries exhausted.
            WikipediaAPIError: MediaWiki reported an application-level error.
        """
        query: dict[str, Any] = {"format": "json", "formatversion": 2, **params}

        with self._lock:
            last_timeout: WikipediaTimeout | None = None
            last_status: int | None = None

            for attempt in range(self.max_retries + 1):
                self._wait_for_slot()
                started = self._monotonic()
                try:
                    response = self._client.get(self.api_url, params=query)
                except httpx.TimeoutException:
                    self._last_request_at = self._monotonic()
                    last_timeout = WikipediaTimeout(self.api_url, self._monotonic() - started)
                    last_status = None
                except httpx.HTTPError as exc:
                    # Connection errors, protocol errors, anything else httpx
                    # raises: never allowed to escape this module.
                    self._last_request_at = self._monotonic()
                    last_timeout = None
                    last_status = None
                    if attempt >= self.max_retries:
                        raise RateLimited(
                            f"Request to {self.api_url} failed after "
                            f"{self.max_retries + 1} attempts: {exc}"
                        ) from exc
                    self._sleep(self._backoff_delay(attempt, None))
                    continue
                else:
                    self._last_request_at = self._monotonic()
                    if response.status_code in RETRYABLE_STATUS:
                        last_status = response.status_code
                        last_timeout = None
                        retry_after = self._parse_retry_after(response)
                        if attempt >= self.max_retries:
                            raise RateLimited(
                                f"Wikipedia returned {response.status_code} after "
                                f"{self.max_retries + 1} attempts",
                                retry_after=retry_after,
                            )
                        self._sleep(self._backoff_delay(attempt, retry_after))
                        continue
                    return self._decode(response)

                # Timeout path: retry or surface the timeout itself.
                if attempt >= self.max_retries:
                    if last_timeout is not None:
                        raise last_timeout
                    raise RateLimited(
                        f"Wikipedia returned {last_status} after "
                        f"{self.max_retries + 1} attempts"
                    )
                self._sleep(self._backoff_delay(attempt, None))

            # Unreachable: the loop either returns or raises.
            raise RateLimited(f"Request to {self.api_url} failed")

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        """Turn a successful HTTP response into a body, or a typed error.

        MediaWiki reports application errors with HTTP 200 plus a
        ``MediaWiki-API-Error`` header and an ``error`` object, so checking the
        status code alone would miss them.
        """
        header_code = response.headers.get("MediaWiki-API-Error")

        try:
            body = response.json()
        except ValueError as exc:
            if header_code:
                raise WikipediaAPIError(header_code) from exc
            raise WikipediaAPIError(
                "invalidresponse", f"Non-JSON response (HTTP {response.status_code})"
            ) from exc

        if not isinstance(body, dict):
            raise WikipediaAPIError("invalidresponse", "Expected a JSON object")

        error = body.get("error")
        if isinstance(error, dict):
            raise WikipediaAPIError(
                str(error.get("code", header_code or "unknown")),
                str(error.get("info", "")),
            )
        if header_code:
            raise WikipediaAPIError(header_code)

        if response.status_code >= 400:
            raise WikipediaAPIError(
                "httperror", f"HTTP {response.status_code} from {self.api_url}"
            )

        return body

    # -- a trivial endpoint, to exercise the discipline above ---------------

    def site_name(self) -> str:
        """Return the wiki's name. Phase 1's smoke endpoint."""
        body = self.request({"action": "query", "meta": "siteinfo"})
        general = body.get("query", {}).get("general", {})
        name = general.get("sitename")
        if not isinstance(name, str):
            raise WikipediaAPIError("invalidresponse", "siteinfo missing 'sitename'")
        return name
