from __future__ import annotations

from pathlib import Path
import re

from backend.security.provider_egress import DEFAULT_PROVIDER_EGRESS_PURPOSES


ROOT = Path(__file__).resolve().parents[2]


def test_ai_runtime_literal_egress_purposes_are_manifested() -> None:
    runtime = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(encoding="utf-8")
    purposes = set(re.findall(r'egress_purpose="([a-z0-9_]+)"', runtime))
    assert purposes
    assert purposes <= set(DEFAULT_PROVIDER_EGRESS_PURPOSES)


def test_provider_boundary_adapter_has_no_secret_or_network_authority() -> None:
    adapter = (
        ROOT / "src" / "backend" / "security" / "provider_egress_boundary_adapter.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "secret_store",
        "api_key",
        "LiteLLM",
        "httpx",
        "urllib",
        "complete_text",
        "requests.",
    ):
        assert forbidden not in adapter
