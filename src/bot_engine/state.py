"""State store implementations.

The engine ships an in-memory store for tests and simple hosts. Production
hosts implement :class:`bot_engine.ports.StateStore` over their own
infrastructure (Redis fast path + database durable store is a common shape).
"""

from __future__ import annotations

from typing import Any


class InMemoryStateStore:
    """Dict-backed :class:`~bot_engine.ports.StateStore`. Not persistent."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get(self, bot_name: str, key: str, default: Any = None) -> Any:
        return self._data.get(bot_name, {}).get(key, default)

    async def set(self, bot_name: str, key: str, value: Any) -> None:
        self._data.setdefault(bot_name, {})[key] = value

    async def delete(self, bot_name: str, key: str) -> None:
        self._data.get(bot_name, {}).pop(key, None)

    async def clear_all(self, bot_name: str) -> None:
        self._data.pop(bot_name, None)
