"""Bot class loading utilities.

Dynamically import bot classes by dotted module path. The bot class must be
a concrete subclass of :class:`bot_engine.base.BaseBot`.

Example::

    bot_class = load_bot_class("mybots.iron_condor.IronCondorBot")
    bot = bot_class(name="weekly_ic", parameters={...}, context=ctx)
    result = await bot.execute()
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_engine.base import BaseBot


def load_bot_class(class_path: str) -> type[BaseBot]:
    """Import and return a bot class from its dotted module path.

    Args:
        class_path: Full dotted path to the class,
                    e.g. ``"mybots.iron_condor.IronCondorBot"``.

    Returns:
        The bot class (not instantiated).

    Raises:
        ImportError: If the module cannot be imported or the class is not found.
        TypeError:   If the class does not inherit from BaseBot.
    """
    from bot_engine.base import BaseBot

    module_path, _, class_name = class_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid class path '{class_path}': must be 'module.ClassName'")

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ImportError(f"Module not found: '{module_path}'") from exc

    bot_class = getattr(module, class_name, None)
    if bot_class is None:
        raise ImportError(f"Class '{class_name}' not found in module '{module_path}'")

    if not (isinstance(bot_class, type) and issubclass(bot_class, BaseBot)):
        raise TypeError(f"'{class_path}' is not a subclass of BaseBot")

    return bot_class


def reload_bot_class(class_path: str) -> type[BaseBot]:
    """Reload the module and return a fresh bot class.

    Useful during development for hot-reloading without restarting the
    engine. Not safe for production use while bots are actively executing.
    """
    module_path, _, _ = class_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid class path '{class_path}'")

    try:
        module = importlib.import_module(module_path)
        importlib.reload(module)
    except ModuleNotFoundError as exc:
        raise ImportError(f"Module not found: '{module_path}'") from exc

    return load_bot_class(class_path)
