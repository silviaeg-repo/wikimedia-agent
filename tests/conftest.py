"""Pytest fixtures. Helpers live in ``tests.helpers``."""

from __future__ import annotations

import pytest

from tests.helpers import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
