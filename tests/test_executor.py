"""BotExecutor — lifecycle, gate, recorder, skip/error semantics."""

from __future__ import annotations

from typing import Any

import pytest

from bot_engine import BotConfig, BotExecutor, BotRole


def cfg(class_name: str, **params: Any) -> BotConfig:
    return BotConfig(
        name="test_bot",
        class_path=f"tests.test_registry_bots.{class_name}",
        schedule="",
        enabled=True,
        parameters=params,
    )


class RecordingRecorder:
    def __init__(self) -> None:
        self.opened: list[dict] = []
        self.closed: list[dict] = []

    async def open(self, **kwargs):
        self.opened.append(kwargs)
        return len(self.opened)  # handle

    async def close(self, handle, **kwargs):
        self.closed.append({"handle": handle, **kwargs})


class DenyEntriesGate:
    async def resolve(self, *, account, bot_name, role):
        if role is BotRole.ENTRY:
            return False, "flag=0"
        return True, "flag=1"


class TestLifecycle:
    async def test_success(self, ctx):
        recorder = RecordingRecorder()
        executor = BotExecutor(ctx, account="acct1", recorder=recorder)
        result = await executor.run(cfg("GoodBot", underlying="RUT"))

        assert result.action == "submitted_entry"
        assert result.order_id == "ord-1"
        assert recorder.opened[0]["bot"] == "test_bot"
        assert recorder.opened[0]["account"] == "acct1"
        assert recorder.opened[0]["underlying"] == "RUT"
        assert recorder.opened[0]["run_id"].startswith("run-test_bot-RUT-")
        assert recorder.closed[0]["result"] == "submitted_entry"
        assert recorder.closed[0]["order_id"] == "ord-1"
        assert recorder.closed[0]["data"] == {"credit": "1.20"}

    async def test_skip_maps_to_skipped_result(self, ctx):
        recorder = RecordingRecorder()
        executor = BotExecutor(ctx, recorder=recorder)
        result = await executor.run(cfg("SkippingBot"))

        assert result.action == "skipped"
        assert result.data["reason"] == "iv too low"
        assert recorder.closed[0]["result"] == "skipped"
        assert recorder.closed[0]["skip_reason"] == "iv too low"

    async def test_error_propagates_after_recording(self, ctx):
        recorder = RecordingRecorder()
        executor = BotExecutor(ctx, recorder=recorder)
        with pytest.raises(RuntimeError, match="boom"):
            await executor.run(cfg("ExplodingBot"))

        assert recorder.closed[0]["result"] == "error"
        assert "RuntimeError: boom" in recorder.closed[0]["error"]

    async def test_no_underlying_slug_is_any(self, ctx):
        recorder = RecordingRecorder()
        executor = BotExecutor(ctx, recorder=recorder)
        await executor.run(cfg("GoodBot"))
        assert recorder.opened[0]["run_id"].startswith("run-test_bot-ANY-")
        assert recorder.opened[0]["underlying"] is None


class TestGate:
    async def test_disabled_entry_skips(self, ctx):
        executor = BotExecutor(ctx, gate=DenyEntriesGate())
        result = await executor.run(cfg("GoodBot"))
        assert result.action == "skipped"
        assert "bot disabled (flag=0)" in result.data["reason"]

    async def test_exit_role_allowed_by_gate(self, ctx):
        executor = BotExecutor(ctx, gate=DenyEntriesGate())
        # ExplodingBot is an EXIT — gate lets it through, then it raises.
        with pytest.raises(RuntimeError):
            await executor.run(cfg("ExplodingBot"))

    async def test_default_gate_always_enabled(self, ctx):
        executor = BotExecutor(ctx)
        result = await executor.run(cfg("GoodBot"))
        assert result.action == "submitted_entry"


class TestRecorderRobustness:
    async def test_recorder_failure_never_blocks_run(self, ctx):
        class BrokenRecorder:
            async def open(self, **kwargs):
                raise OSError("db down")

            async def close(self, handle, **kwargs):
                raise OSError("db down")

        executor = BotExecutor(ctx, recorder=BrokenRecorder())
        result = await executor.run(cfg("GoodBot"))
        assert result.action == "submitted_entry"


class TestHooks:
    async def test_on_run_context_hook(self, ctx):
        seen: list[dict] = []
        executor = BotExecutor(ctx, account="a1", on_run_context=seen.append)
        await executor.run(cfg("GoodBot"))
        assert seen[0]["bot"] == "test_bot"
        assert seen[0]["account"] == "a1"
        assert seen[0]["run_id"].startswith("run-")

    async def test_custom_run_id_minter(self, ctx):
        recorder = RecordingRecorder()
        executor = BotExecutor(
            ctx, recorder=recorder, run_id_minter=lambda bot, und: f"sig-{bot}-fixed"
        )
        await executor.run(cfg("GoodBot"))
        assert recorder.opened[0]["run_id"] == "sig-test_bot-fixed"
