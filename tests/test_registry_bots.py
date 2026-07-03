"""Importable bot classes used by loader/registry/executor tests."""

from __future__ import annotations

from bot_engine import BaseBot, BotRole, ExecutionResult, SkipExecution


class GoodBot(BaseBot):
    role = BotRole.ENTRY

    underlying: str = "SPX"

    async def execute(self) -> ExecutionResult:
        return ExecutionResult(action="submitted_entry", order_id="ord-1", data={"credit": "1.20"})


class NoRoleBot(BaseBot):
    async def execute(self) -> ExecutionResult:
        return ExecutionResult(action="no_action")


class SkippingBot(BaseBot):
    role = BotRole.ENTRY

    async def execute(self) -> ExecutionResult:
        raise SkipExecution("iv too low")


class ExplodingBot(BaseBot):
    role = BotRole.EXIT

    async def execute(self) -> ExecutionResult:
        raise RuntimeError("boom")


class NotABot:
    pass
