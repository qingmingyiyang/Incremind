from __future__ import annotations

from pathlib import Path

from backend.api.expert_turn_e2e_fixture import (
    _DESKTOP_NONCE_ENV,
    _EXPERT_TOKEN_ENV,
    _FIXTURE_NONCE_ENV,
    _PROVIDER_ORIGIN_ENV,
    _RUN_TOKEN_ENV,
    install_expert_turn_e2e_fixture,
)
from backend.api.expert_turn_binding_runtime import build_expert_turn_binding_runtime
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.providers import ProviderRegistry
from core.product_core.expert_catalog import ExpertCatalog, ExpertProjectBindingStore


def test_fixture_is_unreachable_without_both_inherited_gates(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv(_RUN_TOKEN_ENV, raising=False)
    monkeypatch.delenv(_EXPERT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(_FIXTURE_NONCE_ENV, raising=False)
    monkeypatch.delenv(_DESKTOP_NONCE_ENV, raising=False)
    monkeypatch.delenv(_PROVIDER_ORIGIN_ENV, raising=False)

    assert install_expert_turn_e2e_fixture(tmp_path) is False
    assert ExpertCatalog(tmp_path).get("workbench-question-expert") is None


def test_fixture_seeds_existing_authorities_idempotently_after_dual_gate(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv(_RUN_TOKEN_ENV, "run-token")
    monkeypatch.setenv(_EXPERT_TOKEN_ENV, "run-token")
    monkeypatch.setenv(_FIXTURE_NONCE_ENV, "desktop-nonce")
    monkeypatch.setenv(_DESKTOP_NONCE_ENV, "desktop-nonce")
    monkeypatch.setenv(_PROVIDER_ORIGIN_ENV, "http://127.0.0.1:43127/v1")

    assert install_expert_turn_e2e_fixture(tmp_path) is True
    assert install_expert_turn_e2e_fixture(tmp_path) is True

    expert = ExpertCatalog(tmp_path).get("workbench-question-expert")
    binding = ExpertProjectBindingStore(tmp_path).get("default", "workbench-question-expert")
    assert expert is not None and expert["status"] == "active"
    assert binding is not None and binding["default"] is True
    assert expert["tools"] == ["workbench.question.answer"]
    assert "intent_affinity" not in binding
    assert binding["selection_mode"] == "manual"

    selection = build_expert_turn_binding_runtime(tmp_path).select(
        {
            "scope": {"kind": "project", "project_id": "default", "series_id": None},
            "desired_outcome": "workbench.question.answer",
        },
        capability_manifest=None, capabilities=(),
    )
    assert selection["selection_mode"] == "project_default"
    assert selection["selected"]["expert_id"] == "workbench-question-expert"

    store, _settings = build_rebuild_object_store(tmp_path)
    assert store.read("memory_atoms", "expert-turn-e2e-atom") is not None
    assert ProviderRegistry(tmp_path).get_readonly(
        "expert-turn-e2e-local", fallback={},
    )["base_url"] == "http://127.0.0.1:43127/v1"


def test_fixture_rejects_non_loopback_or_malformed_provider_origin(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv(_RUN_TOKEN_ENV, "run-token")
    monkeypatch.setenv(_EXPERT_TOKEN_ENV, "run-token")
    monkeypatch.setenv(_FIXTURE_NONCE_ENV, "desktop-nonce")
    monkeypatch.setenv(_DESKTOP_NONCE_ENV, "desktop-nonce")
    for origin in (
        "https://127.0.0.1:43127/v1",
        "http://localhost:43127/v1",
        "http://127.0.0.1:43127/not-v1",
        "http://127.0.0.1:43127/v1?override=true",
    ):
        monkeypatch.setenv(_PROVIDER_ORIGIN_ENV, origin)
        assert install_expert_turn_e2e_fixture(tmp_path) is False
    assert ExpertCatalog(tmp_path).get("workbench-question-expert") is None
