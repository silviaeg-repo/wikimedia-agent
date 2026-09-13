"""User-Agent policy (§2.1).

Wikimedia blocks non-compliant clients by IP without notice, so these are
correctness tests, not style tests.
"""

from __future__ import annotations

import re

import pytest

from tests.helpers import CONTACT, json_response, recording_transport
from wikimedia_agent.errors import ConfigurationError
from wikimedia_agent.wikipedia import WikipediaClient, build_user_agent

UA_SHAPE = re.compile(r"^wikimedia-agent/\d+\.\d+\.\d+ \(.+\) httpx/\S+$")


def test_user_agent_matches_documented_shape():
    ua = build_user_agent(CONTACT)
    assert UA_SHAPE.match(ua), ua
    assert CONTACT in ua


@pytest.mark.parametrize("contact", ["", "   ", None])
def test_missing_contact_fails_at_startup(contact):
    with pytest.raises(ConfigurationError, match="contact address is required"):
        build_user_agent(contact)


@pytest.mark.parametrize(
    "contact",
    ["you@example.com", "TODO", "changeme@somewhere.net", "your-email@here"],
)
def test_placeholder_contact_fails_at_startup(contact):
    """A placeholder UA violates the policy exactly as much as no UA."""
    with pytest.raises(ConfigurationError, match="placeholder"):
        build_user_agent(contact)


def test_client_construction_fails_before_any_request():
    with pytest.raises(ConfigurationError):
        WikipediaClient(contact="")


def test_every_request_carries_the_user_agent(clock):
    transport, seen = recording_transport(
        lambda _r: json_response({"query": {"general": {"sitename": "Wikipedia"}}})
    )
    with WikipediaClient(
        contact=CONTACT, transport=transport, sleep=clock.sleep, monotonic=clock.monotonic
    ) as client:
        client.request({"action": "query", "probe": 1})
        client.request({"action": "query", "probe": 2})

    assert len(seen) == 2
    for request in seen:
        assert request.headers["User-Agent"] == client.user_agent
        assert UA_SHAPE.match(request.headers["User-Agent"])
