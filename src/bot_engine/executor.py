"""BotExecutor — runs a single bot cycle and returns the result.

The executor is the only component that calls ``bot.execute()``. It

* mints a fresh ``run_id`` (the bot-run correlation identifier),
* resolves the runtime enable gate (a disabled bot no-ops through the
  normal skip path — the engine process stays alive),
* opens/closes a :class:`~bot_engine.ports.RunRecorder` row around the run
  (best-effort — recorder failures never block the fire),
* maps ``SkipExecution`` → ``ExecutionResult(action="skipped")`` and lets
  real exceptions propagate after recording them.

Example::

    executor = BotExecutor(context=ctx, account="individual")

    config = registry.get("spx_ic_16d_5w")
    result = await executor.run(config)

    if result.action == "skipped":
        print("skipped:", result.data.get("reason"))
    elif result.action == "submitted_entry":
        print("order submitted:", result.order_id)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

from bot_engine.base import ExecutionResult, SkipExecution
from bot_engine.loader import load_bot_class
from bot_engine.ports import AlwaysEnabled, BotContext, EnablementGate, RunRecorder

if TYPE_CHECKING:
    from bot_engine.registry import BotConfig

logger = logging.getLogger(__name__)


def mint_run_id(bot_name: str, underlying: str | None = None) -> str:
    """Default run-id minter: ``run-{bot}-{underlying|ANY}-{8hex}``.

    Short, greppable, and unique per fire. Inject your own minter into
    :class:`BotExecutor` to match a house correlation-id convention.
    """
    slug = underlying or "ANY"
    return f"run-{bot_name}-{slug}-{uuid.uuid4().hex[:8]}"


def _extract_underlying(config: BotConfig) -> str | None:
    """Best-effort underlying for the run-id slug (None for watchlist bots)."""
    underlying = (config.parameters or {}).get("underlying")
    if isinstance(underlying, str) and underlying:
        return underlying
    return None


class BotExecutor:
    """Runs a configured bot for one cycle.

    The executor is stateless — it creates a fresh bot instance for every
    call to :meth:`run`. All persistent bot state lives in the context's
    :class:`~bot_engine.ports.StateStore`.

    Args:
        context:  Host context passed to every bot (must satisfy
                  :class:`~bot_engine.ports.BotContext`).
        account:  Label for the account this executor fires against —
                  recorded on runs and passed to the enable gate. One
                  executor per account is the intended shape.
        gate:     Runtime enable gate; defaults to
                  :class:`~bot_engine.ports.AlwaysEnabled`.
        recorder: Optional per-run audit persistence.
        run_id_minter: Correlation-id factory; defaults to :func:`mint_run_id`.
        on_run_context: Optional callable invoked with a dict of run fields
                  (``run_id``, ``bot``, ``account``) before each fire — hook
                  point for binding correlation IDs into a logging/tracing
                  context. Return value is ignored.
    """

    def __init__(
        self,
        context: BotContext,
        *,
        account: str = "default",
        gate: EnablementGate | None = None,
        recorder: RunRecorder | None = None,
        run_id_minter: Callable[[str, str | None], str] = mint_run_id,
        on_run_context: Callable[[dict[str, str]], Any] | None = None,
    ) -> None:
        self._ctx = context
        self._account = account
        self._gate = gate if gate is not None else AlwaysEnabled()
        self._recorder = recorder
        self._mint = run_id_minter
        self._on_run_context = on_run_context

    async def run(self, config: BotConfig) -> ExecutionResult:
        """Execute one cycle of the bot described by *config*.

        Lifecycle:

        1. Mint ``run_id``; call ``on_run_context`` hook.
        2. Open a recorder row (best effort).
        3. Load the bot class and instantiate with name/parameters/context.
        4. Resolve the enable gate — disabled → skip.
        5. Call ``bot.execute()``.
        6. ``SkipExecution`` is returned as ``ExecutionResult(action="skipped")``.
        7. Any other exception propagates to the caller after the recorder
           row is closed with ``result="error"``.
        """
        underlying = _extract_underlying(config)
        run_id = self._mint(config.name, underlying)

        if self._on_run_context is not None:
            try:
                self._on_run_context({"run_id": run_id, "bot": config.name, "account": self._account})
            except Exception:
                logger.warning("on_run_context hook failed", exc_info=True)

        handle = await self._recorder_open(run_id, config, underlying)

        bot_class = load_bot_class(config.class_path)
        bot = bot_class(name=config.name, parameters=config.parameters, context=self._ctx)

        logger.info(
            "Bot fire start",
            extra={"bot": config.name, "account": self._account, "run_id": run_id},
        )

        t0 = time.monotonic()
        try:
            enabled, gate_reason = await self._gate.resolve(
                account=self._account, bot_name=config.name, role=bot_class.role
            )
            if not enabled:
                raise SkipExecution(f"bot disabled ({gate_reason})")
            result = await bot.execute()
        except SkipExecution as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "Bot skipped",
                extra={"bot": config.name, "run_id": run_id, "reason": exc.reason},
            )
            await self._recorder_close(
                handle, result="skipped", duration_ms=elapsed_ms, skip_reason=exc.reason
            )
            return ExecutionResult(action="skipped", data={"reason": exc.reason})
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.exception(
                "Bot run failed", extra={"bot": config.name, "run_id": run_id}
            )
            await self._recorder_close(
                handle,
                result="error",
                duration_ms=elapsed_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "Bot fire complete",
            extra={
                "bot": config.name,
                "run_id": run_id,
                "outcome": result.action,
                "order_id": result.order_id,
                "timing_ms": elapsed_ms,
            },
        )
        await self._recorder_close(
            handle,
            result=result.action,
            duration_ms=elapsed_ms,
            order_id=result.order_id,
            trade_group_id=result.trade_group_id,
            data=result.data,
        )
        return result

    # -------------------------------------------------------------------------
    # Recorder wrappers — best-effort, never block the run
    # -------------------------------------------------------------------------

    async def _recorder_open(
        self, run_id: str, config: BotConfig, underlying: str | None
    ) -> Any:
        if self._recorder is None:
            return None
        try:
            return await self._recorder.open(
                run_id=run_id, bot=config.name, account=self._account, underlying=underlying
            )
        except Exception:
            logger.warning("RunRecorder.open failed", exc_info=True)
            return None

    async def _recorder_close(self, handle: Any, **kwargs: Any) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.close(handle, **kwargs)
        except Exception:
            logger.warning("RunRecorder.close failed", exc_info=True)
