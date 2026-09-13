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


# -- Anthropic API fixtures (§2.2) ----------------------------------------
#
# The real tool_runner is driven against recorded API responses through a mock
# transport, so the agent loop is exercised offline and for free.


def assistant_message(
    *,
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    message_id: str = "msg_test",
) -> dict[str, Any]:
    """A Messages API response body."""
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use_block(name: str, tool_input: dict[str, Any], block_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


class RecordedAnthropic:
    """Serves scripted Messages API responses and records the requests sent.

    ``requests`` holds the decoded request body of every call, so tests can
    assert on what the loop actually put on the wire -- how tool results were
    batched, what was echoed back, which tools were declared.
    """

    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        self.requests.append(_json.loads(request.content))
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return httpx.Response(200, json=self._responses[index])

    def client(self) -> Any:
        from anthropic import Anthropic

        return Anthropic(
            api_key="test-key-not-real",
            http_client=httpx.Client(transport=httpx.MockTransport(self._handler)),
            max_retries=0,
        )

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def tools_sent(self, index: int = 0) -> list[dict[str, Any]]:
        return list(self.requests[index].get("tools", []))

    def messages_sent(self, index: int) -> list[dict[str, Any]]:
        return list(self.requests[index].get("messages", []))
