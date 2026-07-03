"""Clock implementations — wall clock for live, fixed clock for tests/backtests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


class WallClock:
    """Real time, in a configurable timezone (default UTC)."""

    def __init__(self, tz: str | ZoneInfo = UTC) -> None:  # type: ignore[assignment]
        self._tz = ZoneInfo(tz) if isinstance(tz, str) else tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def today(self) -> date:
        return self.now().date()


class FixedClock:
    """A clock frozen at a given instant. Advance manually with :meth:`advance_to`.

    Use in tests and backtests so bot logic that reads ``ctx.clock`` runs
    against simulated time.
    """

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        self._now = at

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def advance_to(self, at: datetime) -> None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        self._now = at
