"""Response caching (§2.1).

Cuts load on donated infrastructure and makes repeat retrievals within a session
free. Expiry is asserted against the fake clock, so no test sleeps.
"""

from __future__ import annotations

import httpx

from tests.helpers import CONTACT, FakeClock, json_response
from wikimedia_agent.cache import ResponseCache, cache_key
from wikimedia_agent.wikipedia import WikipediaClient

SITEINFO = {"query": {"general": {"sitename": "Wikipedia"}}}


def counting_client(clock, body=SITEINFO, **kwargs):
    calls = []

    def handler(request):
        calls.append(request)
        return json_response(body)

    kwargs.setdefault("min_interval", 0.0)
    client = WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )
    return client, calls


# -- key normalization -----------------------------------------------------


def test_key_is_independent_of_parameter_order():
    """Two callers passing the same params must share an entry, not miss."""
    a = cache_key("https://x/api.php", {"action": "query", "titles": "A"})
    b = cache_key("https://x/api.php", {"titles": "A", "action": "query"})
    assert a == b


def test_key_distinguishes_different_params_and_urls():
    base = cache_key("https://x/api.php", {"titles": "A"})
    assert base != cache_key("https://x/api.php", {"titles": "B"})
    assert base != cache_key("https://y/api.php", {"titles": "A"})


# -- hit / miss / expiry ---------------------------------------------------


def test_hit_and_miss_are_counted():
    clock = FakeClock()
    cache = ResponseCache(ttl=10.0, monotonic=clock.monotonic)
    assert cache.get("k") is None
    cache.put("k", {"v": 1})
    assert cache.get("k") == {"v": 1}
    assert (cache.hits, cache.misses) == (1, 1)


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache = ResponseCache(ttl=10.0, monotonic=clock.monotonic)
    cache.put("k", {"v": 1})
    clock.advance(9.9)
    assert cache.get("k") == {"v": 1}
    clock.advance(0.2)
    assert cache.get("k") is None


def test_expired_entry_is_evicted():
    clock = FakeClock()
    cache = ResponseCache(ttl=1.0, monotonic=clock.monotonic)
    cache.put("k", {"v": 1})
    clock.advance(2.0)
    cache.get("k")
    assert len(cache) == 0


def test_zero_ttl_disables_caching():
    cache = ResponseCache(ttl=0.0)
    cache.put("k", {"v": 1})
    assert cache.get("k") is None
    assert len(cache) == 0


def test_cache_is_bounded_and_evicts_least_recently_used():
    cache = ResponseCache(ttl=100.0, max_entries=2)
    cache.put("a", {"v": 1})
    cache.put("b", {"v": 2})
    cache.get("a")           # 'a' is now the most recently used
    cache.put("c", {"v": 3})  # evicts 'b'
    assert cache.get("b") is None
    assert cache.get("a") == {"v": 1}
    assert len(cache) == 2


# -- integration with the request path -------------------------------------


def test_repeat_request_issues_no_http_call(clock):
    client, calls = counting_client(clock)
    with client:
        assert client.site_name() == "Wikipedia"
        assert client.site_name() == "Wikipedia"
    assert len(calls) == 1, "second call must be served from cache"
    assert client.cache.hits == 1


def test_cache_hit_does_not_consume_the_throttle_interval(clock):
    """A cached read is not a request, so it must not wait."""
    client, _ = counting_client(clock, min_interval=5.0)
    with client:
        client.site_name()
        client.site_name()
    assert clock.sleeps == [], "a cache hit should never sleep"


def test_different_params_are_cached_separately(clock):
    client, calls = counting_client(clock)
    with client:
        client.request({"action": "query", "titles": "A"})
        client.request({"action": "query", "titles": "B"})
        client.request({"action": "query", "titles": "A"})
    assert len(calls) == 2


def test_expired_response_is_refetched(clock):
    client, calls = counting_client(clock, cache_ttl=10.0)
    with client:
        client.site_name()
        clock.advance(11.0)
        client.site_name()
    assert len(calls) == 2


def test_errors_are_not_cached(clock):
    """A failed request must not poison later attempts."""
    responses = [json_response({}, status_code=503), json_response(SITEINFO)]
    state = {"n": 0}

    def handler(request):
        response = responses[min(state["n"], len(responses) - 1)]
        state["n"] += 1
        return response

    client = WikipediaClient(
        contact=CONTACT,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        min_interval=0.0,
        jitter=lambda _a, _b: 0.0,
    )
    with client:
        assert client.site_name() == "Wikipedia"
    assert state["n"] == 2
