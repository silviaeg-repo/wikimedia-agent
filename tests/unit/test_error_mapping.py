"""MediaWiki application errors map to typed exceptions (§2.1).

MediaWiki reports these with HTTP 200 plus a ``MediaWiki-API-Error`` header and
an ``error`` object, so a client that only inspects status codes misses them
entirely.
"""

from __future__ import annotations

import httpx
import pytest

from tests.helpers import CONTACT, json_response, transport_returning
from wikimedia_agent.errors import WikipediaAPIError, WikipediaError
from wikimedia_agent.wikipedia import WikipediaClient


def _client(clock, transport):
    return WikipediaClient(
        contact=CONTACT,
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        min_interval=0.0,
    )


def test_error_object_in_body_maps_to_api_error(clock):
    body = {"error": {"code": "nosuchaction", "info": "Unrecognized value"}}
    with _client(clock, transport_returning(json_response(body))) as client:
        with pytest.raises(WikipediaAPIError) as excinfo:
            client.site_name()
    assert excinfo.value.code == "nosuchaction"
    assert "Unrecognized value" in excinfo.value.info


def test_mediawiki_api_error_header_maps_even_without_body_error(clock):
    """The header alone is enough -- HTTP 200 with no error object still fails."""
    response = json_response({"query": {}}, headers={"MediaWiki-API-Error": "badvalue"})
    with _client(clock, transport_returning(response)) as client:
        with pytest.raises(WikipediaAPIError) as excinfo:
            client.site_name()
    assert excinfo.value.code == "badvalue"


def test_non_json_response_maps_to_api_error(clock):
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>nope</html>"))
    with _client(clock, transport) as client:
        with pytest.raises(WikipediaAPIError) as excinfo:
            client.site_name()
    assert excinfo.value.code == "invalidresponse"


def test_non_object_json_maps_to_api_error(clock):
    with _client(clock, transport_returning(json_response(["unexpected"]))) as client:
        with pytest.raises(WikipediaAPIError):
            client.site_name()


def test_non_retryable_http_error_maps_to_api_error(clock):
    """404 is not retryable and must not be mistaken for a rate limit."""
    with _client(clock, transport_returning(json_response({}, status_code=404))) as client:
        with pytest.raises(WikipediaAPIError) as excinfo:
            client.site_name()
    assert excinfo.value.code == "httperror"


def test_malformed_siteinfo_is_reported_not_returned_as_none(clock):
    """Principle #12: degrade honestly rather than returning something empty."""
    with _client(clock, transport_returning(json_response({"query": {}}))) as client:
        with pytest.raises(WikipediaAPIError, match="sitename"):
            client.site_name()


def test_every_failure_is_a_wikipedia_error(clock):
    """One base class callers can catch."""
    bodies: list[object] = [{"error": {"code": "x"}}, ["bad"], {"query": {}}]
    for body in bodies:
        with _client(clock, transport_returning(json_response(body))) as client:
            with pytest.raises(WikipediaError):
                client.site_name()


def test_successful_request_returns_decoded_body(clock):
    body = {"query": {"general": {"sitename": "Wikipedia"}}}
    with _client(clock, transport_returning(json_response(body))) as client:
        assert client.site_name() == "Wikipedia"


def test_format_params_are_always_sent(clock):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return json_response({"query": {"general": {"sitename": "Wikipedia"}}})

    with _client(clock, httpx.MockTransport(handler)) as client:
        client.site_name()

    assert seen["format"] == "json"
    assert seen["formatversion"] == "2"
    assert seen["action"] == "query"
