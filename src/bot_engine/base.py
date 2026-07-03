"""BaseBot — abstract base class for all trading bots.

Bots inherit from ``BaseBot`` and implement ``execute()``. Configurable
parameters are declared as annotated class attributes with defaults; the
registry's parameter overrides are applied at construction with automatic
type coercion. State persistence is scoped to the bot name so concurrent
bots never collide.

Bots are generic over the host's context type, so ``self.ctx`` is fully
typed against whatever capabilities the host provides::

    class WeeklyIronCondorBot(BaseBot["MyContext"]):
        role = BotRole.ENTRY

        underlying: str = "SPX"
        target_dte: int = 45
        short_delta: float = 0.16
        min_iv_rank: float = 30.0

        async def execute(self) -> ExecutionResult:
            iv_rank = await self.ctx.get_iv_rank(self.underlying)
            if iv_rank is None or iv_rank < self.min_iv_rank:
                raise SkipExecution(f"IV rank {iv_rank} below {self.min_iv_rank}")
            ...
            return ExecutionResult(action="submitted_entry", order_id=oid)
"""

from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from bot_engine.ports import BotContext

CtxT = TypeVar("CtxT", bound=BotContext)


class BotRole(str, Enum):
    """Whether a bot opens new positions or manages/closes existing ones.

    This is **safety-critical** for any runtime enable gate: when the enable
    flag cannot be resolved (flag store unavailable or flag undefined), gates
    should fall back on the role —

    * ``ENTRY`` → **disabled** (fail-closed: never open new risk blind).
    * ``EXIT``  → **enabled**  (fail-open: always able to manage/close).

    Misclassifying an exit as an entry would, during a control-plane outage,
    prevent closing an open position — so every concrete bot must declare its
    role, enforced at startup via :meth:`bot_engine.registry.BotRegistry.validate_roles`.
    """

    ENTRY = "entry"
    EXIT = "exit"


@dataclass
class ExecutionResult:
    """Result returned from :meth:`BaseBot.execute`.

    Attributes:
        action:         What the bot did. Conventional values:
                        ``"submitted_entry"``, ``"submitted_exit"``,
                        ``"no_action"``. The executor reserves ``"skipped"``.
        order_id:       Host-side order identifier, if an order was submitted.
        trade_group_id: Trade group / position-group identifier, if any.
        data:           Arbitrary key-value pairs for logging and display
                        (e.g. strikes, net credit, expiration dates).
    """

    action: str
    order_id: str | None = None
    trade_group_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class SkipExecution(Exception):
    """Raised when a bot decides not to trade this cycle (not an error).

    The executor records this as ``action="skipped"`` and does not alert.

    Example::

        if iv_rank is None or iv_rank < self.min_iv_rank:
            raise SkipExecution(f"IV rank {iv_rank} below {self.min_iv_rank}")
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BaseBot(ABC, Generic[CtxT]):
    """Abstract base class for all trading bots.

    Subclasses declare configurable parameters as annotated class attributes
    with defaults. The executor applies the registry's parameter overrides via
    ``_apply_parameters()``, which coerces types automatically based on the
    annotation (JSON/YAML values often arrive as strings).

    State persistence is available through :meth:`get_state` /
    :meth:`set_state`, scoped to the bot name.

    Underlying resolution:
        Bots can specify either a single ``underlying`` or a ``watchlist``
        (mutually exclusive — enforced by the registry). Use
        :meth:`get_underlyings` to resolve the symbol(s) to iterate over.
    """

    # Every concrete bot must declare its role (entry vs exit). Left as None
    # on the base so ``BotRegistry.validate_roles()`` can detect and reject
    # any bot that forgot to set it, rather than silently guessing.
    role: ClassVar[BotRole | None] = None

    # Watchlist support — subclasses may instead declare an ``underlying``
    # class attribute (a plain ``underlying: str = "..."``).
    watchlist: str | None = None

    def __init__(
        self,
        name: str,
        parameters: dict[str, Any],
        context: CtxT,
    ) -> None:
        """Initialise bot.

        Args:
            name:       Bot name from the registry (used for state scoping).
            parameters: Parameter overrides from the registry. Only keys
                        matching existing class attributes are applied.
            context:    Host context providing state, clock, and trading
                        capabilities.
        """
        self.name = name
        self.ctx: CtxT = context
        self._apply_parameters(parameters)

    # -------------------------------------------------------------------------
    # Abstract interface
    # -------------------------------------------------------------------------

    @abstractmethod
    async def execute(self) -> ExecutionResult:
        """Execute bot logic for this cycle.

        Returns:
            ExecutionResult describing the action taken.

        Raises:
            SkipExecution: Graceful no-op — recorded as "skipped", not "error".
            Exception:     Any other exception is recorded as "error".
        """

    # -------------------------------------------------------------------------
    # State persistence (scoped to self.name)
    # -------------------------------------------------------------------------

    async def get_state(self, key: str, default: Any = None) -> Any:
        """Read a persistent state value.

        Example::

            last_entry = await self.get_state("last_entry_date")
            if last_entry == self.ctx.clock.today().isoformat():
                raise SkipExecution("Already entered today")
        """
        return await self.ctx.state.get(self.name, key, default)

    async def set_state(self, key: str, value: Any) -> None:
        """Write a persistent state value. Value must be JSON-serialisable."""
        await self.ctx.state.set(self.name, key, value)

    async def delete_state(self, key: str) -> None:
        """Delete a state key."""
        await self.ctx.state.delete(self.name, key)

    async def clear_all_state(self) -> None:
        """Delete all state for this bot. Use with caution."""
        await self.ctx.state.clear_all(self.name)

    # -------------------------------------------------------------------------
    # Underlying resolution (single or watchlist)
    # -------------------------------------------------------------------------

    async def get_underlyings(self) -> list[str]:
        """Resolve the underlying symbol(s) for this bot to iterate over.

        Bots can specify either:

        * ``underlying: str`` — a single symbol
        * ``watchlist: str`` — the name of a host-managed watchlist; requires
          the context to provide ``get_watchlist_symbols(name) -> set[str]``

        Returns a one-element list for a single underlying, or the sorted
        watchlist symbols (deterministic iteration order).

        Raises:
            SkipExecution: If the watchlist resolves to no symbols.
            ValueError:    If neither is configured, or ``watchlist`` is set
                           but the context has no ``get_watchlist_symbols``.
        """
        underlying = getattr(self, "underlying", None)
        if underlying:
            return [underlying]

        if self.watchlist:
            resolver = getattr(self.ctx, "get_watchlist_symbols", None)
            if resolver is None:
                raise ValueError(
                    f"Bot {self.name} uses watchlist '{self.watchlist}' but the "
                    "context does not provide get_watchlist_symbols()"
                )
            symbols = await resolver(self.watchlist)
            if not symbols:
                raise SkipExecution(f"Watchlist '{self.watchlist}' is empty")
            return sorted(set(symbols))

        raise ValueError(f"Bot {self.name} has neither 'underlying' nor 'watchlist' configured")

    # -------------------------------------------------------------------------
    # Parameter application
    # -------------------------------------------------------------------------

    def _apply_parameters(self, parameters: dict[str, Any]) -> None:
        """Apply registry parameter overrides to instance attributes.

        Only parameters whose names match existing class attributes are
        applied. Unknown keys are silently ignored — a config typo must not
        break bot startup.

        Type coercion is driven by PEP 526 annotations on the bot class (and
        its parent classes). Uses ``typing.get_type_hints()`` so that
        ``from __future__ import annotations`` string annotations resolve.

        Supported coercions (value arriving as str from JSON/YAML):
            str → str | int | float | bool | Decimal
        """
        try:
            hints = typing.get_type_hints(type(self))
        except Exception:
            # Fallback: collect raw __annotations__ from the MRO.
            hints = {}
            for cls in reversed(type(self).__mro__):
                hints.update(getattr(cls, "__annotations__", {}))

        for key, value in parameters.items():
            if not hasattr(self, key):
                continue

            if value is not None:
                expected = hints.get(key)
                value = self._coerce(value, expected)

            setattr(self, key, value)

    @staticmethod
    def _coerce(value: Any, expected: type | None) -> Any:
        """Coerce a value to the expected type if it arrived as a string."""
        if not isinstance(value, str):
            return value
        if expected is Decimal:
            return Decimal(value)
        if expected is int:
            return int(value)
        if expected is float:
            return float(value)
        if expected is bool:
            return value.lower() in ("true", "1", "yes")
        return value
