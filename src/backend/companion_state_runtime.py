from __future__ import annotations

from pathlib import Path

from backend.companion_runtime_layout import resolve_companion_runtime_layout


def resolve_economy_rules_path(container: object) -> Path:
    """Locate economy-rules.json within the resolved companion config root.

    File reading and fail-safe degradation stay in the domain
    (``load_economy_rules``); this adapter only owns layout wiring.
    """
    return resolve_companion_runtime_layout(container).companion_config_root / "economy-rules.json"


__all__ = ["resolve_economy_rules_path"]
