from __future__ import annotations

import json

import pytest

from core.product_core.processing_recipe import (
    ProcessingRecipeConflict,
    ProcessingRecipeError,
    ProcessingRecipeRegistry,
    ProcessingRecipeRuntime,
)
from core.storage_provider import JsonObjectStore


class _RacingStore:
    def __init__(self, inner: JsonObjectStore) -> None:
        self.inner = inner
        self.race_next_conditional_write = False

    def read(self, collection: str, object_id: str):
        return self.inner.read(collection, object_id)

    def read_including_deleted(self, collection: str, object_id: str):
        return self.inner.read_including_deleted(collection, object_id)

    def list(self, collection: str):
        return self.inner.list(collection)

    def delete(self, collection: str, object_id: str) -> bool:
        return self.inner.delete(collection, object_id)

    def revision(self, collection: str, object_id: str) -> int:
        return self.inner.revision(collection, object_id)

    def write(self, collection: str, object_id: str, payload, expected_revision: int | None) -> int:
        if expected_revision is not None and self.race_next_conditional_write:
            self.race_next_conditional_write = False
            concurrent = dict(self.inner.read(collection, object_id) or {})
            concurrent["concurrent_marker"] = "other writer"
            self.inner.write(collection, object_id, concurrent, expected_revision=None)
        return self.inner.write(collection, object_id, payload, expected_revision=expected_revision)


def _draft(recipe_id: str = "recipe-empty-guard", *, priority: int = 100, trigger_mode: str = "any") -> dict[str, object]:
    return {
        "id": recipe_id,
        "name": "空输入确定性防护",
        "description": "仅声明无副作用的空输入匹配合同。",
        "content_matcher": {"content_types": ["text"], "min_length": 0, "max_length": 20},
        "trigger_matcher": {"mode": trigger_mode, "values": [] if trigger_mode == "any" else ["空"]},
        "prompt_ref": {
            "prompt_id": "pt-empty-handle",
            "source": "active",
            "unit_id": "intake.classification",
            "unit_revision": 0,
        },
        "model_route_key": "intake.classification",
        "model_route_revision": 1,
        "output_schema": {
            "type": "object",
            "required": ["empty"],
            "properties": {"empty": {"type": "boolean"}},
        },
        "executor_id": "empty_guard.deterministic",
        "side_effect_class": "none",
        "priority": priority,
        "fallback": {"mode": "continue_default", "reason": "保持现有确定性输入流程"},
    }


def _registry(store, *, now: str | None = None) -> ProcessingRecipeRegistry:
    return ProcessingRecipeRegistry(store, now=now, authority_validator=lambda _recipe: None)


def _save(registry: ProcessingRecipeRegistry, draft: dict[str, object], expected: int = 0) -> dict[str, object]:
    preview = registry.preview_draft(draft)
    return registry.save_draft(
        draft,
        expected_registry_revision=expected,
        validation_token=str(preview["validation_token"]),
    )


def _activate(registry: ProcessingRecipeRegistry, recipe_id: str, expected_registry: int, recipe_revision: int) -> dict[str, object]:
    preview = registry.preview_activation(
        recipe_id,
        expected_registry_revision=expected_registry,
        expected_recipe_revision=recipe_revision,
    )
    return registry.activate(
        recipe_id,
        expected_registry_revision=expected_registry,
        expected_recipe_revision=recipe_revision,
        activation_token=str(preview["activation_token"]),
        confirm=True,
        reason="验证无副作用处理规则合同",
    )


def test_draft_isolated_activation_restart_resolution_and_rollback(tmp_path) -> None:
    store = JsonObjectStore(tmp_path)
    registry = _registry(store, now="2026-07-17T08:00:00+00:00")

    assert registry.status()["production_feature_enabled"] is True
    assert registry.resolve(content_type="text", text="", feature_enabled=True)["status"] == "no_match"

    saved = _save(registry, _draft())
    assert saved["registry_revision"] == 1
    assert saved["active"] == []
    assert registry.resolve(content_type="text", text="", feature_enabled=True)["status"] == "no_match"

    activated = _activate(registry, "recipe-empty-guard", 1, 1)
    assert activated["status"] == "activated"
    assert activated["registry_revision"] == 2
    assert activated["production_feature_enabled"] is True

    restarted = _registry(JsonObjectStore(tmp_path), now="2026-07-17T08:01:00+00:00")
    feature_off = restarted.resolve(content_type="text", text="", feature_enabled=False)
    assert feature_off["status"] == "feature_off"
    assert feature_off["selected"] is None
    resolved = restarted.resolve(content_type="text", text="", feature_enabled=True)
    assert resolved["status"] == "matched"
    assert resolved["selected"]["id"] == "recipe-empty-guard"
    assert resolved["evaluations"][0]["evidence"] == {"content_type": True, "length": True, "trigger": True}

    rolled_back = restarted.rollback(
        "recipe-empty-guard",
        expected_registry_revision=2,
        confirm=True,
        reason="恢复激活前快照",
    )
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["active"] == []
    assert rolled_back["drafts"][0]["revision"] == 1


def test_priority_disabled_no_match_and_stable_tie_break(tmp_path) -> None:
    registry = _registry(JsonObjectStore(tmp_path), now="2026-07-17T08:00:00+00:00")
    _save(registry, _draft("recipe-z", priority=10))
    _save(registry, _draft("recipe-a", priority=10), expected=1)
    _activate(registry, "recipe-z", 2, 1)
    _activate(registry, "recipe-a", 3, 1)

    resolved = registry.resolve(content_type="text", text="hello", feature_enabled=True)
    assert resolved["selected"]["id"] == "recipe-a"

    deactivated = registry.deactivate(
        "recipe-a", expected_registry_revision=4, confirm=True, reason="验证禁用边界"
    )
    assert deactivated["status"] == "deactivated"
    assert registry.resolve(content_type="text", text="hello", feature_enabled=True)["selected"]["id"] == "recipe-z"
    assert registry.resolve(content_type="video", text="hello", feature_enabled=True)["status"] == "no_match"


def test_validation_token_cas_replay_and_physical_drift_fail_closed(tmp_path) -> None:
    store = JsonObjectStore(tmp_path)
    registry = _registry(store)
    draft = _draft()
    preview = registry.preview_draft(draft)
    _save(registry, draft)

    with pytest.raises(ProcessingRecipeConflict, match="revision conflict"):
        registry.save_draft(draft, expected_registry_revision=0, validation_token=str(preview["validation_token"]))

    activation = registry.preview_activation(
        "recipe-empty-guard", expected_registry_revision=1, expected_recipe_revision=1
    )
    changed = _draft()
    changed["description"] = "新的草稿"
    _save(registry, changed, expected=1)
    with pytest.raises(ProcessingRecipeConflict, match="revision conflict|drifted"):
        registry.activate(
            "recipe-empty-guard",
            expected_registry_revision=1,
            expected_recipe_revision=1,
            activation_token=str(activation["activation_token"]),
            confirm=True,
            reason="陈旧确认不得生效",
        )


def test_physical_store_revision_race_fails_closed(tmp_path) -> None:
    store = _RacingStore(JsonObjectStore(tmp_path))
    registry = _registry(store)
    draft = _draft()
    preview = registry.preview_draft(draft)
    store.race_next_conditional_write = True
    with pytest.raises(ProcessingRecipeConflict, match="storage revision conflict"):
        registry.save_draft(
            draft,
            expected_registry_revision=0,
            validation_token=str(preview["validation_token"]),
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"executor_id": "python.eval"}, "executor"),
        ({"side_effect_class": "network"}, "side effect"),
        ({"output_schema": {"type": "array", "required": [], "properties": {}}}, "output schema"),
        ({"metadata": {"api_key": "secret"}}, "sensitive material"),
    ],
)
def test_invalid_executor_side_effect_schema_and_secret_are_rejected(tmp_path, change, message) -> None:
    draft = {**_draft(), **change}
    with pytest.raises(ProcessingRecipeError, match=message):
        _registry(JsonObjectStore(tmp_path)).preview_draft(draft)


def test_legacy_preview_is_zero_write_and_never_maps_enabled_to_active(tmp_path) -> None:
    store = JsonObjectStore(tmp_path)
    registry = _registry(store, now="2026-07-17T08:00:00+00:00")
    preview = registry.legacy_preview([
        {"id": "sk-empty-guard", "enabled": True, "promptTemplateId": "pt-empty-handle", "modelProfileId": "mp-default"},
        {"id": "sk-link-capture", "enabled": True, "promptTemplateId": "pt-card-summary", "modelProfileId": "mp-memory"},
    ])

    assert preview["write_effect"] == "none"
    assert preview["activation_effect"] == "none"
    assert preview["candidates"][0]["legacy_enabled_is_production_active"] is False
    assert preview["candidates"][0]["status"] == "mappable_draft"
    assert preview["candidates"][1]["status"] == "unbound_legacy_draft"
    assert registry.status()["registry_revision"] == 0
    assert not (tmp_path / "processing_recipe_registries").exists()


def test_current_eight_legacy_skill_ids_remain_drafts_and_only_empty_guard_has_an_executor_mapping(tmp_path) -> None:
    ids = [
        "sk-link-capture", "sk-video-memory", "sk-doc-organize", "sk-note-refine",
        "sk-inspiration", "sk-qa-recall", "sk-series-link", "sk-empty-guard",
    ]
    skills = [
        {
            "id": skill_id,
            "enabled": True,
            "promptTemplateId": "pt-empty-handle" if skill_id == "sk-empty-guard" else "pt-summary",
            "modelProfileId": "mp-default",
        }
        for skill_id in ids
    ]
    preview = _registry(JsonObjectStore(tmp_path)).legacy_preview(skills)

    assert len(preview["candidates"]) == 8
    assert [item["legacy_id"] for item in preview["candidates"] if item["status"] == "mappable_draft"] == ["sk-empty-guard"]
    assert all(item["legacy_enabled_is_production_active"] is False for item in preview["candidates"])
    assert _registry(JsonObjectStore(tmp_path)).status()["active"] == []


def test_tampered_registry_unknown_field_history_or_feature_flag_fails_closed(tmp_path) -> None:
    store = JsonObjectStore(tmp_path)
    registry = _registry(store)
    _save(registry, _draft())
    path = next(tmp_path.rglob("recipe-empty-guard.json"), None)
    assert path is None

    record_path = next(tmp_path.rglob("default.json"))
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["production_feature_enabled"] = True
    record_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProcessingRecipeError, match="registry schema"):
        _registry(JsonObjectStore(tmp_path)).status()


def test_tampered_active_snapshot_that_no_longer_matches_history_fails_closed(tmp_path) -> None:
    registry = _registry(JsonObjectStore(tmp_path))
    _save(registry, _draft())
    _activate(registry, "recipe-empty-guard", 1, 1)
    record_path = next(tmp_path.rglob("default.json"))
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["active"][0]["priority"] = 999
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProcessingRecipeError, match="active authority drifted"):
        _registry(JsonObjectStore(tmp_path)).status()


def test_resolution_revalidates_active_prompt_and_model_route_authorities(tmp_path) -> None:
    drifted = False

    def validate(_recipe) -> None:
        if drifted:
            raise ProcessingRecipeError("processing recipe model route revision drifted")

    registry = ProcessingRecipeRegistry(JsonObjectStore(tmp_path), authority_validator=validate)
    _save(registry, _draft())
    _activate(registry, "recipe-empty-guard", 1, 1)
    assert registry.resolve(content_type="text", text="", feature_enabled=True)["status"] == "matched"

    drifted = True
    with pytest.raises(ProcessingRecipeError, match="route revision drifted"):
        registry.resolve(content_type="text", text="", feature_enabled=True)


def test_exact_draft_test_evaluation_and_output_schema_are_zero_write(tmp_path) -> None:
    registry = _registry(JsonObjectStore(tmp_path))
    _save(registry, _draft())
    before = registry.status()

    evaluation = registry.evaluate_for_test(
        "recipe-empty-guard",
        source="draft",
        expected_registry_revision=1,
        expected_recipe_revision=1,
        content_type="text",
        text="",
    )
    assert evaluation["status"] == "matched"
    assert evaluation["executor_executed"] is False
    assert evaluation["write_effect"] == "none"
    assert registry.validate_test_output(evaluation["recipe"], {"empty": True}) == {"valid": True, "errors": []}
    invalid = registry.validate_test_output(evaluation["recipe"], {"empty": "yes", "extra": 1})
    assert invalid["valid"] is False
    assert registry.status() == before


def test_exact_recipe_test_rejects_source_and_revision_drift(tmp_path) -> None:
    registry = _registry(JsonObjectStore(tmp_path))
    _save(registry, _draft())
    with pytest.raises(ProcessingRecipeError, match="source"):
        registry.evaluate_for_test(
            "recipe-empty-guard", source="legacy", expected_registry_revision=1,
            expected_recipe_revision=1, content_type="text", text="",
        )
    with pytest.raises(ProcessingRecipeConflict, match="revision conflict"):
        registry.evaluate_for_test(
            "recipe-empty-guard", source="draft", expected_registry_revision=1,
            expected_recipe_revision=9, content_type="text", text="",
        )


def test_runtime_inactive_match_and_restart_are_zero_write(tmp_path) -> None:
    store = JsonObjectStore(tmp_path)
    registry = _registry(store)
    runtime = ProcessingRecipeRuntime(registry)

    inactive = runtime.preflight(
        content_type="text", text="", trigger="workbench.input-classifier"
    )
    assert inactive == {
        "status": "no_match",
        "registry_revision": 0,
        "production_feature_enabled": True,
        "consumer": "workbench.input-classifier",
        "action": "continue_default",
        "selected": None,
        "evaluations": [],
        "executor_executed": False,
        "side_effects": "none",
        "output_schema": None,
    }

    _save(registry, _draft())
    _activate(registry, "recipe-empty-guard", 1, 1)
    before_revision = store.revision("processing_recipe_registries", "default")

    empty = runtime.preflight(
        content_type="text", text="  ", trigger="workbench.input-classifier"
    )
    assert empty["status"] == "executed"
    assert empty["action"] == "reject_empty"
    assert empty["executor_executed"] is True
    assert empty["output_schema"] == {"valid": True, "errors": []}
    assert empty["selected"] == {
        "recipe_id": "recipe-empty-guard",
        "recipe_revision": 1,
        "executor_id": "empty_guard.deterministic",
        "side_effect_class": "none",
        "prompt_id": "pt-empty-handle",
        "prompt_unit_revision": 0,
        "model_route_key": "intake.classification",
        "model_route_revision": 1,
    }
    assert "text" not in empty

    restarted = ProcessingRecipeRuntime(_registry(JsonObjectStore(tmp_path)))
    nonempty = restarted.preflight(
        content_type="text", text="hello", trigger="workbench.auto-intake"
    )
    assert nonempty["status"] == "executed"
    assert nonempty["action"] == "continue_default"
    assert nonempty["consumer"] == "workbench.auto-intake"
    assert store.revision("processing_recipe_registries", "default") == before_revision


def test_runtime_no_match_and_executor_failure_continue_default(tmp_path) -> None:
    registry = _registry(JsonObjectStore(tmp_path))
    _save(registry, _draft())
    _activate(registry, "recipe-empty-guard", 1, 1)

    no_match = ProcessingRecipeRuntime(registry).preflight(
        content_type="video", text="", trigger="workbench.auto-intake"
    )
    assert no_match["status"] == "no_match"
    assert no_match["action"] == "continue_default"
    assert no_match["executor_executed"] is False

    def fail(_text: str):
        raise RuntimeError("private executor detail")

    failed = ProcessingRecipeRuntime(
        registry, executors={"empty_guard.deterministic": fail}
    ).preflight(content_type="text", text="", trigger="workbench.auto-intake")
    assert failed["status"] == "executor_failed"
    assert failed["action"] == "continue_default"
    assert failed["fallback"] == "continue_default"
    assert "private executor detail" not in str(failed)


def test_runtime_schema_and_authority_drift_fail_closed(tmp_path) -> None:
    drifted = False

    def validate(_recipe) -> None:
        if drifted:
            raise ProcessingRecipeError("processing recipe model route revision drifted")

    registry = ProcessingRecipeRegistry(
        JsonObjectStore(tmp_path), authority_validator=validate
    )
    _save(registry, _draft())
    _activate(registry, "recipe-empty-guard", 1, 1)

    invalid_runtime = ProcessingRecipeRuntime(
        registry,
        executors={"empty_guard.deterministic": lambda _text: {"empty": "yes"}},
    )
    with pytest.raises(ProcessingRecipeError, match="output schema"):
        invalid_runtime.preflight(
            content_type="text", text="", trigger="workbench.input-classifier"
        )

    drifted = True
    with pytest.raises(ProcessingRecipeError, match="route revision drifted"):
        ProcessingRecipeRuntime(registry).preflight(
            content_type="text", text="", trigger="workbench.input-classifier"
        )
