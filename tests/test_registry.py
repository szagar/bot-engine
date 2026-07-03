"""BotRegistry — YAML loading, includes, triggers, role validation."""

from __future__ import annotations

import pytest

from bot_engine import BotRegistry, BotRegistryError, TriggerSpec


def write(tmp_path, name, content):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


BASIC = """
my_bot:
  class_path: "tests.test_registry_bots.GoodBot"
  schedule: "0 14 * * mon-fri"
  enabled: true
  parameters:
    underlying: "SPX"
    target_dte: 45
"""


class TestLoading:
    def test_basic_load(self, tmp_path):
        reg = BotRegistry.from_file(write(tmp_path, "bots.yaml", BASIC))
        cfg = reg.get("my_bot")
        assert cfg is not None
        assert cfg.schedule == "0 14 * * mon-fri"
        assert cfg.enabled is True
        assert cfg.parameters["target_dte"] == 45
        assert len(reg) == 1
        assert "my_bot" in reg

    def test_missing_file(self, tmp_path):
        with pytest.raises(BotRegistryError, match="not found"):
            BotRegistry.from_file(tmp_path / "nope.yaml")

    def test_enabled_bots_filter(self, tmp_path):
        content = BASIC + """
off_bot:
  class_path: "x.Y"
  enabled: false
"""
        reg = BotRegistry.from_file(write(tmp_path, "bots.yaml", content))
        assert [c.name for c in reg.enabled_bots()] == ["my_bot"]
        assert len(reg.all_bots()) == 2

    def test_default_schedule_is_trigger_only(self, tmp_path):
        reg = BotRegistry.from_file(
            write(tmp_path, "b.yaml", 'x:\n  class_path: "m.C"\n  enabled: true\n')
        )
        assert reg.get("x").schedule == ""

    def test_underlying_watchlist_mutual_exclusion(self, tmp_path):
        content = """
bad:
  class_path: "m.C"
  parameters:
    underlying: "SPX"
    watchlist: "earnings"
"""
        with pytest.raises(BotRegistryError, match="mutually exclusive"):
            BotRegistry.from_file(write(tmp_path, "b.yaml", content))


class TestIncludes:
    def test_include_merge_and_override(self, tmp_path):
        write(tmp_path, "shared/frag.yaml", BASIC)
        main = """
includes:
  - shared/frag.yaml

my_bot:
  class_path: "tests.test_registry_bots.GoodBot"
  schedule: "30 9 * * mon-fri"
  enabled: false
"""
        reg = BotRegistry.from_file(write(tmp_path, "bots.yaml", main))
        # main file overrides the include wholesale
        assert reg.get("my_bot").schedule == "30 9 * * mon-fri"
        assert reg.get("my_bot").enabled is False

    def test_include_depth_limit(self, tmp_path):
        for i in range(8):
            write(tmp_path, f"f{i}.yaml", f"includes:\n  - f{i + 1}.yaml\n")
        write(tmp_path, "f8.yaml", "x:\n  class_path: 'm.C'\n")
        with pytest.raises(BotRegistryError, match="depth limit"):
            BotRegistry.from_file(tmp_path / "f0.yaml")


class TestTriggers:
    def test_trigger_parse_and_match(self, tmp_path):
        content = """
sig_bot:
  class_path: "m.C"
  enabled: true
  triggers:
    on_signal:
      - signal: "orb_breakout"
        direction: "long"
"""
        reg = BotRegistry.from_file(write(tmp_path, "b.yaml", content))
        cfg = reg.get("sig_bot")
        assert cfg.triggers == [TriggerSpec(signal="orb_breakout", direction="long")]

        arrival = {"signal": "orb_breakout", "direction": "long", "symbol": "index:SPX"}
        assert cfg.triggers[0].matches(arrival)
        assert not cfg.triggers[0].matches({"signal": "orb_breakout", "direction": "short"})

        assert [c.name for c in reg.match_triggers(arrival)] == ["sig_bot"]
        assert reg.match_triggers({"signal": "other"}) == []

    def test_none_fields_match_any(self):
        spec = TriggerSpec(signal="x")
        assert spec.matches({"signal": "x", "direction": "short", "symbol": "whatever"})


class TestValidateRoles:
    def test_good_role_passes(self, tmp_path):
        reg = BotRegistry.from_file(
            write(
                tmp_path,
                "b.yaml",
                'ok:\n  class_path: "tests.test_registry_bots.GoodBot"\n  enabled: true\n',
            )
        )
        reg.validate_roles()  # no raise

    def test_missing_role_fails(self, tmp_path):
        reg = BotRegistry.from_file(
            write(
                tmp_path,
                "b.yaml",
                'bad:\n  class_path: "tests.test_registry_bots.NoRoleBot"\n  enabled: true\n',
            )
        )
        with pytest.raises(BotRegistryError, match="role not set"):
            reg.validate_roles()

    def test_unimportable_class_fails(self, tmp_path):
        reg = BotRegistry.from_file(
            write(tmp_path, "b.yaml", 'bad:\n  class_path: "nope.Missing"\n  enabled: true\n')
        )
        with pytest.raises(BotRegistryError):
            reg.validate_roles()
