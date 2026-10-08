from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOUNDARY = ROOT / "src" / "core" / "ai_boundary"


def test_privacy_pipeline_is_local_and_model_independent() -> None:
    text = "\n".join(
        (BOUNDARY / name).read_text(encoding="utf-8")
        for name in ("scanner.py", "token_vault.py")
    )
    for forbidden in (
        "httpx",
        "urllib",
        "requests.",
        "LiteLLM",
        "ModelGateway",
        "backend.",
        "openai",
        "anthropic",
    ):
        assert forbidden not in text


def test_token_vault_has_no_filesystem_or_serialisation_path() -> None:
    text = (BOUNDARY / "token_vault.py").read_text(encoding="utf-8")
    for forbidden in (
        "Path(",
        "open(",
        "json.dump",
        "write_text",
        "pickle",
        "sqlite",
        "shelve",
    ):
        assert forbidden not in text
    assert "trusted_projection" in text
    assert "destination_id" in text
    assert "turn_id" in text
