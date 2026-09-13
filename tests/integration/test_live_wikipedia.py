"""Phase 1 acceptance check (§5, Layer 2): free, but hits the live API.

Excluded from the default run. Enable with ``pytest -m integration``.
"""

from __future__ import annotations

import os

import pytest

from wikimedia_agent.errors import DisambiguationError, PageNotFound
from wikimedia_agent.wikipedia import MAX_ARTICLE_CHARS, WikipediaClient

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


# -- Phase 2 acceptance checks --------------------------------------------


@requires_contact
def test_stable_article_fetches_and_splits_into_sections():
    """A long-settled article fetches and section-splits correctly."""
    with WikipediaClient(contact=CONTACT) as client:
        article = client.get_article("Ada Lovelace")

    assert article.title == "Ada Lovelace"
    assert article.page_id > 0
    assert "Biography" in article.section_titles
    assert article.truncated is True, "a long article should hit the budget"
    assert len(article.text) <= MAX_ARTICLE_CHARS

    section = client_section("Ada Lovelace", "Death")
    assert section.section_title == "Death"
    assert 0 < len(section.text) < len(article.text)


def client_section(title: str, section: str):
    with WikipediaClient(contact=CONTACT) as client:
        return client.get_article(title, section=section)


@requires_contact
def test_disambiguation_title_raises_with_options():
    """A known disambiguation page raises, carrying candidates to retry with."""
    with WikipediaClient(contact=CONTACT) as client:
        with pytest.raises(DisambiguationError) as excinfo:
            client.get_article("Mercury")

    assert excinfo.value.options, "disambiguation must carry candidate titles"


@requires_contact
def test_redirect_resolves_to_canonical_title():
    with WikipediaClient(contact=CONTACT) as client:
        summary = client.get_summary("Ada Byron")
    assert summary.title == "Ada Lovelace"
    assert summary.redirected_from == "Ada Byron"


@requires_contact
def test_missing_page_raises_page_not_found():
    with WikipediaClient(contact=CONTACT) as client:
        with pytest.raises(PageNotFound):
            client.get_summary("Not A Real Page Xyzzy Qwerty")


@requires_contact
def test_batch_fetches_several_titles_in_one_request():
    with WikipediaClient(contact=CONTACT) as client:
        summaries = client.get_summaries(["Ada Lovelace", "Charles Babbage"])
    assert set(summaries) == {"Ada Lovelace", "Charles Babbage"}
    assert all(s.extract for s in summaries.values())
