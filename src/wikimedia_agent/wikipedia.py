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

import html
import random
import re
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from . import PROJECT_URL, USER_AGENT_NAME, __version__
from .cache import DEFAULT_TTL, ResponseCache, cache_key
from .errors import (
    ConfigurationError,
    DisambiguationError,
    PageNotFound,
    RateLimited,
    WikipediaAPIError,
    WikipediaError,
    WikipediaTimeout,
)
from .models import (
    Article,
    DisambiguationOption,
    SearchResult,
    Summary,
    split_sections,
    strip_html,
)
from .provenance import (
    Grade,
    Provenance,
    article_url,
    collect_importance,
    parse_timestamp,
    permalink,
    resolve_grade,
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

# -- bounds (§2.1). Enforced here, below the tool layer, so no tool argument --
# -- and no prompt-injected value can raise them (principle #16).           --

DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 20
"""The API permits 500 results. Past the first handful it is context-window
spend with no answer value, so we cap far lower."""

MAX_TITLES_PER_REQUEST = 50
"""The documented multivalue limit for clients without `apihighlimits`."""

MAX_ARTICLE_CHARS = 24_000
"""Whole-article truncation budget, roughly 6k tokens. Section-scoped fetching
is the intended path; this stops an unscoped fetch from flooding the context."""

MAX_DISAMBIGUATION_OPTIONS = 25
"""Enough candidates to disambiguate, few enough to put in a question."""

_LIST_ITEM = re.compile(r"<li\b[^>]*>(.*?)</li>", re.DOTALL)
_ARTICLE_LINK = re.compile(r'<a href="/wiki/([^"#]+)"')
_NAMESPACED = re.compile(
    r"^(Special|Help|Category|Wikipedia|File|Template|Portal|Talk|Module):",
)


def _merge_assessments(body: dict[str, Any], more: dict[str, Any]) -> None:
    """Fold a continuation response's assessments into the first response."""
    existing = {
        str(page.get("title")): page
        for page in body.get("query", {}).get("pages", []) or []
        if isinstance(page, dict)
    }
    for page in more.get("query", {}).get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        target = existing.get(str(page.get("title")))
        extra = page.get("pageassessments")
        if target is None or not isinstance(extra, dict):
            continue
        merged = dict(target.get("pageassessments") or {})
        merged.update(extra)
        target["pageassessments"] = merged


def _describe(item_text: str, title: str) -> str:
    """Turn a list item into a short description of its candidate.

    Entries read "Mercury (planet), the closest planet to the Sun", so dropping
    the leading title leaves the part a user actually needs to choose.
    """
    text = item_text.strip()
    if text.startswith(title):
        text = text[len(title):]
    return text.lstrip(" ,;:-\u2013\u2014").strip()

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
        cache_ttl: float = DEFAULT_TTL,
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
        self.cache = ResponseCache(ttl=cache_ttl, monotonic=monotonic)

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

        key = cache_key(self.api_url, query)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

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
                    body = self._decode(response)
                    self.cache.put(key, body)
                    return body

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

    # -- retrieval ---------------------------------------------------------

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[SearchResult]:
        """Search Wikipedia and return candidate articles.

        ``limit`` is clamped to :data:`MAX_SEARCH_LIMIT` here rather than
        validated by the caller: bounds belong below the tool layer, where no
        argument the model chooses can widen them (principle #16).
        """
        if not query.strip():
            raise ValueError("search query must not be empty")

        effective = max(1, min(limit, MAX_SEARCH_LIMIT))
        body = self.request(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": effective,
            }
        )
        hits = body.get("query", {}).get("search", [])
        if not isinstance(hits, list):
            raise WikipediaAPIError("invalidresponse", "search results missing")

        return [
            SearchResult(
                title=str(hit.get("title", "")),
                page_id=int(hit.get("pageid", 0)),
                snippet=strip_html(str(hit.get("snippet", ""))),
                word_count=int(hit.get("wordcount", 0)),
            )
            for hit in hits
        ]

    def _fetch_pages(self, titles: Sequence[str], *, intro_only: bool) -> dict[str, Any]:
        """Fetch one or more pages in a single request.

        Multivalue parameters are the sanctioned way to go faster: several
        titles in one call, rather than several calls in parallel (§2.1).
        """
        if not titles:
            raise ValueError("at least one title is required")
        if len(titles) > MAX_TITLES_PER_REQUEST:
            raise ValueError(
                f"at most {MAX_TITLES_PER_REQUEST} titles per request, got {len(titles)}"
            )

        params: dict[str, Any] = {
            "action": "query",
            # Content, revision and quality grade in a single request: three
            # facts we always need together, and one request instead of three
            # (§2.1 -- batch, do not parallelise).
            "prop": "extracts|pageprops|revisions|pageassessments",
            "explaintext": 1,
            "redirects": 1,
            "rvprop": "ids|timestamp",
            "palimit": "max",
            "titles": "|".join(titles),
        }
        if intro_only:
            params["exintro"] = 1

        body = self.request(params)
        return self._follow_assessment_pages(params, body)

    def _follow_assessment_pages(
        self, params: dict[str, Any], body: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge any continued `pageassessments` pages into ``body``.

        Assessments paginate independently of the rest of the response: a page
        with many WikiProjects returns a ``pacontinue`` token, and stopping at
        the first page would silently drop projects -- which changes the
        lowest-grade fallback in :func:`resolve_grade`.
        """
        continuation = body.get("continue")
        guard = 0
        while isinstance(continuation, dict) and "pacontinue" in continuation and guard < 10:
            guard += 1
            next_params = {**params, "pacontinue": continuation["pacontinue"],
                           "continue": continuation.get("continue", "")}
            more = self.request(next_params)
            _merge_assessments(body, more)
            continuation = more.get("continue")
        return body

    @staticmethod
    def _redirect_map(body: dict[str, Any]) -> dict[str, str]:
        """Map final title -> the title originally asked for.

        Covers both ``normalized`` (case/underscore fixes) and ``redirects``
        (actual page redirects), so ``ada_byron`` traces back correctly.
        """
        query = body.get("query", {})
        origin: dict[str, str] = {}
        for entry in query.get("normalized", []) or []:
            origin[str(entry.get("to"))] = str(entry.get("from"))
        for entry in query.get("redirects", []) or []:
            source = str(entry.get("from"))
            origin[str(entry.get("to"))] = origin.get(source, source)
        return origin

    def _page_or_raise(self, body: dict[str, Any], requested: str) -> dict[str, Any]:
        """Pull a single page out of a response, turning absence into typed errors."""
        pages = body.get("query", {}).get("pages", [])
        if not isinstance(pages, list) or not pages:
            raise PageNotFound(requested)

        page = pages[0]
        if not isinstance(page, dict):
            raise WikipediaAPIError("invalidresponse", "page entry was not an object")
        title = str(page.get("title", requested))

        if page.get("missing"):
            raise PageNotFound(requested)

        pageprops = page.get("pageprops") or {}
        if "disambiguation" in pageprops:
            raise DisambiguationError(title, self._disambiguation_options(title))

        return page

    def _disambiguation_options(self, title: str) -> list[DisambiguationOption]:
        """Candidate articles a disambiguation page points at, with descriptions.

        Uses ``action=parse`` and reads the first article link out of each list
        item. ``prop=links`` would be one obvious alternative, but it returns
        every link on the page in alphabetical order -- for "Mercury" that
        yields "Anna Kavan" before "Mercury (planet)". Parsing the list items
        preserves the page's own ordering and, crucially, keeps each entry's
        description, which is what makes a clarifying question to the user
        useful rather than a wall of bare titles (§2.3).

        Best-effort: a failure here must not mask the disambiguation itself.
        """
        try:
            body = self.request({"action": "parse", "page": title, "prop": "text"})
        except WikipediaError:
            return []

        markup = body.get("parse", {}).get("text", "")
        if not isinstance(markup, str):
            return []

        options: list[DisambiguationOption] = []
        seen: set[str] = set()

        for item in _LIST_ITEM.findall(markup):
            match = _ARTICLE_LINK.search(item)
            if match is None:
                continue
            candidate = html.unescape(match.group(1)).replace("_", " ")
            if _NAMESPACED.match(candidate) or candidate in seen:
                continue
            seen.add(candidate)
            options.append(
                DisambiguationOption(
                    title=candidate,
                    description=_describe(strip_html(item), candidate),
                )
            )
            if len(options) >= MAX_DISAMBIGUATION_OPTIONS:
                break
        return options

    def _provenance(
        self,
        page: Mapping[str, Any],
        *,
        requested_title: str,
        redirected_from: str | None,
        section: str | None = None,
    ) -> Provenance:
        """Build a Provenance record from API metadata only.

        Nothing here reads article text: a page whose body claims a revision
        number or a Featured rating changes neither field (§2.3).
        """
        revisions = page.get("revisions") or [{}]
        revision = revisions[0] if isinstance(revisions, list) and revisions else {}
        revision_id = int(revision.get("revid", 0) or 0)
        title = str(page.get("title", requested_title))

        return Provenance(
            title=title,
            page_id=int(page.get("pageid", 0) or 0),
            revision_id=revision_id,
            article_url=article_url(self.api_url, title),
            permalink=permalink(self.api_url, revision_id),
            retrieved_at=datetime.now(timezone.utc),
            revision_timestamp=parse_timestamp(str(revision.get("timestamp", ""))),
            requested_title=requested_title,
            redirected_from=redirected_from,
            section=section,
        )

    @staticmethod
    def _quality(page: Mapping[str, Any]) -> tuple[Grade, dict[str, str]]:
        assessments = page.get("pageassessments") or {}
        if not isinstance(assessments, Mapping):
            return Grade.UNASSESSED, {}
        return resolve_grade(assessments), collect_importance(assessments)

    def get_summary(self, title: str) -> Summary:
        """Fetch an article's lead extract.

        Cheap disambiguation: read the lead before deciding whether to spend
        context on the full article.
        """
        body = self._fetch_pages([title], intro_only=True)
        page = self._page_or_raise(body, title)
        resolved = str(page.get("title", title))
        redirected_from = self._redirect_map(body).get(resolved)
        grade, importance = self._quality(page)
        return Summary(
            title=resolved,
            page_id=int(page.get("pageid", 0)),
            extract=str(page.get("extract", "")).strip(),
            requested_title=title,
            provenance=self._provenance(
                page, requested_title=title, redirected_from=redirected_from
            ),
            grade=grade,
            importance=importance,
            redirected_from=redirected_from,
        )

    def get_article(self, title: str, section: str | None = None) -> Article:
        """Fetch an article, or one section of it.

        Section-scoped by preference: whole-article text is truncated at
        :data:`MAX_ARTICLE_CHARS` so an unscoped fetch cannot flood the context
        window. Truncation is reported on the result, never silent (principle #12).
        """
        body = self._fetch_pages([title], intro_only=False)
        page = self._page_or_raise(body, title)

        resolved = str(page.get("title", title))
        extract = str(page.get("extract", ""))
        sections = split_sections(extract)
        redirected_from = self._redirect_map(body).get(resolved)
        grade, importance = self._quality(page)

        if section is not None:
            found = None
            for candidate in sections:
                if candidate.title.casefold() == section.strip().casefold():
                    found = candidate
                    break
            if found is None:
                available = ", ".join(s.title for s in sections[:10]) or "none"
                raise WikipediaAPIError(
                    "nosuchsection",
                    f"{resolved!r} has no section {section!r} (available: {available})",
                )
            return Article(
                title=resolved,
                page_id=int(page.get("pageid", 0)),
                text=found.text,
                sections=sections,
                requested_title=title,
                provenance=self._provenance(
                    page,
                    requested_title=title,
                    redirected_from=redirected_from,
                    section=found.title,
                ),
                grade=grade,
                importance=importance,
                redirected_from=redirected_from,
                section_title=found.title,
            )

        truncated = len(extract) > MAX_ARTICLE_CHARS
        return Article(
            title=resolved,
            page_id=int(page.get("pageid", 0)),
            text=extract[:MAX_ARTICLE_CHARS].strip(),
            sections=sections,
            requested_title=title,
            provenance=self._provenance(
                page, requested_title=title, redirected_from=redirected_from
            ),
            grade=grade,
            importance=importance,
            redirected_from=redirected_from,
            truncated=truncated,
        )

    def get_summaries(self, titles: Sequence[str]) -> dict[str, Summary]:
        """Fetch several lead extracts in one request.

        Batching, not concurrency: this is how we go faster without violating
        the serial-request guidance (§2.1). Missing and disambiguation pages are
        skipped rather than raising, since one bad title should not lose the rest.
        """
        body = self._fetch_pages(list(titles), intro_only=True)
        origin = self._redirect_map(body)
        results: dict[str, Summary] = {}

        for page in body.get("query", {}).get("pages", []) or []:
            if page.get("missing") or "disambiguation" in (page.get("pageprops") or {}):
                continue
            resolved = str(page.get("title", ""))
            requested = origin.get(resolved, resolved)
            grade, importance = self._quality(page)
            results[resolved] = Summary(
                title=resolved,
                page_id=int(page.get("pageid", 0)),
                extract=str(page.get("extract", "")).strip(),
                requested_title=requested,
                provenance=self._provenance(
                    page, requested_title=requested, redirected_from=origin.get(resolved)
                ),
                grade=grade,
                importance=importance,
                redirected_from=origin.get(resolved),
            )
        return results

    # -- a trivial endpoint, to exercise the discipline above ---------------

    def site_name(self) -> str:
        """Return the wiki's name. Phase 1's smoke endpoint."""
        body = self.request({"action": "query", "meta": "siteinfo"})
        general = body.get("query", {}).get("general", {})
        name = general.get("sitename")
        if not isinstance(name, str):
            raise WikipediaAPIError("invalidresponse", "siteinfo missing 'sitename'")
        return name
