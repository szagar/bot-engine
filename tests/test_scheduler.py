"""BotScheduler — job registration, trigger_now, calendar gate."""

from __future__ import annotations

import asyncio
from datetime import date

from bot_engine import BotConfig, BotExecutor, BotRegistry
from bot_engine.scheduler import AccountBots, BotScheduler


def make_registry(tmp_path, content: str) -> BotRegistry:
    p = tmp_path / "bots.yaml"
    p.write_text(content, encoding="utf-8")
    return BotRegistry.from_file(p)


YAML = """
scheduled_bot:
  class_path: "tests.test_registry_bots.GoodBot"
  schedule: "0 14 * * mon-fri"
  enabled: true

trigger_only_bot:
  class_path: "tests.test_registry_bots.GoodBot"
  schedule: ""
  enabled: true

disabled_bot:
  class_path: "tests.test_registry_bots.GoodBot"
  schedule: "0 9 * * *"
  enabled: false
"""


class ClosedCalendar:
    def is_trading_day(self, day: date) -> bool:
        return False


async def test_setup_registers_only_enabled_scheduled(tmp_path, ctx):
    registry = make_registry(tmp_path, YAML)
    scheduler = BotScheduler(
        [AccountBots("acct", registry, BotExecutor(ctx, account="acct"))],
        timezone="America/New_York",
    )
    scheduler.setup()
    assert scheduler.scheduled_jobs() == ["acct:scheduled_bot"]
    assert any("scheduled_bot" in line for line in scheduler.summary_lines())


async def test_trigger_now_runs_trigger_only_bot(tmp_path, ctx):
    registry = make_registry(tmp_path, YAML)

    ran: list[str] = []

    class SpyExecutor(BotExecutor):
        async def run(self, config: BotConfig):
            ran.append(config.name)
            return await super().run(config)

    scheduler = BotScheduler([AccountBots("acct", registry, SpyExecutor(ctx, account="acct"))])
    await scheduler.trigger_now("acct", "trigger_only_bot")
    await asyncio.sleep(0.05)  # let the background task complete
    assert ran == ["trigger_only_bot"]


async def test_trigger_now_unknown_bot_raises(tmp_path, ctx):
    registry = make_registry(tmp_path, YAML)
    scheduler = BotScheduler([AccountBots("acct", registry, BotExecutor(ctx))])
    try:
        await scheduler.trigger_now("acct", "nope")
        raise AssertionError("expected KeyError")
    except KeyError:
        pass


async def test_calendar_blocks_scheduled_but_not_manual(tmp_path, ctx):
    registry = make_registry(tmp_path, YAML)

    ran: list[str] = []

    class SpyExecutor(BotExecutor):
        async def run(self, config: BotConfig):
            ran.append(config.name)
            return await super().run(config)

    executor = SpyExecutor(ctx, account="acct")
    scheduler = BotScheduler(
        [AccountBots("acct", registry, executor)], calendar=ClosedCalendar()
    )
    config = registry.get("scheduled_bot")

    # Scheduled path respects the calendar
    await scheduler._run_bot("acct", config, executor)
    assert ran == []

    # Manual trigger bypasses it
    await scheduler._run_bot("acct", config, executor, allow_non_trading_day=True)
    assert ran == ["scheduled_bot"]
