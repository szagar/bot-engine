"""ManagedExitBot — rule evaluation, selection filters, order submission."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from bot_engine.base import SkipExecution
from bot_engine.clock import FixedClock
from bot_engine.contrib import ExitOrderPlan, ManagedExitBot, ManagedPosition
from bot_engine.state import InMemoryStateStore


@dataclass
class StubPositionReader:
    positions: list[ManagedPosition] = field(default_factory=list)

    async def get_open_positions(self, *, underlying: str | None = None) -> list[ManagedPosition]:
        if underlying is None:
            return list(self.positions)
        return [p for p in self.positions if p.underlying == underlying]


@dataclass
class RecordingSubmitter:
    submitted: list[tuple[ManagedPosition, ExitOrderPlan]] = field(default_factory=list)

    async def submit_close(self, position: ManagedPosition, plan: ExitOrderPlan) -> str:
        self.submitted.append((position, plan))
        return f"ord-{len(self.submitted)}"


@dataclass
class ExitStubContext:
    state: InMemoryStateStore = field(default_factory=InMemoryStateStore)
    clock: FixedClock = field(
        default_factory=lambda: FixedClock(datetime(2026, 7, 3, 16, 0, tzinfo=UTC))
    )
    positions: StubPositionReader = field(default_factory=StubPositionReader)
    orders: RecordingSubmitter = field(default_factory=RecordingSubmitter)


@pytest.fixture
def ctx() -> ExitStubContext:
    return ExitStubContext()


def make_position(**overrides) -> ManagedPosition:
    """A healthy SPX credit position no rule should fire on by default:
    25% profit captured, 40 DTE, opened yesterday."""
    defaults = dict(
        position_id="pos-1",
        underlying="SPX",
        credit_received=Decimal("2.00"),
        cost_to_close=Decimal("1.50"),
        opened_at=datetime(2026, 7, 2, 14, 0, tzinfo=UTC),
        expiration=date(2026, 8, 12),
        entered_by="spx_ic_16d_5w",
        strategy_tag="iron_condor",
        trade_group_id=42,
    )
    return ManagedPosition(**{**defaults, **overrides})


def make_bot(ctx, **parameters) -> ManagedExitBot:
    return ManagedExitBot("exit_test", parameters, ctx)


class TestExitRules:
    async def test_profit_target_triggers_close(self, ctx):
        # 55% of the 2.00 credit captured >= 50% target
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.90"))]
        result = await make_bot(ctx, profit_target_pct=50).execute()

        assert result.action == "submitted_exit"
        assert result.order_id == "ord-1"
        assert result.trade_group_id == 42
        assert result.data["closed"][0]["exit_reason"] == "profit_target"

    async def test_stop_loss_uses_loss_multiple(self, ctx):
        # loss = 6.10 - 2.00 = 4.10 >= 2 x 2.00
        ctx.positions.positions = [make_position(cost_to_close=Decimal("6.10"))]
        result = await make_bot(ctx, stop_loss_multiple=2.0).execute()
        assert result.data["closed"][0]["exit_reason"] == "stop_loss"

    async def test_time_rule_outranks_profit_target(self, ctx):
        # Both fire (profitable at 15 DTE) — priority order picks the time rule.
        ctx.positions.positions = [
            make_position(cost_to_close=Decimal("0.50"), expiration=date(2026, 7, 18))
        ]
        result = await make_bot(ctx, exit_at_dte=21, profit_target_pct=50).execute()
        assert result.data["closed"][0]["exit_reason"] == "exit_at_dte"

    async def test_max_days_in_trade(self, ctx):
        ctx.positions.positions = [make_position(opened_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC))]
        result = await make_bot(ctx, max_days_in_trade=30, exit_at_dte=0).execute()
        assert result.data["closed"][0]["exit_reason"] == "max_days_in_trade"

    async def test_short_strike_touch(self, ctx):
        ctx.positions.positions = [
            make_position(
                short_put_strike=Decimal("6800"),
                short_call_strike=Decimal("7100"),
                underlying_price=Decimal("6795"),
            )
        ]
        result = await make_bot(ctx, stop_on_short_strike_touch=True).execute()
        assert result.data["closed"][0]["exit_reason"] == "short_strike_touch"

    async def test_pnl_rules_skip_debit_positions(self, ctx):
        # Debit structure (credit <= 0): stop/profit rules must not evaluate.
        ctx.positions.positions = [
            make_position(credit_received=Decimal("-3.00"), cost_to_close=Decimal("0.10"))
        ]
        bot = make_bot(ctx, profit_target_pct=50, stop_loss_multiple=2.0, exit_at_dte=0)
        with pytest.raises(SkipExecution, match="no exit rules triggered"):
            await bot.execute()

    async def test_disabled_rules_never_fire(self, ctx):
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.01"))]
        bot = make_bot(ctx, profit_target_pct=0, stop_loss_multiple=0, exit_at_dte=0)
        with pytest.raises(SkipExecution, match="no exit rules triggered"):
            await bot.execute()


class TestSelection:
    async def test_entered_by_filter(self, ctx):
        ctx.positions.positions = [
            make_position(position_id="mine", cost_to_close=Decimal("0.10")),
            make_position(position_id="other", cost_to_close=Decimal("0.10"), entered_by="rut_bot"),
        ]
        result = await make_bot(ctx, entered_by="spx_ic_16d_5w").execute()
        assert [c["position_id"] for c in result.data["closed"]] == ["mine"]

    async def test_underlying_scopes_the_reader_query(self, ctx):
        ctx.positions.positions = [
            make_position(position_id="spx", cost_to_close=Decimal("0.10")),
            make_position(position_id="rut", underlying="RUT", cost_to_close=Decimal("0.10")),
        ]
        result = await make_bot(ctx, underlying="RUT").execute()
        assert [c["position_id"] for c in result.data["closed"]] == ["rut"]

    async def test_unscoped_bot_manages_all_underlyings(self, ctx):
        ctx.positions.positions = [
            make_position(position_id="spx", cost_to_close=Decimal("0.10")),
            make_position(position_id="rut", underlying="RUT", cost_to_close=Decimal("0.10")),
        ]
        result = await make_bot(ctx).execute()
        assert len(result.data["closed"]) == 2

    async def test_no_matching_positions_skips(self, ctx):
        with pytest.raises(SkipExecution, match="no open positions match"):
            await make_bot(ctx).execute()


class TestSafetyRails:
    async def test_dry_run_submits_nothing(self, ctx):
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.10"))]
        result = await make_bot(ctx, dry_run=True).execute()

        assert result.action == "no_action"
        assert result.data["dry_run"] is True
        assert result.data["would_close"][0]["exit_reason"] == "profit_target"
        assert ctx.orders.submitted == []

    async def test_max_closes_per_run_defers_excess(self, ctx):
        ctx.positions.positions = [
            make_position(position_id=f"pos-{i}", cost_to_close=Decimal("0.10")) for i in range(5)
        ]
        result = await make_bot(ctx, max_closes_per_run=2).execute()

        assert len(result.data["closed"]) == 2
        assert len(result.data["deferred"]) == 3
        assert len(ctx.orders.submitted) == 2
        assert result.trade_group_id is None  # multi-close: no single group id

    async def test_outside_time_window_skips(self, ctx):
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.10"))]
        bot = make_bot(ctx, exit_time_window="18:00-20:00")  # clock is 16:00
        with pytest.raises(SkipExecution, match="outside exit window"):
            await bot.execute()

    async def test_inside_time_window_proceeds(self, ctx):
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.10"))]
        result = await make_bot(ctx, exit_time_window="15:00-20:00").execute()
        assert result.action == "submitted_exit"


class TestValidation:
    def test_invalid_enum_fails_at_construction(self, ctx):
        with pytest.raises(ValueError, match="unfilled_action"):
            make_bot(ctx, unfilled_action="retry_forever")

    def test_invalid_time_window_fails_at_construction(self, ctx):
        with pytest.raises(ValueError, match="exit_time_window"):
            make_bot(ctx, exit_time_window="25:00-26:00")

    def test_inverted_time_window_fails_at_construction(self, ctx):
        with pytest.raises(ValueError, match="start must precede end"):
            make_bot(ctx, exit_time_window="18:00-09:00")

    async def test_scalar_params_coerce_from_yaml_strings(self, ctx):
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.90"))]
        bot = make_bot(ctx, profit_target_pct="50", dry_run="true", max_closes_per_run="1")
        assert bot.profit_target_pct == 50.0
        assert bot.dry_run is True
        result = await bot.execute()
        assert result.data["would_close"][0]["exit_reason"] == "profit_target"

    async def test_order_plan_carries_order_params(self, ctx):
        ctx.positions.positions = [make_position(cost_to_close=Decimal("0.10"))]
        await make_bot(ctx, unfilled_action="market", reprice_max_attempts=2).execute()
        _, plan = ctx.orders.submitted[0]
        assert plan.unfilled_action == "market"
        assert plan.reprice_max_attempts == 2
