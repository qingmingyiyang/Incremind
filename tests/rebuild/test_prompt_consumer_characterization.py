from __future__ import annotations

import pytest

from backend.api.routes.product.developer_test_lab import (
    _TestLabFidelityError,
    _test_lab_prompt_snapshot,
)
from backend.api.routes.product.developer_prompt_catalog import (
    _developer_studio_prompt_refs,
    _developer_studio_prompts,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed(store: JsonObjectStore) -> None:
    prompt_ids = (
        "pt-classification",
        "pt-summary",
        "pt-tags",
        "pt-longterm-organize",
        "pt-title",
        "pt-detail-summary",
        "pt-output-validate",
        "pt-video-summary",
        "pt-empty-handle",
    )
    store.write(
        "developer_studio_configs",
        "default",
        {
            "schema_version": "1.0.0",
            "id": "default",
            "revision": 9,
            "model_profiles": [],
            "task_model_map": {},
            "prompts": [
                {
                    "id": prompt_id,
                    "stageId": prompt_id.removeprefix("pt-"),
                    "modelProfileId": "mp-test",
                    "version": index,
                    "content": f"sentinel body for {prompt_id}",
                }
                for index, prompt_id in enumerate(prompt_ids, start=1)
            ],
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
            "updated_at": "2026-07-17T00:00:00+08:00",
        },
        expected_revision=None,
    )


def test_structuring_reads_prompt_identity_but_not_prompt_body(tmp_path) -> None:
    store = _store(tmp_path)
    _seed(store)

    refs = _developer_studio_prompt_refs(
        store,
        ("pt-classification", "pt-summary", "pt-tags", "pt-longterm-organize"),
    )

    assert [item["id"] for item in refs] == [
        "pt-classification", "pt-summary", "pt-tags", "pt-longterm-organize",
    ]
    assert all("content" not in item for item in refs)
    assert [item["revision"] for item in refs] == [1, 2, 3, 4]


def test_provider_template_loader_preserves_bodies_for_the_four_verified_prompts(tmp_path) -> None:
    store = _store(tmp_path)
    _seed(store)

    prompts = _developer_studio_prompts(
        store,
        ("pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate"),
    )

    assert [item["id"] for item in prompts] == [
        "pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate",
    ]
    assert [item["content"] for item in prompts] == [
        "sentinel body for pt-title",
        "sentinel body for pt-detail-summary",
        "sentinel body for pt-longterm-organize",
        "sentinel body for pt-output-validate",
    ]


def test_media_template_loader_records_draft_metadata_without_claiming_active_body(tmp_path) -> None:
    store = _store(tmp_path)
    _seed(store)

    prompts = _developer_studio_prompts(store, ("pt-video-summary",))
    refs = _developer_studio_prompt_refs(store, ("pt-video-summary",))

    assert prompts == ()
    assert refs == (
        {
            "id": "pt-video-summary",
            "revision": 8,
            "source": "developer_studio",
            "stage_id": "video-summary",
            "model_profile_id": "mp-test",
        },
    )


def test_test_lab_resolves_an_exact_saved_draft_without_making_it_a_production_consumer(tmp_path) -> None:
    store = _store(tmp_path)
    _seed(store)

    content, evidence = _test_lab_prompt_snapshot(
        store,
        prompt_id="pt-empty-handle",
        source="draft",
        expected_config_revision=9,
        expected_activation_revision=None,
        expected_unit_revision=None,
    )
    assert content == "sentinel body for pt-empty-handle"
    assert evidence["source"] == "draft"
    assert evidence["config_revision"] == 9
    with pytest.raises(_TestLabFidelityError, match="not found"):
        _test_lab_prompt_snapshot(
            store,
            prompt_id="pt-not-present",
            source="draft",
            expected_config_revision=9,
            expected_activation_revision=None,
            expected_unit_revision=None,
        )
