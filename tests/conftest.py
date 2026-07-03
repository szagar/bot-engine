"""Shared fixtures — a minimal host context with in-memory state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from bot_engine.clock import FixedClock
from bot_engine.state import InMemoryStateStore


@dataclass
class StubContext:
    """The smallest possible host context: state + clock, plus a watchlist map."""

    state: InMemoryStateStore = field(default_factory=InMemoryStateStore)
    clock: FixedClock = field(default_factory=lambda: FixedClock(datetime(2026, 7, 3, tzinfo=UTC)))
    watchlists: dict[str, set[str]] = field(default_factory=dict)

    async def get_watchlist_symbols(self, name: str) -> set[str]:
        return self.watchlists.get(name, set())


@pytest.fixture
def ctx() -> StubContext:
    return StubContext()
