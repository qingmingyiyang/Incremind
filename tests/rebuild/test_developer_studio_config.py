from __future__ import annotations

from pathlib import Path

from core.product_core import (
    GetDeveloperStudioConfig,
    SaveDeveloperStudioConfig,
    serialize_developer_studio_config,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_developer_studio_config_defaults_and_persists_without_secret_material(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    default_payload = serialize_developer_studio_config(GetDeveloperStudioConfig(object_store).execute())

    assert default_payload["status"] == "default_empty"
    assert default_payload["revision"] == 0
    assert default_payload["model_profiles"] == []

    saved = SaveDeveloperStudioConfig(object_store).execute(
        model_profiles=[
            {
                "id": "mp-intake-main",
                "name": "主识别模型",
                "providerId": "intake-main-model",
                "modelId": "deepseek-chat",
            }
        ],
        prompts=[
            {
                "id": "pt-input-understanding",
                "stageId": "input-understanding",
                "content": "输出 JSON：{ input_type, intent, confidence, child_inputs, route }",
            }
        ],
        skills=[{"id": "skill-video-memory", "enabled": True}],
        workflow_steps=[{"id": "wf-classify", "enabled": True}],
        snapshots=[],
        expected_revision=0,
    )
    payload = serialize_developer_studio_config(saved)

    assert payload["status"] == "ready"
    assert payload["revision"] == 1
    assert payload["model_profiles"][0]["providerId"] == "intake-main-model"
    assert payload["task_model_map"] == {}
    assert "api_key" not in str(payload).lower()
    assert "cookie:" not in str(payload).lower()


def test_developer_studio_config_rejects_sensitive_fields_and_revision_conflicts(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    writer = SaveDeveloperStudioConfig(object_store)

    writer.execute(
        model_profiles=[],
        task_model_map={},
        prompts=[],
        skills=[],
        workflow_steps=[],
        expected_revision=0,
    )

    try:
        writer.execute(
            model_profiles=[{"id": "mp-default", "api_key": "local-secret"}],
            task_model_map={},
            prompts=[],
            skills=[],
            workflow_steps=[],
            expected_revision=1,
        )
    except ValueError as error:
        assert str(error) == "sensitive field is not allowed: api_key"
    else:
        raise AssertionError("expected sensitive field rejection")

    try:
        writer.execute(
            model_profiles=[],
            task_model_map={},
            prompts=[],
            skills=[],
            workflow_steps=[],
            expected_revision=0,
        )
    except ValueError as error:
        assert str(error) == "developer studio config revision conflict"
    else:
        raise AssertionError("expected revision conflict")


def test_developer_studio_config_preserves_legacy_task_model_map_as_read_only_backup(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    object_store.write(
        "developer_studio_configs",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "revision": 4,
            "model_profiles": [],
            "task_model_map": {"intakeMain": "mp-legacy"},
            "prompts": [],
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
            "updated_at": "2026-07-01T00:00:00+08:00",
        },
        expected_revision=None,
    )
    writer = SaveDeveloperStudioConfig(object_store)

    saved = writer.execute(
        model_profiles=[],
        prompts=[{"id": "pt-input-understanding", "content": "updated"}],
        skills=[],
        workflow_steps=[],
        expected_revision=4,
    )

    assert saved.revision == 5
    assert saved.task_model_map == {"intakeMain": "mp-legacy"}

    try:
        writer.execute(
            model_profiles=[],
            task_model_map={"intakeMain": "mp-replacement"},
            prompts=[],
            skills=[],
            workflow_steps=[],
            expected_revision=5,
        )
    except ValueError as error:
        assert str(error) == "legacy task_model_map is read-only"
    else:
        raise AssertionError("expected legacy task_model_map mutation rejection")
