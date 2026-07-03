"""load_bot_class — import paths and type checks."""

from __future__ import annotations

import pytest

from bot_engine import load_bot_class


def test_loads_valid_class():
    cls = load_bot_class("tests.test_registry_bots.GoodBot")
    assert cls.__name__ == "GoodBot"


def test_invalid_path_no_module():
    with pytest.raises(ImportError, match="Invalid class path"):
        load_bot_class("JustAClassName")


def test_missing_module():
    with pytest.raises(ImportError, match="Module not found"):
        load_bot_class("no.such.module.Bot")


def test_missing_class():
    with pytest.raises(ImportError, match="not found in module"):
        load_bot_class("tests.test_registry_bots.MissingBot")


def test_not_a_bot_subclass():
    with pytest.raises(TypeError, match="not a subclass"):
        load_bot_class("tests.test_registry_bots.NotABot")
