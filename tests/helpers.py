"""Shared test helpers.

No test in ``tests/unit`` or ``tests/compliance`` may touch the network. HTTP is
faked with ``httpx.MockTransport`` and time is faked with :class:`FakeClock`, so
the suite stays offline, free and fast (principle #14).
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

CONTACT = "tests@wikimedia-agent.invalid"


class FakeClock:
    """A monotonic clock that only advances when something sleeps.

    Lets the throttling and backoff tests assert on *durations* without the
    suite actually waiting.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        """Move time forward without recording a sleep."""
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


def json_response(
    payload: Any,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status_code, json=payload, headers=headers or {})


class CountingTransport(httpx.MockTransport):
    """Plays back ``responses`` in order, repeating the last, and counts calls."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            index = min(self.call_count, len(responses) - 1)
            self.call_count += 1
            return responses[index]

        super().__init__(handler)


def transport_returning(*responses: httpx.Response) -> CountingTransport:
    return CountingTransport(*responses)


def recording_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that records every request it receives."""
    seen: list[httpx.Request] = []

    def wrapper(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapper), seen
