"""Serial requests and the minimum interval (§2.1).

The MediaWiki etiquette guidance is explicit: make requests in series, waiting
for one to finish before sending the next. These assert against a fake clock, so
the suite never actually sleeps.
"""

from __future__ import annotations

import threading

from tests.helpers import CONTACT, json_response, recording_transport
from wikimedia_agent.wikipedia import WikipediaClient

SITEINFO = {"query": {"general": {"sitename": "Wikipedia"}}}


def _client(clock, transport, **kwargs):
    return WikipediaClient(
        contact=CONTACT,
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def test_first_request_does_not_wait(clock):
    transport, _ = recording_transport(lambda _r: json_response(SITEINFO))
    with _client(clock, transport) as client:
        client.site_name()
    assert clock.sleeps == []


def test_second_request_waits_the_minimum_interval(clock):
    transport, _ = recording_transport(lambda _r: json_response(SITEINFO))
    with _client(clock, transport, min_interval=1.0) as client:
        client.site_name()
        client.site_name()
    assert clock.sleeps == [1.0]


def test_no_wait_when_enough_time_already_passed(clock):
    """Time spent elsewhere counts toward the interval -- we throttle, not stall."""
    transport, _ = recording_transport(lambda _r: json_response(SITEINFO))
    with _client(clock, transport, min_interval=1.0) as client:
        client.site_name()
        clock.advance(5.0)
        client.site_name()
    assert clock.sleeps == []


def test_partial_wait_when_some_time_passed(clock):
    transport, _ = recording_transport(lambda _r: json_response(SITEINFO))
    with _client(clock, transport, min_interval=2.0) as client:
        client.site_name()
        clock.advance(0.5)
        client.site_name()
    assert clock.sleeps == [1.5]


def test_requests_are_serialized_across_threads(clock):
    """Two threads must not overlap requests: the guidance forbids parallelism."""
    overlapping = []
    in_flight = []
    lock = threading.Lock()

    def handler(_request):
        with lock:
            in_flight.append(1)
            if len(in_flight) > 1:
                overlapping.append(True)
        # Yield the GIL so a genuinely-concurrent implementation would overlap.
        threading.Event().wait(0.01)
        with lock:
            in_flight.pop()
        return json_response(SITEINFO)

    transport, seen = recording_transport(handler)
    with _client(clock, transport, min_interval=0.0) as client:
        threads = [threading.Thread(target=client.site_name) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(seen) == 4
    assert not overlapping, "requests overlapped; the client must be serial"
