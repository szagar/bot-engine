# Ports reference — implementing the host side

Every integration point between your platform and the engine is a `typing.Protocol`
in `bot_engine.ports` — structural typing, so your classes implement them without
importing anything (though importing the protocols gives you static checks).

| Protocol | Required? | Called | Ships with |
|---|---|---|---|
| `StateStore` | yes (`context.state`) | by bots via `get_state`/`set_state` | `InMemoryStateStore` |
| `Clock` | yes (`context.clock`) | by bots via `ctx.clock` | `WallClock`, `FixedClock` |
| `EnablementGate` | optional (executor) | once per fire, **fresh every time** | `AlwaysEnabled` |
| `RunRecorder` | optional (executor) | open before / close after each fire | — |
| `TradingCalendar` | optional (scheduler) | once per scheduled fire | — |
| `get_watchlist_symbols` | optional (context method) | by `bot.get_underlyings()` | — |

---

## StateStore

```python
class StateStore(Protocol):
    async def get(self, bot_name: str, key: str, default: Any = None) -> Any: ...
    async def set(self, bot_name: str, key: str, value: Any) -> None: ...
    async def delete(self, bot_name: str, key: str) -> None: ...
    async def clear_all(self, bot_name: str) -> None: ...
```

Contract: values are JSON-serialisable; keys are namespaced by `bot_name` (the
YAML name), so concurrent bots never collide. Bots use state for things like
"already entered today", cooldown timestamps, and rolling counters — small,
frequently-read, must survive restarts.

### Postgres implementation

```sql
CREATE TABLE bot_state (
    bot_name    text        NOT NULL,
    key         text        NOT NULL,
    value       jsonb       NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_name, key)
);
```

```python
import json

class PgStateStore:
    def __init__(self, pool) -> None:          # asyncpg.Pool
        self._pool = pool

    async def get(self, bot_name, key, default=None):
        row = await self._pool.fetchval(
            "SELECT value FROM bot_state WHERE bot_name=$1 AND key=$2", bot_name, key)
        return default if row is None else json.loads(row)

    async def set(self, bot_name, key, value):
        await self._pool.execute(
            """INSERT INTO bot_state (bot_name, key, value) VALUES ($1,$2,$3)
               ON CONFLICT (bot_name, key)
               DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            bot_name, key, json.dumps(value))

    async def delete(self, bot_name, key):
        await self._pool.execute(
            "DELETE FROM bot_state WHERE bot_name=$1 AND key=$2", bot_name, key)

    async def clear_all(self, bot_name):
        await self._pool.execute("DELETE FROM bot_state WHERE bot_name=$1", bot_name)
```

### Redis implementation

```python
class RedisStateStore:
    def __init__(self, redis) -> None:
        self._r = redis

    @staticmethod
    def _key(bot_name: str) -> str:
        return f"bot_state:{bot_name}"

    async def get(self, bot_name, key, default=None):
        raw = await self._r.hget(self._key(bot_name), key)
        return default if raw is None else json.loads(raw)

    async def set(self, bot_name, key, value):
        await self._r.hset(self._key(bot_name), key, json.dumps(value))

    async def delete(self, bot_name, key):
        await self._r.hdel(self._key(bot_name), key)

    async def clear_all(self, bot_name):
        await self._r.delete(self._key(bot_name))
```

**Choosing:** state read/write volume is per-fire, not per-tick, so Postgres
alone is fast enough for almost every host and is durable by default. Use Redis
only if it's already on your critical path; if you must have both, make the
database the source of truth and treat Redis as a cache.

---

## Clock

```python
class Clock(Protocol):
    def now(self) -> datetime: ...
    def today(self) -> date: ...
```

Use the shipped `WallClock(tz)` live and `FixedClock(at)` in tests/backtests.
Give `WallClock` your market timezone (`"America/New_York"`) so `today()` rolls
over at midnight ET, not UTC — a UTC clock makes "already entered today" state
checks wrong for the 7–8 PM ET window.

---

## EnablementGate

```python
class EnablementGate(Protocol):
    async def resolve(
        self, *, account: str, bot_name: str, role: BotRole | None
    ) -> tuple[bool, str]: ...
```

Resolved **fresh on every fire** — this is the dynamic kill switch operators flip
without restarting the engine (the YAML `enabled:` flag is the static one).
Returns `(enabled, reason)`; the reason is a short greppable string recorded on
the skip (`"bot disabled (flag=0)"`).

**Failure semantics are the whole design.** When the flag store is unreachable
or the flag is undefined, fall back on `role_default(role)`:

- `ENTRY` → **disabled** (fail-closed: never open new risk blind)
- `EXIT` → **enabled** (fail-open: never strand an open position)

### Postgres gate (recommended default)

```sql
CREATE TABLE bot_flags (
    account    text        NOT NULL,
    bot_name   text        NOT NULL,
    enabled    boolean     NOT NULL,
    changed_by text,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, bot_name)
);
```

```python
from bot_engine import role_default

class PgEnablementGate:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def resolve(self, *, account, bot_name, role):
        try:
            row = await self._pool.fetchval(
                "SELECT enabled FROM bot_flags WHERE account=$1 AND bot_name=$2",
                account, bot_name)
        except Exception:
            return role_default(role), f"flag_store_unavailable role={role}"
        if row is None:
            return role_default(role), "flag_undefined"
        return bool(row), f"flag={'1' if row else '0'}"
```

### Redis gate

```python
from redis.exceptions import RedisError
from bot_engine import role_default

class RedisGate:
    def __init__(self, redis) -> None:
        self._r = redis

    async def resolve(self, *, account, bot_name, role):
        try:
            raw = await self._r.hget("bots:enabled", f"{account}:{bot_name}")
        except RedisError:
            return role_default(role), f"redis_unavailable role={role}"
        if raw is None:
            return role_default(role), "flag_undefined"
        return raw == b"1", f"flag={raw.decode()}"
```

**Choosing:** prefer the database gate unless Redis is already unavoidably on
your critical path (e.g. it backs your `StateStore`). The flag is durable
operator intent — a Redis restart without persistence silently reverts every
flag to `flag_undefined`, undoing an operator's explicit disable; a DB row
can't lose it, gives you `changed_by`/`changed_at` audit for free, and joins
against `bot_runs`. Read volume (one lookup per fire) is trivially low either
way. A gate flip is one SQL statement from your ops CLI:

```sql
INSERT INTO bot_flags (account, bot_name, enabled, changed_by)
VALUES ('live', 'spx_ic_16d_5w', false, 'szagar')
ON CONFLICT (account, bot_name)
DO UPDATE SET enabled=EXCLUDED.enabled, changed_by=EXCLUDED.changed_by, changed_at=now();
```

---

## RunRecorder

```python
class RunRecorder(Protocol):
    async def open(self, *, run_id: str, bot: str, account: str,
                   underlying: str | None) -> Any: ...
    async def close(self, handle: Any, *, result: str, duration_ms: int,
                    skip_reason: str | None = None, error: str | None = None,
                    order_id: str | None = None, trade_group_id: int | None = None,
                    data: dict[str, Any] | None = None) -> None: ...
```

One row per fire — your durable audit trail ("what did the bots do and why").
Both calls are **best-effort**: if they raise, the executor logs a warning and
the fire proceeds; a recorder outage must never block trading.

```sql
CREATE TABLE bot_runs (
    run_id         text PRIMARY KEY,
    bot            text NOT NULL,
    account        text NOT NULL,
    underlying     text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    result         text,          -- submitted_entry | submitted_exit | no_action | skipped | error
    duration_ms    integer,
    skip_reason    text,
    error          text,
    order_id       text,
    trade_group_id bigint,
    data           jsonb
);
CREATE INDEX ON bot_runs (bot, started_at DESC);
```

```python
class PgRunRecorder:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def open(self, *, run_id, bot, account, underlying):
        await self._pool.execute(
            "INSERT INTO bot_runs (run_id, bot, account, underlying) VALUES ($1,$2,$3,$4)",
            run_id, bot, account, underlying)
        return run_id                      # the handle passed back to close()

    async def close(self, handle, *, result, duration_ms, skip_reason=None,
                    error=None, order_id=None, trade_group_id=None, data=None):
        if handle is None:                 # open() failed — nothing to close
            return
        await self._pool.execute(
            """UPDATE bot_runs SET finished_at=now(), result=$2, duration_ms=$3,
               skip_reason=$4, error=$5, order_id=$6, trade_group_id=$7, data=$8
               WHERE run_id=$1""",
            handle, result, duration_ms, skip_reason, error,
            order_id, trade_group_id, json.dumps(data) if data else None)
```

Note `open()` can return `None` on failure (the executor passes whatever it got
back to `close()`), so `close()` must tolerate a `None` handle.

---

## TradingCalendar

```python
class TradingCalendar(Protocol):
    def is_trading_day(self, day: date) -> bool: ...
```

Given to `BotScheduler`; scheduled fires on non-trading days are skipped with a
log line. **Manual triggers (`trigger_now`) bypass it** — signals and operators
outrank the calendar. With `exchange_calendars`:

```python
import exchange_calendars as xcals

class NyseCalendar:
    def __init__(self) -> None:
        self._cal = xcals.get_calendar("XNYS")

    def is_trading_day(self, day) -> bool:
        return self._cal.is_session(day)
```

This gates whole days only. Intraday windows (avoid the first 5 minutes, stop
entries after 3:30 PM) belong in cron expressions or in the bot's own
`SkipExecution` checks.

---

## get_watchlist_symbols (context method)

```python
async def get_watchlist_symbols(self, name: str) -> set[str]: ...
```

Optional method on your context; required only if any bot declares
`watchlist: "..."` instead of `underlying`. `bot.get_underlyings()` calls it and
returns the symbols sorted (deterministic iteration). An empty watchlist raises
`SkipExecution` — an empty "open positions to manage" list is a normal no-op,
not an error.
