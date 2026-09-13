"""Phase 1 acceptance check (§5, Layer 2): free, but hits the live API.

Excluded from the default run. Enable with ``pytest -m integration``.
"""

from __future__ import annotations

import os

import pytest

from wikimedia_agent.wikipedia import WikipediaClient

pytestmark = pytest.mark.integration

CONTACT = os.environ.get("WIKIMEDIA_AGENT_CONTACT", "").strip()

requires_contact = pytest.mark.skipif(
    not CONTACT,
    reason="Set WIKIMEDIA_AGENT_CONTACT to a real address to run live API tests.",
)


@requires_contact
def test_live_request_succeeds_with_our_user_agent():
    """One real request, with the UA we claim to send."""
    with WikipediaClient(contact=CONTACT) as client:
        assert client.site_name() == "Wikipedia"


@requires_contact
def test_live_api_error_is_typed():
    """A deliberately bad request comes back as WikipediaAPIError, not a crash."""
    from wikimedia_agent.errors import WikipediaAPIError

    with WikipediaClient(contact=CONTACT) as client:
        with pytest.raises(WikipediaAPIError):
            client.request({"action": "definitely-not-an-action"})
