"""End-to-end example: a host project integrating bot-engine.

The host defines:
  1. Its own trading ports (here: an in-memory quote reader + paper broker)
  2. A context dataclass bundling them with the engine's state/clock
  3. Bots typed against that context
  4. A bots.yaml registry

Run:  uv run python examples/quickstart.py
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bot_engine import (
    BaseBot,
    BotExecutor,
    BotRegistry,
    BotRole,
    ExecutionResult,
    InMemoryStateStore,
    SkipExecution,
    WallClock,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


# ---------------------------------------------------------------------------
# 1. Host trading ports — whatever YOUR platform provides
# ---------------------------------------------------------------------------


class FakeQuotes:
    """Stands in for the host's market-data read path."""

    prices = {"SPX": 6810.0, "RUT": 2310.0}

    async def get_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)


class PaperBroker:
    """Stands in for the host's order-submission path (OMS, broker API, ...)."""

    def __init__(self) -> None:
        self.orders: list[dict] = []

    async def submit(self, symbol: str, side: str, qty: int) -> str:
        order_id = f"ord-{len(self.orders) + 1}"
        self.orders.append({"id": order_id, "symbol": symbol, "side": side, "qty": qty})
        return order_id


# ---------------------------------------------------------------------------
# 2. Host context — state + clock satisfy the engine; the rest is yours
# ---------------------------------------------------------------------------


@dataclass
class MyContext:
    state: InMemoryStateStore = field(default_factory=InMemoryStateStore)
    clock: WallClock = field(default_factory=WallClock)
    quotes: FakeQuotes = field(default_factory=FakeQuotes)
    broker: PaperBroker = field(default_factory=PaperBroker)


# ---------------------------------------------------------------------------
# 3. A bot — parameters are annotated class attributes, overridable from YAML
# ---------------------------------------------------------------------------


class PriceThresholdBot(BaseBot[MyContext]):
    """Buys when the underlying trades below a threshold, once per day."""

    role = BotRole.ENTRY

    underlying: str = "SPX"
    buy_below: float = 7000.0
    qty: int = 1

    async def execute(self) -> ExecutionResult:
        today = self.ctx.clock.today().isoformat()
        if await self.get_state("last_entry_date") == today:
            raise SkipExecution("already entered today")

        price = await self.ctx.quotes.get_price(self.underlying)
        if price is None:
            raise SkipExecution(f"no quote for {self.underlying}")
        if price >= self.buy_below:
            raise SkipExecution(f"{self.underlying} at {price} >= threshold {self.buy_below}")

        order_id = await self.ctx.broker.submit(self.underlying, "buy", self.qty)
        await self.set_state("last_entry_date", today)
        return ExecutionResult(
            action="submitted_entry",
            order_id=order_id,
            data={"symbol": self.underlying, "price": price, "qty": self.qty},
        )


# ---------------------------------------------------------------------------
# 4. Wire it up and fire
# ---------------------------------------------------------------------------


async def main() -> None:
    registry = BotRegistry.from_file(Path(__file__).parent / "bots.yaml")
    registry.validate_roles()

    ctx = MyContext()
    executor = BotExecutor(ctx, account="paper")

    for config in registry.enabled_bots():
        result = await executor.run(config)
        print(f"{config.name}: {result.action} {result.data}")

    # Second fire the same day → state-based skip
    result = await executor.run(registry.get("spx_dip_buyer"))
    print(f"second fire: {result.action} ({result.data.get('reason')})")

    print(f"broker received: {ctx.broker.orders}")


if __name__ == "__main__":
    asyncio.run(main())
