"""bot-engine — host-agnostic trading-bot runtime.

The engine owns the bot lifecycle: a :class:`BaseBot` base class with
declarative parameters, a YAML :class:`BotRegistry`, a :class:`BotExecutor`
with skip/error semantics and pluggable enable-gate/run-recorder hooks, and
an optional cron :class:`BotScheduler`.

Everything domain-specific — market data, order submission, positions —
lives on the *host's* context object, which needs only a ``state`` store and
a ``clock`` to satisfy the engine (see :mod:`bot_engine.ports`).
"""

from bot_engine.base import BaseBot, BotRole, ExecutionResult, SkipExecution
from bot_engine.clock import FixedClock, WallClock
from bot_engine.executor import BotExecutor, mint_run_id
from bot_engine.loader import load_bot_class, reload_bot_class
from bot_engine.ports import (
    AlwaysEnabled,
    BotContext,
    Clock,
    EnablementGate,
    RunRecorder,
    StateStore,
    TradingCalendar,
    role_default,
)
from bot_engine.registry import (
    BotConfig,
    BotRegistry,
    BotRegistryError,
    TriggerSpec,
)
from bot_engine.state import InMemoryStateStore

__all__ = [
    "AlwaysEnabled",
    "BaseBot",
    "BotConfig",
    "BotContext",
    "BotExecutor",
    "BotRegistry",
    "BotRegistryError",
    "BotRole",
    "Clock",
    "EnablementGate",
    "ExecutionResult",
    "FixedClock",
    "InMemoryStateStore",
    "RunRecorder",
    "SkipExecution",
    "StateStore",
    "TradingCalendar",
    "TriggerSpec",
    "WallClock",
    "load_bot_class",
    "mint_run_id",
    "reload_bot_class",
    "role_default",
]

# BotScheduler / AccountBots require the ``scheduler`` extra (APScheduler);
# import them from bot_engine.scheduler directly.
