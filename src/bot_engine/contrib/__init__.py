"""Contrib — generic, reusable bots built on the engine's core contracts.

Unlike the engine core, contrib bots need more from the host context than
``state`` and ``clock``; each module documents the extra ports it requires.
Import from the submodule (nothing here is re-exported at the
``bot_engine`` top level)::

    from bot_engine.contrib import ManagedExitBot
"""

from bot_engine.contrib.managed_exit import (
    ExitOrderPlan,
    ExitOrderSubmitter,
    ManagedExitBot,
    ManagedExitContext,
    ManagedPosition,
    PositionReader,
)

__all__ = [
    "ExitOrderPlan",
    "ExitOrderSubmitter",
    "ManagedExitBot",
    "ManagedExitContext",
    "ManagedPosition",
    "PositionReader",
]
