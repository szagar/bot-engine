"""Bot registry — loads bot configuration from YAML files.

Each entry in a bots YAML maps a name to a class path, cron schedule,
enabled flag, parameter overrides, and optional signal-trigger predicates.
The registry is the single source of truth for which bots exist and how
they are configured.

Example::

    registry = BotRegistry.from_file("config/bots.yaml")

    for config in registry.enabled_bots():
        print(config.name, config.schedule)

    config = registry.get("spx_ic_16d_5w")

YAML shape::

    includes:                       # optional, merged left-to-right first
      - shared/iron_condor_bots.yaml

    spx_ic_16d_5w:
      class_path: "mybots.iron_condor.IronCondorBot"
      schedule: "0 14 * * mon-fri"  # 5-field cron; "" = trigger-only
      enabled: true
      parameters:
        underlying: "SPX"
        target_dte: 45
      triggers:                     # optional subscriber-side signal selection
        on_signal:
          - signal: "orb_breakout"
            direction: "long"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TriggerSpec:
    """One ``triggers.on_signal`` subscription predicate.

    Each field is an optional equality filter; ``None`` means "match any".
    A spec matches a signal arrival when every set field equals the
    arrival's corresponding attribute. This is how a bot *selects* which
    signals fire it (subscriber-side), replacing a central route map.
    """

    signal: str | None = None  # the payload 'signal' identifier
    symbol: str | None = None  # instrument identifier
    signal_kind: str | None = None  # entry | exit | adjust | info
    direction: str | None = None  # long | short | flat

    def matches(self, arrival: dict[str, str]) -> bool:
        """True if every set filter equals the arrival's corresponding field."""
        for fld in ("signal", "symbol", "signal_kind", "direction"):
            want = getattr(self, fld)
            if want is not None and want != arrival.get(fld, ""):
                return False
        return True


@dataclass
class BotConfig:
    """Configuration for a single bot instance.

    Attributes:
        name:       Unique bot name (used for state scoping and logging).
        class_path: Dotted import path to the bot class,
                    e.g. ``"mybots.iron_condor.IronCondorBot"``.
        schedule:   5-field cron expression. Empty string means the bot is
                    trigger-only (never scheduled; fire via
                    ``BotScheduler.trigger_now`` or a signal consumer).
        enabled:    When False the bot is ignored by the scheduler.
        parameters: Parameter overrides applied on top of class defaults.
        triggers:   ``triggers.on_signal`` subscription predicates.
    """

    name: str
    class_path: str
    schedule: str
    enabled: bool
    parameters: dict[str, Any] = field(default_factory=dict)
    triggers: list[TriggerSpec] = field(default_factory=list)


class BotRegistryError(Exception):
    """Raised when the bot config file cannot be loaded or parsed."""


_INCLUDE_DEPTH_LIMIT = 5


class BotRegistry:
    """In-memory registry of bot configurations loaded from a YAML file.

    Call :meth:`from_file` to construct, then use :meth:`get`,
    :meth:`all_bots`, and :meth:`enabled_bots` to query.
    """

    def __init__(self, configs: dict[str, BotConfig]) -> None:
        self._configs = configs

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> BotRegistry:
        """Load bot configurations from a YAML file.

        Supports an optional top-level ``includes:`` list of relative paths.
        Included files are merged first (left-to-right), then the current
        file's bots override on top. Includes may be nested up to
        ``_INCLUDE_DEPTH_LIMIT`` levels.

        Raises:
            BotRegistryError: If the file is missing, unreadable, or malformed.
        """
        path = Path(path)
        data = cls._load_with_includes(path)

        configs: dict[str, BotConfig] = {}
        for name, entry in data.items():
            if not isinstance(entry, dict):
                raise BotRegistryError(f"Bot '{name}': entry must be a mapping")
            try:
                configs[name] = cls._parse_entry(name, entry)
            except (KeyError, TypeError, ValueError) as exc:
                raise BotRegistryError(f"Bot '{name}': {exc}") from exc

        return cls(configs)

    @classmethod
    def _load_with_includes(cls, path: Path, depth: int = 0) -> dict[str, Any]:
        """Load a YAML file and recursively merge any ``includes:`` fragments."""
        if depth > _INCLUDE_DEPTH_LIMIT:
            raise BotRegistryError(f"Include depth limit ({_INCLUDE_DEPTH_LIMIT}) exceeded at {path}")
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise BotRegistryError(f"Bot config file not found: {path}")
        except OSError as exc:
            raise BotRegistryError(f"Cannot read bot config file: {path}") from exc

        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise BotRegistryError(f"Invalid YAML in {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise BotRegistryError(f"Expected a mapping at top level in {path}")

        includes = data.pop("includes", [])
        if not isinstance(includes, list):
            raise BotRegistryError(f"'includes' must be a list in {path}")

        merged: dict[str, Any] = {}
        for rel in includes:
            included_path = path.parent / rel
            merged.update(cls._load_with_includes(included_path, depth + 1))
        merged.update(data)
        return merged

    @staticmethod
    def _parse_entry(name: str, entry: dict[str, Any]) -> BotConfig:
        parameters = dict(entry.get("parameters") or {})

        # underlying and watchlist are mutually exclusive
        if "underlying" in parameters and "watchlist" in parameters:
            raise ValueError(
                "'underlying' and 'watchlist' are mutually exclusive. "
                "Specify one or the other, not both."
            )

        on_signal = ((entry.get("triggers") or {}).get("on_signal")) or []
        trigger_specs = [
            TriggerSpec(
                signal=t.get("signal"),
                symbol=t.get("symbol"),
                signal_kind=t.get("signal_kind"),
                direction=t.get("direction"),
            )
            for t in on_signal
        ]

        return BotConfig(
            name=name,
            class_path=entry["class_path"],
            schedule=entry.get("schedule", ""),
            enabled=bool(entry.get("enabled", False)),
            parameters=parameters,
            triggers=trigger_specs,
        )

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def get(self, name: str) -> BotConfig | None:
        """Return the config for *name*, or None if not registered."""
        return self._configs.get(name)

    def all_bots(self) -> list[BotConfig]:
        """Return all bots (enabled and disabled), in file order."""
        return list(self._configs.values())

    def enabled_bots(self) -> list[BotConfig]:
        """Return only bots with ``enabled: true``."""
        return [c for c in self._configs.values() if c.enabled]

    def validate_roles(self) -> None:
        """Ensure every configured bot's class declares a :class:`BotRole`.

        Call once at startup. A bot that forgot ``role = BotRole.ENTRY|EXIT``
        must fail fast here — otherwise a role-aware enable gate would hit an
        unknown role and fail closed, silently disabling it (and, worse, an
        exit). Raises :class:`BotRegistryError` listing every offender.
        """
        from bot_engine.base import BotRole
        from bot_engine.loader import load_bot_class

        offenders: list[str] = []
        for config in self._configs.values():
            try:
                bot_class = load_bot_class(config.class_path)
            except (ImportError, TypeError) as exc:
                offenders.append(f"{config.name} ({config.class_path}): {exc}")
                continue
            if not isinstance(getattr(bot_class, "role", None), BotRole):
                offenders.append(f"{config.name} ({config.class_path}): role not set")

        if offenders:
            raise BotRegistryError(
                "Bots missing a valid BotRole (set `role = BotRole.ENTRY|EXIT` "
                "on the class):\n  " + "\n  ".join(offenders)
            )

    def match_triggers(self, arrival: dict[str, str]) -> list[BotConfig]:
        """Return enabled bots whose trigger specs match a signal arrival.

        Convenience for signal-consumer loops: feed each arrival through and
        fire the matching bots (e.g. via ``BotScheduler.trigger_now``).
        """
        return [
            c
            for c in self._configs.values()
            if c.enabled and any(spec.matches(arrival) for spec in c.triggers)
        ]

    def __len__(self) -> int:
        return len(self._configs)

    def __contains__(self, name: str) -> bool:
        return name in self._configs
