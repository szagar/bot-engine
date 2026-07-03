# Integrating bot-engine with a trading platform

This guide walks through wiring `bot-engine` into a real trading platform, end to end:
what you must implement, what the engine provides, and a complete host-service skeleton
you can copy. Companion documents:

- [`ports-reference.md`](ports-reference.md) — protocol-by-protocol contracts with
  production-grade implementations (Postgres/Redis state store, enable gate, run
  recorder DDL, trading calendar).
- [`integration-checklist.md`](integration-checklist.md) — go-live checklist.
- [`diagrams/`](diagrams/) — Excalidraw source for the architecture, run-lifecycle,
  and signal-flow diagrams (open at [excalidraw.com](https://excalidraw.com) or with
  the VS Code Excalidraw extension).

## The division of responsibility

The engine owns the **bot lifecycle**; your platform owns **everything that touches
the market**. The engine never sees quotes, orders, or positions — bots reach those
through the context object *you* define.

| bot-engine provides | You (the host) provide |
|---|---|
| `BaseBot[CtxT]` — parameterised bot base class | Context dataclass with your trading capabilities |
| `BotRegistry` — YAML config (schedules, params, triggers) | Market-data / broker / position adapters |
| `BotExecutor` — run lifecycle, skip/error semantics | `StateStore` backed by your infra (or `InMemoryStateStore`) |
| `BotScheduler` — cron fires via APScheduler (optional extra) | `EnablementGate` over your flag store (optional) |
| `WallClock` / `FixedClock`, `InMemoryStateStore` | `RunRecorder` over your audit table (optional) |
| `role_default()` fail-open/fail-closed fallback | `TradingCalendar` over your holiday calendar (optional) |
| Run-id minting, structured log fields | Signal consumer loop, ops CLI, observability wiring |

Hard requirements on your context are just two attributes: `state` (a `StateStore`)
and `clock` (a `Clock`). Everything else is your choice.

## Install

```bash
# with the cron scheduler (APScheduler):
uv add "bot-engine[scheduler] @ git+https://github.com/szagar/bot-engine"
# executor/registry only (you drive fires yourself):
uv add "bot-engine @ git+https://github.com/szagar/bot-engine"
```

Python ≥ 3.11. Runtime dependency is only PyYAML; APScheduler comes with the
`scheduler` extra.

## Step 1 — Define your trading ports

These are *your* classes with *your* method names; the engine never calls them.
Typical shape for an options platform:

```python
class QuoteReader:
    async def get_price(self, symbol: str) -> float | None: ...
    async def get_chain(self, symbol: str, dte_min: int, dte_max: int) -> Chain: ...
    async def get_iv_rank(self, symbol: str) -> float | None: ...

class OrderSubmitter:
    async def submit_spread(self, order: SpreadOrder) -> str: ...   # returns order_id
    async def close_position(self, trade_group_id: int) -> str: ...

class PositionReader:
    async def open_positions(self, underlying: str) -> list[Position]: ...
```

Back them with whatever you have — a broker SDK (TastyTrade, IBKR, Alpaca), an
internal OMS, or a paper simulator. Keep them `async`; bots await them directly.

## Step 2 — Build the context

One dataclass bundling the engine's two required ports with your trading ports:

```python
from dataclasses import dataclass
from bot_engine import StateStore, Clock

@dataclass
class TradingContext:
    # required by the engine
    state: StateStore
    clock: Clock
    # yours — anything bots need
    quotes: QuoteReader
    broker: OrderSubmitter
    positions: PositionReader
    account_number: str

    # optional: enables `watchlist:` on bots
    async def get_watchlist_symbols(self, name: str) -> set[str]:
        return await self._watchlist_repo.symbols(name)
```

Build **one context per account** (live vs paper differ in broker adapter and
account number, and usually share market data). Bots are generic over this type
(`BaseBot[TradingContext]`), so `self.ctx.quotes` is fully typed.

Rules the engine enforces or assumes:

- Bots must use `self.ctx.clock.now()` / `.today()` — never `datetime.now()` —
  so the same bot code runs identically live and in simulation.
- `StateStore` values must be JSON-serialisable; keys are namespaced per bot name.

## Step 3 — Write bots

```python
from bot_engine import BaseBot, BotRole, ExecutionResult, SkipExecution

class IronCondorEntryBot(BaseBot[TradingContext]):
    role = BotRole.ENTRY          # REQUIRED — see "Roles" below

    # parameters: annotated class attributes with defaults, overridable from YAML
    underlying: str = "SPX"
    target_dte: int = 45
    short_delta: float = 0.16
    min_iv_rank: float = 30.0
    max_positions: int = 3

    async def execute(self) -> ExecutionResult:
        today = self.ctx.clock.today().isoformat()
        if await self.get_state("last_entry_date") == today:
            raise SkipExecution("already entered today")

        iv_rank = await self.ctx.quotes.get_iv_rank(self.underlying)
        if iv_rank is None or iv_rank < self.min_iv_rank:
            raise SkipExecution(f"IV rank {iv_rank} below {self.min_iv_rank}")

        open_now = await self.ctx.positions.open_positions(self.underlying)
        if len(open_now) >= self.max_positions:
            raise SkipExecution(f"{len(open_now)} positions open >= {self.max_positions}")

        order = build_condor(self.underlying, self.target_dte, self.short_delta)
        order_id = await self.ctx.broker.submit_spread(order)
        await self.set_state("last_entry_date", today)
        return ExecutionResult(
            action="submitted_entry",
            order_id=order_id,
            data={"underlying": self.underlying, "dte": self.target_dte},
        )
```

Key semantics:

- **`SkipExecution("reason")` is not an error.** It's the normal outcome of a
  scheduled bot whose conditions aren't met — recorded as `action="skipped"`,
  logged at INFO, never alerted. Any *other* exception is recorded as
  `result="error"` and re-raised (the scheduler catches it so the process
  survives; your alerting should watch for it).
- **Parameters** are applied from YAML with automatic type coercion driven by the
  annotations (`str → int/float/bool/Decimal`). Unknown YAML keys are silently
  ignored (a config typo must not break startup) — so test that your overrides
  actually land.
- **State** (`get_state` / `set_state`) is scoped to the *bot name from YAML*,
  not the class — two YAML entries using the same class have independent state.
- **Underlyings**: declare `underlying: str = "..."` for one symbol, or
  `watchlist: "earnings"` for a host-managed list; `await self.get_underlyings()`
  resolves either (sorted, deterministic). They are mutually exclusive —
  the registry rejects configs setting both.

### Roles — safety-critical

Every concrete bot must declare `role = BotRole.ENTRY | BotRole.EXIT`. The role
drives what an enable gate does when its flag store is unreachable:

- **ENTRY → disabled** (fail-closed: never open new risk blind)
- **EXIT → enabled** (fail-open: never strand an open position unable to close)

Call `registry.validate_roles()` at startup — it imports every configured class
and fails fast listing any bot without a role. Misclassifying an exit bot as
entry would, during a control-plane outage, leave a position stuck open.

## Step 4 — The registry (bots.yaml)

```yaml
includes:                          # optional, merged first (left-to-right),
  - shared/condor_bots.yaml        # this file's entries override on top

spx_ic_16d_5w:
  class_path: "myplatform.bots.condor.IronCondorEntryBot"
  schedule: "0 14 * * mon-fri"     # 5-field cron, evaluated in the scheduler's tz
  enabled: true
  parameters:
    underlying: "SPX"
    target_dte: 45
    short_delta: 0.16

condor_manager:
  class_path: "myplatform.bots.condor.CondorExitBot"
  schedule: "*/5 13-20 * * mon-fri"
  enabled: true
  parameters:
    watchlist: "open_condors"

orb_breakout_entry:
  class_path: "myplatform.bots.orb.OrbEntryBot"
  schedule: ""                     # trigger-only: never cron-fired
  enabled: true
  triggers:
    on_signal:                     # subscriber-side predicates (see Step 7)
      - signal: "orb_breakout"
        direction: "long"
```

Notes:

- `enabled: false` (the default if omitted) removes the bot from scheduling and
  from `match_triggers()`. This is the *static* switch; the runtime gate
  (Step 5) is the *dynamic* one you flip without restarting.
- `schedule: ""` = trigger-only. Fire via `scheduler.trigger_now()` or your
  signal consumer.
- The bot **name** (the YAML key) is the identity used for state scoping, job
  ids, gate flags, and audit rows. Renaming a bot orphans its state — migrate
  the `StateStore` keys if you rename.

## Step 5 — The executor (one per account)

```python
from bot_engine import BotExecutor

executor = BotExecutor(
    context=ctx_live,
    account="live",                       # recorded on runs, passed to the gate
    gate=PgEnablementGate(pool),          # optional — default AlwaysEnabled
    recorder=PgRunRecorder(pool),         # optional — per-fire audit rows
    run_id_minter=my_minter,              # optional — house correlation-id style
    on_run_context=bind_logging_context,  # optional — contextvars / OTel binding
)
```

Per fire, the executor: mints a `run_id` → calls `on_run_context` → opens a
recorder row (best-effort) → imports and instantiates the bot class with
name/params/context → resolves the gate **fresh** (so operators can flip bots
without a restart) → calls `bot.execute()` → closes the recorder row with the
outcome. A disabled bot no-ops through the normal skip path.

Gate and recorder implementations (Postgres and Redis variants, with DDL and the
fail-open/fail-closed reasoning) are in [`ports-reference.md`](ports-reference.md).

## Step 6 — The scheduler

```python
from bot_engine.scheduler import AccountBots, BotScheduler   # requires [scheduler]

scheduler = BotScheduler(
    [
        AccountBots("live", registry, executor_live),
        AccountBots("paper", registry, executor_paper),
    ],
    timezone="America/New_York",     # cron expressions evaluate in this tz
    calendar=MyTradingCalendar(),    # optional: skip fires on non-trading days
)
scheduler.setup()    # registers every enabled bot with a non-empty schedule
scheduler.start()    # needs a running asyncio event loop
```

Operational behaviour you get for free:

- Job ids are `{account}:{bot}`, so the same bot name runs under multiple
  accounts without collision.
- `max_instances=1` + `coalesce=True` — a slow bot never stacks overlapping runs.
- `misfire_grace_time=300s` — if the process was down briefly, a missed fire
  within 5 minutes still runs; older misses are dropped.
- All exceptions from a fire are caught and logged; one bad bot cannot kill the
  scheduler.
- `scheduler.trigger_now(account, bot)` fires any registered bot immediately in
  a background task, **bypassing the calendar** (manual/ signal-driven fires run
  on holidays too — intentional: signals and humans outrank the calendar).
- `scheduler.summary_lines()` gives a ready-to-print table of jobs and next-fire
  times for your startup log or ops CLI.

If you don't want APScheduler, skip the scheduler entirely and call
`await executor.run(registry.get(name))` from your own loop — the executor is
self-contained.

## Step 7 — Signal-driven bots

Bots select which signals fire them via `triggers.on_signal` predicates in YAML
(subscriber-side — no central route map). Your consumer loop is the glue:

```python
async def consume_signals(stream, registry, scheduler):
    async for payload in stream:                # webhook, Redis stream, queue...
        arrival = {
            "signal": payload["signal"],        # e.g. "orb_breakout"
            "symbol": payload.get("symbol", ""),
            "signal_kind": payload.get("kind", ""),    # entry|exit|adjust|info
            "direction": payload.get("direction", ""), # long|short|flat
        }
        for config in registry.match_triggers(arrival):
            await scheduler.trigger_now("live", config.name)
```

A `TriggerSpec` matches when every field it sets equals the arrival's value;
unset fields match anything. Disabled bots never match.

## Step 8 — The host service skeleton

```python
import asyncio
from bot_engine import BotRegistry, BotExecutor
from bot_engine.scheduler import AccountBots, BotScheduler

async def main() -> None:
    registry = BotRegistry.from_file("config/bots.yaml")
    registry.validate_roles()                       # fail fast on role-less bots

    pool = await create_pg_pool()
    ctx = TradingContext(
        state=PgStateStore(pool),
        clock=WallClock("America/New_York"),
        quotes=BrokerQuotes(session),
        broker=BrokerOrders(session, account_no),
        positions=BrokerPositions(session, account_no),
        account_number=account_no,
    )
    executor = BotExecutor(
        ctx, account="live",
        gate=PgEnablementGate(pool),
        recorder=PgRunRecorder(pool),
    )
    scheduler = BotScheduler(
        [AccountBots("live", registry, executor)],
        timezone="America/New_York",
        calendar=NyseCalendar(),
    )
    scheduler.setup()
    scheduler.start()
    print("\n".join(scheduler.summary_lines()))

    signal_task = asyncio.create_task(consume_signals(stream, registry, scheduler))
    try:
        await asyncio.Event().wait()                # run until SIGTERM
    finally:
        signal_task.cancel()
        await scheduler.stop()                      # waits for in-flight fires

asyncio.run(main())
```

## Testing and simulation

The engine was extracted with testability as a design goal — no infrastructure
is needed to exercise a bot fully:

```python
from datetime import datetime
from bot_engine import BotConfig, BotExecutor, FixedClock, InMemoryStateStore

async def test_condor_skips_on_low_iv():
    ctx = TradingContext(
        state=InMemoryStateStore(),
        clock=FixedClock(datetime(2026, 7, 6, 14, 0, tzinfo=NY)),
        quotes=StubQuotes(iv_rank=12.0),
        broker=RecordingBroker(),
        positions=StubPositions([]),
        account_number="paper",
    )
    executor = BotExecutor(ctx, account="test")
    config = BotConfig(
        name="ic_test",
        class_path="myplatform.bots.condor.IronCondorEntryBot",
        schedule="", enabled=True,
        parameters={"underlying": "SPX", "min_iv_rank": 30.0},
    )
    result = await executor.run(config)
    assert result.action == "skipped"
    assert "IV rank" in result.data["reason"]
```

For backtests, drive the same executor in a loop, advancing a `FixedClock`
(`clock.advance_to(...)`) and pointing `quotes`/`broker` at historical data and a
fill simulator. Because bots only ever see `self.ctx`, no bot code changes
between live, paper, and backtest.

## Observability

Every fire emits structured log records (stdlib `logging`, `extra={}` fields):
`bot`, `account`, `run_id`, `outcome`/`action`, `order_id`, `timing_ms`,
`reason` (skips). Wire them into your JSON log formatter and you have per-run
tracing with zero extra code. Use `on_run_context` to bind `run_id` into
`contextvars`/OTel so *your adapters'* log lines correlate with the fire, and
`run_id_minter=` to match a house correlation-id convention. The `RunRecorder`
gives you the durable per-run audit trail (see ports reference for DDL).
