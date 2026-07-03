"""Ports — the protocols a host project implements to run bots.

The engine owns the bot *lifecycle* (loading, parameters, execute/skip/error
semantics, scheduling). Everything domain-specific — market data, order
submission, position lookups — lives on the host's context object. The engine
itself requires only two capabilities on that context: a :class:`StateStore`
and a :class:`Clock`.

A host context is any object satisfying :class:`BotContext`::

    @dataclass
    class MyContext:
        state: StateStore
        clock: Clock
        # ... plus whatever your bots need:
        quotes: MyQuoteReader
        broker: MyOrderSubmitter

Bots are generic over the context type (``BaseBot[MyContext]``), so bot code
gets full static typing on ``self.ctx.quotes`` etc. without the engine knowing
anything about those capabilities.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bot_engine.base import BotRole


class StateStore(Protocol):
    """Persistent key-value state, scoped per bot name.

    Keys are namespaced by ``bot_name`` so concurrently-running bots never
    collide. Values must be JSON-serialisable. The engine ships an
    :class:`bot_engine.state.InMemoryStateStore` for tests and simple hosts;
    production hosts typically back this with Redis and/or a database.
    """

    async def get(self, bot_name: str, key: str, default: Any = None) -> Any: ...

    async def set(self, bot_name: str, key: str, value: Any) -> None: ...

    async def delete(self, bot_name: str, key: str) -> None: ...

    async def clear_all(self, bot_name: str) -> None: ...


class Clock(Protocol):
    """Time source. Inject a fixed/simulated clock for backtests and tests.

    Bots must never call ``datetime.now()`` / ``date.today()`` directly —
    always ``self.ctx.clock.now()`` / ``.today()`` so the same bot code runs
    identically live and in simulation.
    """

    def now(self) -> datetime: ...

    def today(self) -> date: ...


@runtime_checkable
class BotContext(Protocol):
    """The minimal contract a host context must satisfy.

    ``state`` and ``clock`` are the only attributes the engine touches.
    Optionally, a context may also provide::

        async def get_watchlist_symbols(self, name: str) -> set[str]

    which enables the ``watchlist`` parameter on bots (see
    :meth:`bot_engine.base.BaseBot.get_underlyings`).
    """

    state: StateStore
    clock: Clock


class EnablementGate(Protocol):
    """Runtime enable/disable check, resolved fresh on every bot fire.

    Lets an operator turn bots on/off without restarting the engine (e.g. a
    Redis flag written by a CLI). Returns ``(enabled, reason)`` — the reason
    is a short greppable string recorded on the skip.

    When the flag source is unavailable, gates should fall back on the bot's
    role: EXIT → enabled (fail-open — never strand an open position), ENTRY →
    disabled (fail-closed — never open new risk blind). Use
    :func:`role_default` for that fallback.
    """

    async def resolve(
        self, *, account: str, bot_name: str, role: BotRole | None
    ) -> tuple[bool, str]: ...


class RunRecorder(Protocol):
    """Persistence hook for bot-run audit rows (one row per fire).

    ``open()`` is called before ``execute()`` and returns an opaque handle;
    ``close()`` is called exactly once afterwards with the outcome. Both are
    best-effort from the engine's perspective — a recorder failure must not
    block the run (raise inside and the engine logs + continues).
    """

    async def open(
        self, *, run_id: str, bot: str, account: str, underlying: str | None
    ) -> Any: ...

    async def close(
        self,
        handle: Any,
        *,
        result: str,
        duration_ms: int,
        skip_reason: str | None = None,
        error: str | None = None,
        order_id: str | None = None,
        trade_group_id: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None: ...


class TradingCalendar(Protocol):
    """Market-calendar gate for the scheduler.

    When provided, scheduled fires on non-trading days are skipped (manual
    triggers bypass the check). Hosts wrap their own holiday calendar.
    """

    def is_trading_day(self, day: date) -> bool: ...


def role_default(role: BotRole | None) -> bool:
    """Fallback enablement when a gate's flag source can't be resolved.

    EXIT → True (fail-open: always able to manage/close existing positions).
    ENTRY or unknown → False (fail-closed: never open new risk blind).
    """
    from bot_engine.base import BotRole

    return role is BotRole.EXIT


class AlwaysEnabled:
    """Default gate: every bot is enabled. Suitable for dev and simple hosts."""

    async def resolve(
        self, *, account: str, bot_name: str, role: BotRole | None
    ) -> tuple[bool, str]:
        return True, "no_gate"
