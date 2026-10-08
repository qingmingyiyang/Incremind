from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.routes.product.developer_prompt_catalog import (
    _developer_studio_draft_prompt,
    _developer_studio_prompt,
    _developer_studio_prompts,
)
from core.product_core import (
    DeveloperStudioConfigError,
    GetDeveloperStudioConfig,
    PromptActivationConflict,
    PromptActivationError,
    PromptActivationService,
    SaveDeveloperStudioConfig,
    resolve_active_prompt,
    serialize_prompt_activation,
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


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _prompt(prompt_id: str, content: str, version: int = 1) -> dict[str, object]:
    return {
        "id": prompt_id,
        "stageId": prompt_id.removeprefix("pt-"),
        "name": prompt_id,
        "content": content,
        "variables": [],
        "outputSchema": "{}",
        "modelProfileId": "mp-test",
        "version": version,
        "isProtected": False,
        "updatedAt": f"2026-07-17T00:00:0{version}+08:00",
    }


def _template_prompts(prefix: str, version: int = 1) -> list[dict[str, object]]:
    return [
        _prompt("pt-title", f"{prefix} title", version),
        _prompt("pt-detail-summary", f"{prefix} detail", version),
        _prompt("pt-longterm-organize", f"{prefix} organize", version),
        _prompt("pt-output-validate", f"{prefix} validate", version),
    ]


def _seed_legacy(store: JsonObjectStore) -> None:
    store.write(
        "developer_studio_configs",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "revision": 4,
            "model_profiles": [],
            "task_model_map": {},
            "prompts": [_prompt("pt-input-understanding", "active legacy", 1), *_template_prompts("active", 1)],
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
            "updated_at": "2026-07-17T00:00:01+08:00",
        },
        expected_revision=None,
    )


def _save(store: JsonObjectStore, *, revision: int, prompts: list[dict[str, object]]):
    return SaveDeveloperStudioConfig(store, now="2026-07-17T00:00:02+08:00").execute(
        model_profiles=[],
        prompts=prompts,
        skills=[],
        workflow_steps=[],
        snapshots=[],
        expected_revision=revision,
    )


def test_first_draft_save_freezes_legacy_prompts_as_active_baseline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    draft_prompts = [_prompt("pt-input-understanding", "draft changed", 2), *_template_prompts("draft", 2)]

    saved = _save(store, revision=4, prompts=draft_prompts)
    reloaded = GetDeveloperStudioConfig(store).execute()

    assert saved.revision == 5
    assert saved.prompts[0]["content"] == "draft changed"
    assert resolve_active_prompt(saved, "pt-input-understanding")["content"] == "active legacy"
    assert resolve_active_prompt(reloaded, "pt-title")["content"] == "active title"
    status = serialize_prompt_activation(reloaded)
    assert status["activation_revision"] == 0
    assert status["draft_save_effect"] == "none_until_explicit_activation"
    units = {unit["unit_id"]: unit for unit in status["units"]}
    assert units["intake.classification"]["dirty"] is True
    assert units["source.template-document"]["dirty"] is True
    assert units["companion.chat"]["dirty"] is False


def test_fresh_first_save_keeps_product_core_fallback_active(tmp_path: Path) -> None:
    store = _store(tmp_path)

    saved = _save(
        store,
        revision=0,
        prompts=[_prompt("pt-input-understanding", "first draft", 1), *_template_prompts("first draft", 1)],
    )

    assert resolve_active_prompt(saved, "pt-input-understanding") is None
    status = serialize_prompt_activation(saved)
    assert status["activation_revision"] == 0
    assert all(unit["active_prompt_ids"] == [] for unit in status["units"])
    units = {unit["unit_id"]: unit for unit in status["units"]}
    assert units["intake.classification"]["dirty"] is True
    assert units["source.template-document"]["dirty"] is True
    assert units["companion.chat"]["dirty"] is False


def test_companion_prompt_preview_names_only_companion_consumers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, revision=0, prompts=[_prompt("pt-companion-character", "温柔地回复")])
    preview = PromptActivationService(store).preview(
        unit_id="companion.chat",
        expected_config_revision=1,
        expected_activation_revision=0,
    )
    assert preview["consumer_manifest"] == ["companion.prompt-composer", "model-route:companion.chat"]
    assert "source.provider-template-document" not in preview["consumer_manifest"]


def test_intake_preview_activate_replay_restart_and_rollback_preserve_draft(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    drafts = [_prompt("pt-input-understanding", "draft intake", 2), *_template_prompts("active", 1)]
    saved = _save(store, revision=4, prompts=drafts)
    service = PromptActivationService(store, now="2026-07-17T00:00:03+08:00")

    preview = service.preview(
        unit_id="intake.classification", expected_config_revision=5, expected_activation_revision=0,
    )
    activated = service.activate(
        unit_id="intake.classification",
        expected_config_revision=5,
        expected_activation_revision=0,
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="validated intake draft",
    )

    assert activated.status == "activated"
    assert activated.config_revision == 6
    assert activated.activation_revision == 1
    reloaded = GetDeveloperStudioConfig(store).execute()
    assert resolve_active_prompt(reloaded, "pt-input-understanding")["content"] == "draft intake"
    assert reloaded.prompts[0]["content"] == "draft intake"

    replay_preview = service.preview(
        unit_id="intake.classification", expected_config_revision=6, expected_activation_revision=1,
    )
    replay = service.activate(
        unit_id="intake.classification",
        expected_config_revision=6,
        expected_activation_revision=1,
        preview_token=str(replay_preview["preview_token"]),
        confirm=True,
        reason="same validated draft",
    )
    assert replay.status == "already_active"
    assert replay.replayed is True
    assert GetDeveloperStudioConfig(store).execute().revision == 6

    rolled_back = service.rollback(
        unit_id="intake.classification",
        expected_config_revision=6,
        expected_activation_revision=1,
        confirm=True,
        reason="restore previous intake",
    )
    assert rolled_back.status == "rolled_back"
    after = GetDeveloperStudioConfig(store).execute()
    assert after.revision == 7
    assert resolve_active_prompt(after, "pt-input-understanding")["content"] == "active legacy"
    assert after.prompts[0]["content"] == "draft intake"
    rollback_status = serialize_prompt_activation(after)
    assert rollback_status["units"][0]["source_config_revision"] == 4
    assert rollback_status["units"][0]["can_rollback"] is False


def test_template_activation_is_complete_and_atomic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    incomplete = [_prompt("pt-input-understanding", "active legacy"), *_template_prompts("draft", 2)[:-1]]
    _save(store, revision=4, prompts=incomplete)
    service = PromptActivationService(store)

    with pytest.raises(PromptActivationError, match="pt-output-validate"):
        service.preview(
            unit_id="source.template-document", expected_config_revision=5, expected_activation_revision=0,
        )

    complete = [*incomplete, _prompt("pt-output-validate", "draft validate", 2)]
    _save(store, revision=5, prompts=complete)
    preview = service.preview(
        unit_id="source.template-document", expected_config_revision=6, expected_activation_revision=0,
    )
    result = service.activate(
        unit_id="source.template-document",
        expected_config_revision=6,
        expected_activation_revision=0,
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="validated template group",
    )

    assert result.status == "activated"
    active = GetDeveloperStudioConfig(store).execute()
    assert [resolve_active_prompt(active, prompt_id)["content"] for prompt_id in (
        "pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate",
    )] == ["draft title", "draft detail", "draft organize", "draft validate"]


def test_preview_token_and_double_revision_cas_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    drafts = [_prompt("pt-input-understanding", "draft", 2), *_template_prompts("active", 1)]
    _save(store, revision=4, prompts=drafts)
    service = PromptActivationService(store)
    preview = service.preview(
        unit_id="intake.classification", expected_config_revision=5, expected_activation_revision=0,
    )

    with pytest.raises(PromptActivationConflict, match="preview drifted"):
        service.activate(
            unit_id="intake.classification", expected_config_revision=5, expected_activation_revision=0,
            preview_token="wrong", confirm=True, reason="invalid token",
        )
    with pytest.raises(PromptActivationConflict, match="config revision"):
        service.preview(
            unit_id="intake.classification", expected_config_revision=4, expected_activation_revision=0,
        )
    with pytest.raises(PromptActivationConflict, match="activation revision"):
        service.preview(
            unit_id="intake.classification", expected_config_revision=5, expected_activation_revision=1,
        )
    assert GetDeveloperStudioConfig(store).execute().revision == 5
    assert preview["changed"] is True


def test_activation_rejects_secret_like_prompt_and_confirmation_gaps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    clean = _save(
        store,
        revision=4,
        prompts=[_prompt("pt-input-understanding", "clean draft", 2), *_template_prompts("active", 1)],
    )
    tampered = dict(store.read("developer_studio_configs", "default"))
    tampered["prompts"] = [
        _prompt("pt-input-understanding", "Authorization: Bearer secret-value", 2),
        *_template_prompts("active", 1),
    ]
    store.write("developer_studio_configs", "default", tampered, expected_revision=None)
    assert clean.revision == 5
    service = PromptActivationService(store)

    with pytest.raises(PromptActivationError, match="secret-like"):
        service.preview(
            unit_id="intake.classification", expected_config_revision=5, expected_activation_revision=0,
        )
    with pytest.raises(PromptActivationError, match="unsupported"):
        service.preview(unit_id="pt-video-summary", expected_config_revision=5, expected_activation_revision=0)


def test_tampered_active_projection_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    _save(
        store,
        revision=4,
        prompts=[_prompt("pt-input-understanding", "draft", 2), *_template_prompts("draft", 2)],
    )
    record = dict(store.read("developer_studio_configs", "default"))
    projection = dict(record["prompt_activation"])
    units = dict(projection["units"])
    intake = dict(units["intake.classification"])
    intake["active_prompts"] = [_prompt("pt-video-summary", "wrong member", 1)]
    units["intake.classification"] = intake
    projection["units"] = units
    record["prompt_activation"] = projection
    store.write("developer_studio_configs", "default", record, expected_revision=None)

    with pytest.raises(PromptActivationError, match="membership drifted"):
        GetDeveloperStudioConfig(store).execute()


def test_tampered_rollback_history_fails_closed_before_it_can_replace_active_prompts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    saved = _save(
        store,
        revision=4,
        prompts=[_prompt("pt-input-understanding", "draft", 2), *_template_prompts("active", 1)],
    )
    service = PromptActivationService(store)
    preview = service.preview(
        unit_id="intake.classification",
        expected_config_revision=saved.revision,
        expected_activation_revision=0,
    )
    activated = service.activate(
        unit_id="intake.classification",
        expected_config_revision=saved.revision,
        expected_activation_revision=0,
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="valid activation",
    )
    record = dict(store.read("developer_studio_configs", "default"))
    projection = dict(record["prompt_activation"])
    history = [dict(item) for item in projection["history"]]
    history[0]["before"] = [_prompt("pt-video-summary", "wrong rollback target", 1)]
    projection["history"] = history
    record["prompt_activation"] = projection
    store.write("developer_studio_configs", "default", record, expected_revision=None)

    with pytest.raises(PromptActivationError, match="history before membership drifted"):
        service.rollback(
            unit_id="intake.classification",
            expected_config_revision=activated.config_revision,
            expected_activation_revision=activated.activation_revision,
            confirm=True,
            reason="must not use tampered history",
        )


def test_production_prompt_helpers_read_active_snapshot_until_activation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    drafts = [_prompt("pt-input-understanding", "draft intake", 2), *_template_prompts("draft", 2)]
    _save(store, revision=4, prompts=drafts)

    intake_before = _developer_studio_prompt(store, "pt-input-understanding")
    templates_before = _developer_studio_prompts(
        store,
        ("pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate"),
    )
    draft_before = _developer_studio_draft_prompt(store, "pt-input-understanding")

    assert intake_before is not None
    assert intake_before["source"] == "developer_studio_active"
    assert intake_before["content"] == "active legacy"
    assert [item["content"] for item in templates_before] == [
        "active title", "active detail", "active organize", "active validate",
    ]
    assert draft_before is not None
    assert draft_before["content"] == "draft intake"

    service = PromptActivationService(store)
    intake_preview = service.preview(
        unit_id="intake.classification", expected_config_revision=5, expected_activation_revision=0,
    )
    service.activate(
        unit_id="intake.classification",
        expected_config_revision=5,
        expected_activation_revision=0,
        preview_token=str(intake_preview["preview_token"]),
        confirm=True,
        reason="activate intake helper",
    )
    template_preview = service.preview(
        unit_id="source.template-document", expected_config_revision=6, expected_activation_revision=1,
    )
    service.activate(
        unit_id="source.template-document",
        expected_config_revision=6,
        expected_activation_revision=1,
        preview_token=str(template_preview["preview_token"]),
        confirm=True,
        reason="activate template helpers",
    )

    assert _developer_studio_prompt(store, "pt-input-understanding")["content"] == "draft intake"
    assert [item["content"] for item in _developer_studio_prompts(
        store,
        ("pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate"),
    )] == ["draft title", "draft detail", "draft organize", "draft validate"]


def test_metadata_only_prompt_never_acquires_a_production_active_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_legacy(store)
    current = list(GetDeveloperStudioConfig(store).execute().prompts)
    current.append(_prompt("pt-video-summary", "metadata-only draft", 1))
    _save(store, revision=4, prompts=current)

    assert _developer_studio_prompt(store, "pt-video-summary") is None
    assert _developer_studio_draft_prompt(store, "pt-video-summary")["content"] == "metadata-only draft"


def test_config_save_and_activation_use_physical_store_cas(tmp_path: Path) -> None:
    racing = _RacingStore(_store(tmp_path))
    _seed_legacy(racing)
    drafts = [_prompt("pt-input-understanding", "draft", 2), *_template_prompts("draft", 2)]

    racing.race_next_conditional_write = True
    with pytest.raises(DeveloperStudioConfigError, match="developer studio config revision conflict"):
        _save(racing, revision=4, prompts=drafts)
    assert racing.read("developer_studio_configs", "default")["concurrent_marker"] == "other writer"

    saved = _save(racing, revision=4, prompts=drafts)
    service = PromptActivationService(racing)
    preview = service.preview(
        unit_id="intake.classification",
        expected_config_revision=saved.revision,
        expected_activation_revision=0,
    )
    racing.race_next_conditional_write = True
    with pytest.raises(PromptActivationConflict, match="storage revision conflict"):
        service.activate(
            unit_id="intake.classification",
            expected_config_revision=saved.revision,
            expected_activation_revision=0,
            preview_token=str(preview["preview_token"]),
            confirm=True,
            reason="race proof",
        )
    assert resolve_active_prompt(GetDeveloperStudioConfig(racing).execute(), "pt-input-understanding")["content"] == "active legacy"
