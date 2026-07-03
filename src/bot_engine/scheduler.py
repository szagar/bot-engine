"""BotScheduler — runs bots on their cron schedules.

Wraps APScheduler's AsyncIOScheduler (install the ``scheduler`` extra).
Each enabled bot with a non-empty schedule gets one job; ``max_instances=1``
prevents a slow bot from accumulating overlapping runs.

Bots with ``schedule: ""`` are trigger-only — fire them via
:meth:`BotScheduler.trigger_now` (e.g. from a signal-consumer loop) but they
are never scheduled automatically.

Example::

    registry = BotRegistry.from_file("config/bots.yaml")
    executor = BotExecutor(context=ctx, account="individual")
    scheduler = BotScheduler(
        [AccountBots("individual", registry, executor)],
        timezone="America/New_York",
        calendar=my_calendar,          # optional TradingCalendar
    )

    scheduler.setup()   # registers all enabled bots
    scheduler.start()

    await scheduler.trigger_now("individual", "spx_ic_16d_5w")

    await scheduler.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from bot_engine.executor import BotExecutor
from bot_engine.registry import BotConfig, BotRegistry

if TYPE_CHECKING:
    from bot_engine.ports import TradingCalendar

logger = logging.getLogger(__name__)

# Missed-fire grace period: if the scheduler was down, run any job missed
# within this window (seconds). Beyond it, skip and wait for the next fire.
_MISFIRE_GRACE_SECONDS = 300


@dataclass
class AccountBots:
    """One account's bots, with the executor bound to that account's context."""

    account: str
    registry: BotRegistry
    executor: BotExecutor


class BotScheduler:
    """Schedules enabled bots using APScheduler cron triggers.

    One engine can drive multiple accounts (e.g. a live and a paper account);
    each account has its own registry + executor. Job ids are namespaced
    ``{account}:{bot}`` so the same bot name can run under more than one
    account without colliding.

    Args:
        groups:   Per-account ``(account, registry, executor)`` bundles.
        timezone: IANA timezone for cron evaluation (default UTC).
        calendar: Optional market calendar; scheduled fires on non-trading
                  days are skipped (manual triggers bypass it).
    """

    def __init__(
        self,
        groups: list[AccountBots],
        *,
        timezone: str | ZoneInfo = "UTC",
        calendar: TradingCalendar | None = None,
    ) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "BotScheduler requires APScheduler — install bot-engine[scheduler]"
            ) from exc

        self._groups = groups
        self._tz = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
        self._scheduler = AsyncIOScheduler(timezone=self._tz)
        self._calendar = calendar
        # job_id ("{account}:{bot}") -> (account, config, executor)
        self._jobs: dict[str, tuple[str, BotConfig, BotExecutor]] = {}

    @staticmethod
    def _job_id(account: str, bot: str) -> str:
        return f"{account}:{bot}"

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def setup(self) -> None:
        """Register all enabled bots that have a cron schedule.

        Safe to call multiple times — existing jobs are replaced. Bots with
        ``schedule: ""`` are skipped (trigger-only).
        """
        scheduled = 0
        for group in self._groups:
            for config in group.registry.all_bots():
                if not config.enabled or not config.schedule:
                    continue
                self._add_job(group, config)
                scheduled += 1
        logger.info("Scheduler setup complete", extra={"scheduled": scheduled})

    def start(self) -> None:
        """Start the scheduler (requires a running asyncio event loop)."""
        self._scheduler.start()
        logger.info("Scheduler started", extra={"job_count": len(self._scheduler.get_jobs())})

    async def stop(self) -> None:
        """Shut down gracefully, waiting for running jobs to complete."""
        self._scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped")

    # -------------------------------------------------------------------------
    # Manual trigger
    # -------------------------------------------------------------------------

    async def trigger_now(self, account: str, bot_name: str) -> None:
        """Run a bot immediately, outside its normal schedule.

        Useful for trigger-only bots, signal-driven execution, and testing a
        bot without waiting for its next cron fire. The bot runs in a
        background asyncio task so this coroutine returns immediately.

        Raises:
            KeyError: If ``(account, bot_name)`` is not registered.
        """
        config = None
        executor = None
        for group in self._groups:
            if group.account == account:
                config = group.registry.get(bot_name)
                executor = group.executor
                break
        if config is None or executor is None:
            raise KeyError(f"Bot '{bot_name}' not found for account '{account}'")

        logger.info("Manual trigger requested", extra={"account": account, "bot": bot_name})
        asyncio.create_task(
            self._run_bot(account, config, executor, allow_non_trading_day=True),
            name=f"trigger:{self._job_id(account, bot_name)}",
        )

    # -------------------------------------------------------------------------
    # Introspection
    # -------------------------------------------------------------------------

    def scheduled_jobs(self) -> list[str]:
        """Return the ids of all currently scheduled jobs."""
        return [job.id for job in self._scheduler.get_jobs()]

    def summary_lines(self) -> list[str]:
        """Return a formatted summary table of scheduled bots and next fire times."""
        jobs = self._scheduler.get_jobs()
        if not jobs:
            return ["  (no scheduled bots)"]

        # next_run_time is absent on jobs added before the scheduler starts.
        max_dt = datetime.max.replace(tzinfo=self._tz)
        rows: list[tuple[str, str, str]] = []
        for job in sorted(jobs, key=lambda j: getattr(j, "next_run_time", None) or max_dt):
            entry = self._jobs.get(job.id)
            schedule = entry[1].schedule if entry else "?"
            nrt = getattr(job, "next_run_time", None)
            next_fire = nrt.strftime("%a %b %d %I:%M %p %Z") if nrt else "—"
            rows.append((job.id, schedule, next_fire))

        name_w = max(len(r[0]) for r in rows)
        sched_w = max(len(r[1]) for r in rows)
        lines = [
            f"  {'Bot':<{name_w}}  {'Schedule':<{sched_w}}  Next Fire",
            f"  {'-' * name_w}  {'-' * sched_w}  {'-' * 28}",
        ]
        for name, sched, nf in rows:
            lines.append(f"  {name:<{name_w}}  {sched:<{sched_w}}  {nf}")
        return lines

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _add_job(self, group: AccountBots, config: BotConfig) -> None:
        from apscheduler.triggers.cron import CronTrigger

        job_id = self._job_id(group.account, config.name)
        trigger = CronTrigger.from_crontab(config.schedule, timezone=self._tz)
        self._scheduler.add_job(
            self._run_bot,
            trigger=trigger,
            args=[group.account, config, group.executor],
            id=job_id,
            name=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        )
        self._jobs[job_id] = (group.account, config, group.executor)
        logger.info(
            "Bot scheduled",
            extra={"account": group.account, "bot": config.name, "schedule": config.schedule},
        )

    async def _run_bot(
        self,
        account: str,
        config: BotConfig,
        executor: BotExecutor,
        *,
        allow_non_trading_day: bool = False,
    ) -> None:
        """Execute one bot cycle, catching all exceptions so the scheduler survives."""
        if self._calendar is not None and not allow_non_trading_day:
            today = datetime.now(self._tz).date()
            if not self._calendar.is_trading_day(today):
                logger.info(
                    "Bot skipped (non-trading day)",
                    extra={"account": account, "bot": config.name, "date": today.isoformat()},
                )
                return

        t0 = time.monotonic()
        try:
            result = await executor.run(config)
            logger.info(
                "Bot cycle complete",
                extra={
                    "account": account,
                    "bot": config.name,
                    "action": result.action,
                    "timing_ms": int((time.monotonic() - t0) * 1000),
                },
            )
        except Exception:
            logger.exception(
                "Bot cycle failed with unhandled exception",
                extra={"account": account, "bot": config.name},
            )
