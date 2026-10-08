from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "backend" / "security" / "project_boundary_profiles.py"


def test_project_boundary_profile_is_a_non_secret_local_authority() -> None:
    text = STORE.read_text(encoding="utf-8")
    for forbidden in (
        "secret_store",
        "LiteLLM",
        "httpx",
        "urllib",
        "fastapi",
        "ModelGateway",
        "TurnEventStore",
    ):
        assert forbidden not in text
    assert "atomic_write_text" in text
    assert "expected_revision" in text
    assert "_PROJECT_ID.fullmatch" in text


def test_missing_profile_default_is_guarded_and_not_implicitly_persisted() -> None:
    text = STORE.read_text(encoding="utf-8")
    assert 'mode="guarded"' in text
    assert 'remote_default="review"' in text
    default_branch = text[text.index("if not path.exists()") : text.index("profile = _decode_profile")]
    assert "atomic_write_text" not in default_branch
