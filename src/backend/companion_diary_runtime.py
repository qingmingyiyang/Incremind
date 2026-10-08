from __future__ import annotations

import json

from backend.companion_runtime_layout import resolve_companion_runtime_layout
from core.companion_core import project_diary_food_names


def load_diary_food_names(container: object) -> dict[str, str]:
    """Load the diary food-name projection from the packaged companion catalog.

    Degrades to an empty projection when the catalog file is missing,
    unreadable, non-UTF-8, or malformed JSON; domain projection rules stay
    in `core.companion_core.diary`.
    """
    config_root = resolve_companion_runtime_layout(container).companion_config_root
    try:
        catalog = json.loads((config_root / "items.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        catalog = {}
    return project_diary_food_names(catalog)


__all__ = ["load_diary_food_names"]
