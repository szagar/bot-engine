"""BaseBot — parameter coercion, state scoping, underlying resolution."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bot_engine import BaseBot, BotRole, ExecutionResult, SkipExecution


class SampleBot(BaseBot):
    role = BotRole.ENTRY

    underlying: str = "SPX"
    target_dte: int = 45
    short_delta: float = 0.16
    min_credit: Decimal = Decimal("1.50")
    aggressive: bool = False

    async def execute(self) -> ExecutionResult:
        return ExecutionResult(action="no_action")


class WatchlistBot(BaseBot):
    role = BotRole.ENTRY
    watchlist: str | None = "earnings"

    async def execute(self) -> ExecutionResult:
        return ExecutionResult(action="no_action")


class TestParameterCoercion:
    def test_defaults_apply(self, ctx):
        bot = SampleBot("t", {}, ctx)
        assert bot.underlying == "SPX"
        assert bot.target_dte == 45

    def test_string_coercion_from_yaml(self, ctx):
        bot = SampleBot(
            "t",
            {
                "target_dte": "30",
                "short_delta": "0.10",
                "min_credit": "2.25",
                "aggressive": "true",
            },
            ctx,
        )
        assert bot.target_dte == 30
        assert bot.short_delta == 0.10
        assert bot.min_credit == Decimal("2.25")
        assert bot.aggressive is True

    def test_unknown_keys_ignored(self, ctx):
        bot = SampleBot("t", {"nonexistent_param": 99}, ctx)
        assert not hasattr(bot, "nonexistent_param")

    def test_native_types_pass_through(self, ctx):
        bot = SampleBot("t", {"target_dte": 7, "short_delta": 0.2}, ctx)
        assert bot.target_dte == 7
        assert bot.short_delta == 0.2


class TestState:
    async def test_state_roundtrip(self, ctx):
        bot = SampleBot("bot_a", {}, ctx)
        await bot.set_state("k", {"x": 1})
        assert await bot.get_state("k") == {"x": 1}
        await bot.delete_state("k")
        assert await bot.get_state("k", "gone") == "gone"

    async def test_state_scoped_by_bot_name(self, ctx):
        a = SampleBot("bot_a", {}, ctx)
        b = SampleBot("bot_b", {}, ctx)
        await a.set_state("k", "from_a")
        assert await b.get_state("k") is None

    async def test_clear_all(self, ctx):
        bot = SampleBot("bot_a", {}, ctx)
        await bot.set_state("k1", 1)
        await bot.set_state("k2", 2)
        await bot.clear_all_state()
        assert await bot.get_state("k1") is None


class TestUnderlyings:
    async def test_single_underlying(self, ctx):
        bot = SampleBot("t", {}, ctx)
        assert await bot.get_underlyings() == ["SPX"]

    async def test_watchlist_sorted_deduped(self, ctx):
        ctx.watchlists["earnings"] = {"NVDA", "AAPL", "NVDA"}
        bot = WatchlistBot("t", {}, ctx)
        assert await bot.get_underlyings() == ["AAPL", "NVDA"]

    async def test_empty_watchlist_skips(self, ctx):
        bot = WatchlistBot("t", {}, ctx)
        with pytest.raises(SkipExecution):
            await bot.get_underlyings()

    async def test_neither_configured_raises(self, ctx):
        bot = WatchlistBot("t", {"watchlist": None}, ctx)
        with pytest.raises(ValueError, match="neither"):
            await bot.get_underlyings()
