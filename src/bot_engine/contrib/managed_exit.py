"""ManagedExitBot — generic, declaratively-parameterized exit manager.

Scans open positions it is responsible for and closes any that hit a stop,
time rule, or profit target. All strategy variance lives in registry
parameters; the class itself contains no strategy-specific logic.

Rule semantics:

* Every rule has an explicit off-switch (``0`` / ``""`` / ``False`` =
  disabled), so one class covers "profit-target only", "time-stop only", etc.
* Any firing rule closes the position. Rules are checked in fixed priority —
  stop_loss → short_strike_touch → exit_at_dte → max_days_in_trade →
  profit_target — and the first match is recorded as ``exit_reason``.
* P&L rules (``profit_target_pct``, ``stop_loss_multiple``) apply only to
  credit positions (``credit_received > 0``); debit structures are governed
  by the time and touch rules alone.
* ``stop_loss_multiple`` is loss-based: trigger when
  ``cost_to_close - credit_received >= stop_loss_multiple * credit_received``.

Host contract (see :class:`ManagedExitContext`): beyond the engine's
``state``/``clock``, the context must provide a :class:`PositionReader` as
``positions`` and an :class:`ExitOrderSubmitter` as ``orders``. Order-working
mechanics (repricing ladder, final unfilled action) are delegated to the
submitter via :class:`ExitOrderPlan` so the bot itself never blocks — and so
the same bot runs unchanged against a simulated host.

Registry example::

    spx_ic_exit:
      class_path: "bot_engine.contrib.ManagedExitBot"
      schedule: "*/5 15-20 * * mon-fri"
      enabled: true
      parameters:
        underlying: "SPX"
        entered_by: "spx_ic_16d_5w"
        profit_target_pct: 50
        stop_loss_multiple: 2.0
        exit_at_dte: 21
        unfilled_action: "market"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Protocol, Sequence, runtime_checkable

from bot_engine.base import BaseBot, BotRole, ExecutionResult, SkipExecution
from bot_engine.ports import BotContext

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data contracts
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ManagedPosition:
    """One open position as seen by the exit manager.

    Monetary fields share one convention: ``credit_received`` is the net
    premium collected at open (``<= 0`` for debit structures) and
    ``cost_to_close`` is the current debit to exit, in the same units.
    Profit captured is therefore ``credit_received - cost_to_close``.

    ``short_put_strike`` / ``short_call_strike`` and ``underlying_price``
    are only needed when ``stop_on_short_strike_touch`` is enabled; leave
    them ``None`` otherwise.
    """

    position_id: str
    underlying: str
    credit_received: Decimal
    cost_to_close: Decimal
    opened_at: datetime
    expiration: date | None = None
    entered_by: str = ""
    strategy_tag: str = ""
    trade_group_id: int | None = None
    short_put_strike: Decimal | None = None
    short_call_strike: Decimal | None = None
    underlying_price: Decimal | None = None


@dataclass(frozen=True)
class ExitOrderPlan:
    """Order-working instructions handed to the host's submitter.

    The bot decides *whether* to close; the submitter owns *how* — including
    the repricing ladder, which may outlive the bot fire that requested it.
    """

    order_type: str  # limit | market
    limit_price_mode: str  # mid | natural | mid_minus_ticks
    reprice_interval_s: int
    reprice_max_attempts: int
    unfilled_action: str  # leave | cancel | market


class PositionReader(Protocol):
    """Read open positions eligible for exit management.

    Must **exclude** positions that already have a working close order —
    the reader is the component that knows about live orders, and this is
    what makes repeated cron fires idempotent (the bot never deduplicates).
    """

    async def get_open_positions(
        self, *, underlying: str | None = None
    ) -> Sequence[ManagedPosition]: ...


class ExitOrderSubmitter(Protocol):
    """Submit a closing order for a position. Returns the host order id."""

    async def submit_close(self, position: ManagedPosition, plan: ExitOrderPlan) -> str: ...


@runtime_checkable
class ManagedExitContext(BotContext, Protocol):
    """Context contract for :class:`ManagedExitBot`: core engine ports plus
    a position reader and an exit-order submitter."""

    positions: PositionReader
    orders: ExitOrderSubmitter


# -----------------------------------------------------------------------------
# Parameter vocabularies
# -----------------------------------------------------------------------------

_ORDER_TYPES = ("limit", "market")
_LIMIT_PRICE_MODES = ("mid", "natural", "mid_minus_ticks")
_UNFILLED_ACTIONS = ("leave", "cancel", "market")


def _parse_window(spec: str) -> tuple[time, time]:
    """Parse ``"HH:MM-HH:MM"`` into (start, end). Start must precede end."""
    try:
        start_s, _, end_s = spec.partition("-")
        start = time.fromisoformat(start_s.strip())
        end = time.fromisoformat(end_s.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid exit_time_window '{spec}': expected 'HH:MM-HH:MM'") from exc
    if start >= end:
        raise ValueError(f"Invalid exit_time_window '{spec}': start must precede end")
    return start, end


# -----------------------------------------------------------------------------
# The bot
# -----------------------------------------------------------------------------


class ManagedExitBot(BaseBot[ManagedExitContext]):
    """Generic exit manager — see the module docstring for rule semantics."""

    role = BotRole.EXIT

    # --- Position selection ---
    underlying: str = ""  # or watchlist; both empty = manage all underlyings
    entered_by: str = ""  # only positions opened by this bot name ("" = any)
    strategy_tag: str = ""  # only positions with this host-side tag ("" = any)

    # --- Profit target (credit positions: % of max profit captured) ---
    profit_target_pct: float = 50.0  # 0 = disabled

    # --- Stop loss ---
    stop_loss_multiple: float = 2.0  # loss >= N x credit; 0 = disabled
    stop_on_short_strike_touch: bool = False

    # --- Time rules ---
    exit_at_dte: int = 21  # close at <= N days to expiration; 0 = disabled
    max_days_in_trade: int = 0  # close after N calendar days; 0 = disabled
    exit_time_window: str = ""  # "HH:MM-HH:MM" in the clock's tz; "" = always

    # --- Order mechanics (delegated to the submitter via ExitOrderPlan) ---
    order_type: str = "limit"
    limit_price_mode: str = "mid"
    reprice_interval_s: int = 30
    reprice_max_attempts: int = 4
    unfilled_action: str = "leave"

    # --- Safety rails ---
    max_closes_per_run: int = 3  # 0 = unlimited; excess deferred to next fire
    dry_run: bool = False  # evaluate and report, submit nothing

    def __init__(self, name: str, parameters: dict[str, Any], context: ManagedExitContext) -> None:
        super().__init__(name, parameters, context)
        self._validate()

    def _validate(self) -> None:
        """Fail fast on config errors — a typo'd enum must not reach a fire."""
        for param, value, allowed in (
            ("order_type", self.order_type, _ORDER_TYPES),
            ("limit_price_mode", self.limit_price_mode, _LIMIT_PRICE_MODES),
            ("unfilled_action", self.unfilled_action, _UNFILLED_ACTIONS),
        ):
            if value not in allowed:
                raise ValueError(f"Bot {self.name}: {param}='{value}' not in {allowed}")
        if self.exit_time_window:
            _parse_window(self.exit_time_window)

    # -------------------------------------------------------------------------
    # Execution
    # -------------------------------------------------------------------------

    async def execute(self) -> ExecutionResult:
        if self.exit_time_window:
            start, end = _parse_window(self.exit_time_window)
            now = self.ctx.clock.now().time()
            if not start <= now <= end:
                raise SkipExecution(f"outside exit window {self.exit_time_window}")

        positions = await self._eligible_positions()
        if not positions:
            raise SkipExecution("no open positions match selection")

        today = self.ctx.clock.today()
        decisions = [
            (position, reason)
            for position in positions
            if (reason := self._exit_reason(position, today)) is not None
        ]
        if not decisions:
            raise SkipExecution(f"no exit rules triggered across {len(positions)} position(s)")

        if self.max_closes_per_run > 0:
            acted, deferred = (
                decisions[: self.max_closes_per_run],
                decisions[self.max_closes_per_run :],
            )
        else:
            acted, deferred = decisions, []

        if deferred:
            logger.warning(
                "max_closes_per_run cap hit — deferring exits to next fire",
                extra={"bot": self.name, "acted": len(acted), "deferred": len(deferred)},
            )

        if self.dry_run:
            return ExecutionResult(
                action="no_action",
                data={
                    "dry_run": True,
                    "would_close": [self._describe(p, r) for p, r in acted],
                    "deferred": [self._describe(p, r) for p, r in deferred],
                },
            )

        plan = ExitOrderPlan(
            order_type=self.order_type,
            limit_price_mode=self.limit_price_mode,
            reprice_interval_s=self.reprice_interval_s,
            reprice_max_attempts=self.reprice_max_attempts,
            unfilled_action=self.unfilled_action,
        )
        closed: list[dict[str, Any]] = []
        for position, reason in acted:
            order_id = await self.ctx.orders.submit_close(position, plan)
            closed.append(self._describe(position, reason) | {"order_id": order_id})

        data: dict[str, Any] = {"closed": closed}
        if deferred:
            data["deferred"] = [self._describe(p, r) for p, r in deferred]

        return ExecutionResult(
            action="submitted_exit",
            order_id=closed[0]["order_id"],
            trade_group_id=acted[0][0].trade_group_id if len(acted) == 1 else None,
            data=data,
        )

    # -------------------------------------------------------------------------
    # Selection and rule evaluation
    # -------------------------------------------------------------------------

    async def _eligible_positions(self) -> list[ManagedPosition]:
        """Fetch open positions for the configured scope and apply filters."""
        if self.underlying or self.watchlist:
            underlyings: list[str | None] = list(await self.get_underlyings())
        else:
            underlyings = [None]  # unscoped: manage all underlyings

        positions: list[ManagedPosition] = []
        for symbol in underlyings:
            positions.extend(await self.ctx.positions.get_open_positions(underlying=symbol))

        if self.entered_by:
            positions = [p for p in positions if p.entered_by == self.entered_by]
        if self.strategy_tag:
            positions = [p for p in positions if p.strategy_tag == self.strategy_tag]
        return positions

    def _exit_reason(self, position: ManagedPosition, today: date) -> str | None:
        """First matching rule in priority order, or None to hold."""
        credit = position.credit_received
        is_credit = credit > 0

        if self.stop_loss_multiple > 0 and is_credit:
            loss = position.cost_to_close - credit
            if loss >= credit * Decimal(str(self.stop_loss_multiple)):
                return "stop_loss"

        if self.stop_on_short_strike_touch and position.underlying_price is not None:
            price = position.underlying_price
            if position.short_put_strike is not None and price <= position.short_put_strike:
                return "short_strike_touch"
            if position.short_call_strike is not None and price >= position.short_call_strike:
                return "short_strike_touch"

        if self.exit_at_dte > 0 and position.expiration is not None:
            if (position.expiration - today).days <= self.exit_at_dte:
                return "exit_at_dte"

        if self.max_days_in_trade > 0:
            if (today - position.opened_at.date()).days >= self.max_days_in_trade:
                return "max_days_in_trade"

        if self.profit_target_pct > 0 and is_credit:
            captured_pct = (credit - position.cost_to_close) / credit * 100
            if captured_pct >= Decimal(str(self.profit_target_pct)):
                return "profit_target"

        return None

    @staticmethod
    def _describe(position: ManagedPosition, reason: str) -> dict[str, Any]:
        """JSON-serialisable audit entry for the run recorder's data payload."""
        return {
            "position_id": position.position_id,
            "underlying": position.underlying,
            "trade_group_id": position.trade_group_id,
            "exit_reason": reason,
        }
