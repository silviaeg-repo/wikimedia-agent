"""Timeouts, backoff and retry bounds (§2.1)."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers import CONTACT, json_response, recording_transport, transport_returning
from wikimedia_agent.errors import RateLimited, WikipediaError, WikipediaTimeout
from wikimedia_agent.wikipedia import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    WikipediaClient,
)

SITEINFO = {"query": {"general": {"sitename": "Wikipedia"}}}


def _client(clock, transport, **kwargs):
    kwargs.setdefault("min_interval", 0.0)
    kwargs.setdefault("jitter", lambda _a, _b: 0.0)  # deterministic backoff
    return WikipediaClient(
        contact=CONTACT,
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def test_timeouts_are_explicit_not_library_defaults(clock):
    transport, _ = recording_transport(lambda _r: json_response(SITEINFO))
    with _client(clock, transport) as client:
        timeout = client._client.timeout
    assert timeout.connect == DEFAULT_CONNECT_TIMEOUT
    assert timeout.read == DEFAULT_READ_TIMEOUT
    assert timeout.connect is not None and timeout.read is not None


def test_timeout_raises_wikipedia_timeout_after_retries(clock):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    transport, seen = recording_transport(handler)
    with _client(clock, transport, max_retries=2) as client:
        with pytest.raises(WikipediaTimeout) as excinfo:
            client.site_name()

    assert len(seen) == 3, "one initial attempt plus two retries"
    assert excinfo.value.url.endswith("api.php")


def test_retry_after_is_honoured_on_429(clock):
    responses = [
        json_response({}, status_code=429, headers={"Retry-After": "7"}),
        json_response(SITEINFO),
    ]
    with _client(clock, transport_returning(*responses)) as client:
        assert client.site_name() == "Wikipedia"
    assert clock.sleeps == [7.0]


def test_backoff_is_exponential_when_no_retry_after(clock):
    error = json_response({}, status_code=503)
    with _client(clock, transport_returning(error), max_retries=3, backoff_base=1.0) as client:
        with pytest.raises(RateLimited):
            client.site_name()
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_jitter_is_added_to_backoff(clock):
    error = json_response({}, status_code=503)
    with _client(
        clock,
        transport_returning(error),
        max_retries=1,
        backoff_base=1.0,
        jitter=lambda _a, _b: 0.25,
    ) as client:
        with pytest.raises(RateLimited):
            client.site_name()
    assert clock.sleeps == [1.25]


def test_retries_are_bounded_and_surface_rate_limited(clock):
    error = json_response({}, status_code=429)
    transport = transport_returning(error)
    with _client(clock, transport, max_retries=2) as client:
        with pytest.raises(RateLimited, match="429"):
            client.site_name()
    assert transport.call_count == 3


def test_retryable_status_eventually_succeeds(clock):
    responses = [
        json_response({}, status_code=503),
        json_response({}, status_code=503),
        json_response(SITEINFO),
    ]
    with _client(clock, transport_returning(*responses)) as client:
        assert client.site_name() == "Wikipedia"


def test_connection_error_never_escapes_as_httpx(clock):
    """Principle #9: no httpx type crosses the boundary."""

    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    transport, _ = recording_transport(handler)
    with _client(clock, transport, max_retries=1) as client:
        with pytest.raises(WikipediaError) as excinfo:
            client.site_name()
    assert not isinstance(excinfo.value, httpx.HTTPError)
