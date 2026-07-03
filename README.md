# bot-engine

Host-agnostic trading-bot runtime. The engine owns the **bot lifecycle** —
a declarative bot base class, a YAML registry, an executor with
skip/error/audit semantics, and a cron scheduler. Everything domain-specific
(market data, order submission, positions, risk) stays in **your** project:
you hand bots a context object, and the engine only requires two things on
it — a state store and a clock.

Extracted from the ZTS platform's strategy engine, generalized so any
trading project can import it to define and run its own bots.

```
┌────────────────────────────────────────────────────────────┐
│ bot-engine (this package)                                  │
│                                                            │
│  BotRegistry ──▶ BotScheduler ──▶ BotExecutor ──▶ BaseBot  │
│  (YAML+includes)  (cron, APS)     (gate, audit)   (yours)  │
└──────────────────────────┬─────────────────────────────────┘
                           │ self.ctx: YourContext
┌──────────────────────────▼─────────────────────────────────┐
│ your project                                               │
│                                                            │
│  YourContext { state: StateStore, clock: Clock,            │
│                quotes, broker, positions, ... }            │
└────────────────────────────────────────────────────────────┘
```

## Documentation

- [`docs/integration-guide.md`](docs/integration-guide.md) — step-by-step wiring into a trading platform, with a full host-service skeleton
- [`docs/ports-reference.md`](docs/ports-reference.md) — every port's contract plus production implementations (Postgres/Redis state store, enable gate, run-recorder DDL, trading calendar)
- [`docs/integration-checklist.md`](docs/integration-checklist.md) — go-live checklist
- [`docs/diagrams/`](docs/diagrams/) — Excalidraw architecture, run-lifecycle, and signal-flow diagrams

## Install

```toml
# pyproject.toml
dependencies = ["bot-engine[scheduler]"]

[tool.uv.sources]
bot-engine = { git = "https://github.com/szagar/bot-engine.git", branch = "main" }
```

The base package depends only on `pyyaml`. The `scheduler` extra adds
APScheduler for `BotScheduler`; skip it if you drive `BotExecutor.run()`
from your own loop.

## Quickstart

Define a context with whatever capabilities your bots need. The engine
requires only `state` and `clock`:

```python
from dataclasses import dataclass, field
from bot_engine import InMemoryStateStore, WallClock

@dataclass
class MyContext:
    state: InMemoryStateStore = field(default_factory=InMemoryStateStore)
    clock: WallClock = field(default_factory=WallClock)
    quotes: MyQuoteReader = ...      # your market data
    broker: MyOrderSubmitter = ...   # your execution path
```

Write a bot. Parameters are annotated class attributes; YAML overrides are
applied with automatic type coercion (str → int/float/bool/Decimal):

```python
from bot_engine import BaseBot, BotRole, ExecutionResult, SkipExecution

class PriceThresholdBot(BaseBot[MyContext]):
    role = BotRole.ENTRY          # required — see "Roles" below

    underlying: str = "SPX"
    buy_below: float = 7000.0
    qty: int = 1

    async def execute(self) -> ExecutionResult:
        today = self.ctx.clock.today().isoformat()
        if await self.get_state("last_entry_date") == today:
            raise SkipExecution("already entered today")

        price = await self.ctx.quotes.get_price(self.underlying)
        if price is None or price >= self.buy_below:
            raise SkipExecution(f"{self.underlying} at {price}, no entry")

        order_id = await self.ctx.broker.submit(self.underlying, "buy", self.qty)
        await self.set_state("last_entry_date", today)
        return ExecutionResult(action="submitted_entry", order_id=order_id,
                               data={"price": price})
```

Configure it in YAML:

```yaml
# bots.yaml
includes:                      # optional shared fragments, merged first
  - shared/dip_buyers.yaml

spx_dip_buyer:
  class_path: "mybots.PriceThresholdBot"
  schedule: "0 14 * * mon-fri" # 5-field cron; "" = trigger-only
  enabled: true
  parameters:
    underlying: "SPX"
    buy_below: 6900.0
```

Run it:

```python
from bot_engine import BotExecutor, BotRegistry
from bot_engine.scheduler import AccountBots, BotScheduler

registry = BotRegistry.from_file("bots.yaml")
registry.validate_roles()      # fail fast on any bot missing a role

executor = BotExecutor(ctx, account="individual")
scheduler = BotScheduler(
    [AccountBots("individual", registry, executor)],
    timezone="America/New_York",
    calendar=my_trading_calendar,          # optional; skips holidays
)
scheduler.setup()
scheduler.start()
```

A runnable end-to-end version is in [examples/quickstart.py](examples/quickstart.py).

## Concepts

### Execute / Skip / Error

`execute()` has exactly three outcomes, and the executor maps them to a
uniform result:

| Bot does | Executor records | Propagates? |
|---|---|---|
| returns `ExecutionResult` | that action (`submitted_entry`, `no_action`, ...) | no |
| raises `SkipExecution("reason")` | `action="skipped"` + reason | no — a skip is not an error |
| raises anything else | `result="error"` | yes, after the audit row closes |

This keeps "conditions not met" (the overwhelmingly common case for a
scheduled bot) cheap, quiet, and greppable, while real failures stay loud.

### Roles — fail-open vs fail-closed

Every concrete bot must declare `role = BotRole.ENTRY | BotRole.EXIT`
(`registry.validate_roles()` enforces at startup). The role drives what a
runtime enable gate should do when its flag source is unreachable:

- **ENTRY → disabled** (fail-closed: never open new risk blind)
- **EXIT → enabled** (fail-open: never strand an open position unable to close)

`bot_engine.ports.role_default(role)` implements that fallback for your gate.

### Runtime enable gate

`BotExecutor` resolves an `EnablementGate` fresh on **every** fire, so
operators can flip bots on/off (Redis flag, DB row, feature flag service)
without restarting the engine. A disabled bot's job still fires and no-ops
through the normal skip path. Default is `AlwaysEnabled`.

```python
class RedisGate:
    async def resolve(self, *, account, bot_name, role):
        try:
            raw = await self.redis.hget("bots:enabled", f"{account}:{bot_name}")
        except RedisError:
            return role_default(role), f"redis_unavailable role={role}"
        if raw is None:
            return role_default(role), "flag_undefined"
        return raw == b"1", f"flag={raw.decode()}"
```

### Run audit (`RunRecorder`)

Give the executor a `RunRecorder` and every fire opens/closes an audit
record (run id, bot, account, outcome, duration, skip reason / error /
order id). Recorder failures are logged and never block the run. The
`run_id` is mintable with your own convention (`run_id_minter=`), and the
`on_run_context` hook lets you bind it into a logging/tracing context so
downstream log lines correlate.

### State

`get_state` / `set_state` on the bot are scoped by bot name — two bots (or
the same bot class under two names) never collide. Back the
`StateStore` protocol with whatever you have; `InMemoryStateStore` ships
for tests and simple hosts.

### Watchlists

A bot may declare `watchlist: "earnings"` instead of `underlying`; the
registry enforces mutual exclusion, and `await self.get_underlyings()`
resolves either into a sorted symbol list. Watchlists require the context
to provide `get_watchlist_symbols(name) -> set[str]`.

### Signal triggers

Bots with `schedule: ""` are trigger-only. `triggers.on_signal` entries in
the YAML are subscriber-side predicates (`TriggerSpec`) — in your signal
consumer loop, call `registry.match_triggers(arrival)` and fire the matches
via `scheduler.trigger_now(account, bot_name)`.

### Multiple accounts

One scheduler drives N accounts: each `AccountBots` group binds a registry
to an executor whose context is wired to that account (live vs paper, etc.).
Job ids are namespaced `{account}:{bot}`.

## Port summary

| Protocol | Required? | Purpose | Ships with |
|---|---|---|---|
| `StateStore` | yes (on context) | scoped bot state | `InMemoryStateStore` |
| `Clock` | yes (on context) | injectable time (backtests) | `WallClock`, `FixedClock` |
| `EnablementGate` | optional | runtime on/off per bot | `AlwaysEnabled` |
| `RunRecorder` | optional | per-fire audit rows | — |
| `TradingCalendar` | optional | holiday gate on the scheduler | — |
| `get_watchlist_symbols` | optional (on context) | watchlist bots | — |

## Provenance — mapping from the ZTS strategy engine

This package is the extracted, de-platformed core of
`zts-massive/services/strategy-engine`. If you know that codebase:

| ZTS | bot-engine | Notes |
|---|---|---|
| `strategy_engine.bot.base.BaseBot` | `BaseBot[CtxT]` | now generic over the host context; leg-conflict helpers stayed in ZTS (structure-resolution-specific) |
| `ExecutionResult.oms_order_id` | `ExecutionResult.order_id` | generic name |
| `signal_id` | `run_id` | injectable minter preserves any house convention |
| `ExecutionContext` | *your context* | the whole point — the engine never sees quotes/orders/positions |
| `StateManager.get(name, key, default)` | `StateStore` protocol | identical shape; ZTS's manager already satisfies it via a `state` attribute alias |
| `bot.enablement.resolve_enabled` (Redis) | `EnablementGate` protocol + `role_default()` | same fail-open/fail-closed semantics, flag source is yours |
| `BotRunRecorder` (`bot_runs` table) | `RunRecorder` protocol | same open/close lifecycle |
| `BotRegistry` / `BotConfig` / `TriggerSpec` | same names | YAML shape unchanged (includes, triggers, mutual exclusion) |
| `BotScheduler` (ET hardcoded, `TradingCalendar`) | `BotScheduler` | timezone + calendar injectable |
| OTel spans / structured logging / metrics | `on_run_context` hook + stdlib logging | observability is host wiring |

## Development

```bash
uv sync                 # installs with dev group (pytest, apscheduler)
uv run pytest           # 40-ish tests, no infrastructure needed
uv run python examples/quickstart.py
```
