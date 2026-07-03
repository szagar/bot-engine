# Integration checklist

Work top to bottom; the first block is the minimum to run anything, the rest is
what separates a demo from a production trading service.

## Minimum viable integration

- [ ] Context dataclass defined with `state: StateStore` and `clock: Clock`
      plus your trading ports (quotes / broker / positions).
- [ ] `WallClock` constructed with your **market timezone**, not UTC.
- [ ] At least one bot subclassing `BaseBot[YourContext]` with
      `role = BotRole.ENTRY | EXIT` and an `execute()` that raises
      `SkipExecution` for every "conditions not met" path.
- [ ] `bots.yaml` written; bot names are stable identifiers (state, flags, and
      audit rows key off them).
- [ ] `registry.validate_roles()` called at startup.
- [ ] `BotExecutor` constructed per account; smoke-tested with
      `await executor.run(registry.get(name))`.
- [ ] `BotScheduler` (install `bot-engine[scheduler]`) with
      `timezone="America/New_York"` (or your market); `setup()` + `start()`
      inside a running event loop.

## Durability & control plane

- [ ] `StateStore` backed by durable storage (Postgres table or persistent
      Redis) — `InMemoryStateStore` loses "already entered today" on every
      restart, which means double entries.
- [ ] `EnablementGate` implemented over a durable flag store, using
      `role_default()` when the store is unreachable or the flag undefined.
      Verify both directions: ENTRY bot skips during a flag-store outage,
      EXIT bot still runs.
- [ ] Ops path to flip flags (CLI / dashboard) that does **not** require an
      engine restart.
- [ ] `RunRecorder` writing `bot_runs` rows; confirmed that a recorder failure
      only warns and the fire still completes.

## Market correctness

- [ ] `TradingCalendar` wired so scheduled fires skip holidays; confirmed
      `trigger_now` still works on a holiday (by design).
- [ ] Cron expressions reviewed against the scheduler timezone (a `14:00`
      schedule in UTC is 10:00 ET — a classic mis-fire).
- [ ] Bots never call `datetime.now()` / `date.today()` directly (grep for it) —
      only `self.ctx.clock`.
- [ ] Idempotency per fire: every entry bot has a state- or position-based
      guard so a manual re-trigger cannot double-submit.
- [ ] Order submission is the *last* side effect in `execute()`; state marking
      the entry is written immediately after the order id returns.

## Signals (if used)

- [ ] Trigger-only bots have `schedule: ""` and `triggers.on_signal` predicates.
- [ ] Consumer loop maps payloads to the arrival dict
      (`signal` / `symbol` / `signal_kind` / `direction`) and calls
      `registry.match_triggers(arrival)` → `scheduler.trigger_now(...)`.
- [ ] Duplicate-signal protection (the engine dedupes nothing — same signal
      twice fires the bot twice; the bot's state guard must absorb it).

## Testing

- [ ] Per-bot unit tests with `FixedClock` + `InMemoryStateStore` + stub
      quotes/broker covering: happy path, each skip reason, and the
      double-fire-same-day guard.
- [ ] A test that YAML parameter overrides actually land (typo'd parameter
      names are *silently ignored* by design).
- [ ] Gate fallback tests: flag store down → ENTRY skipped, EXIT ran.
- [ ] `uv run pytest` green, including the engine's own suite if vendored.

## Observability & ops

- [ ] JSON log formatter emitting the `extra` fields (`bot`, `account`,
      `run_id`, `outcome`, `timing_ms`, `reason`).
- [ ] `on_run_context` binding `run_id` into contextvars/OTel so adapter logs
      correlate with the fire.
- [ ] Alerting on `result="error"` in `bot_runs` (skips are normal; errors are
      not).
- [ ] Startup log prints `scheduler.summary_lines()` — job list + next fire
      times.
- [ ] Deployment: single process per engine; restart is safe (misfire grace is
      5 min; `stop()` waits for in-flight fires; durable state carries over).
- [ ] Paper account wired as a second `AccountBots` group before going live.
