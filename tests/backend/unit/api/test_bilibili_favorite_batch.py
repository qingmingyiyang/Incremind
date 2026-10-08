from __future__ import annotations

from types import SimpleNamespace
import time

import pytest

from backend.api.bilibili_favorite_batch import (
    BilibiliFavoriteBatchRepository,
    batch_public,
)
from backend.api.bilibili_favorite_batch_runtime import (
    BilibiliFavoriteBatchEffectHandler,
    EFFECT_KIND,
    admit_bilibili_favorite_batch_command,
)
from backend.api.job_execution_runtime import register_job_execution_handler
from backend.api.routes import bilibili_media_ingress as routes
from core.effect_log import build_effect_runtime
from core.effect_log.runtime import EffectExecutionCancelled
from core.storage_provider import JsonObjectStore
from core.source_processing import SourcePermissionAuthority


def _snapshot_items():
    return [
        {
            "ordinal": ordinal,
            "bvid": bvid,
            "url": f"https://www.bilibili.com/video/{bvid}/",
            "title": f"视频 {ordinal + 1}",
        }
        for ordinal, bvid in enumerate(("BV1xx411c7mD", "BV17x411w7KC", "BV1Q541167Qg"))
    ]


def _repository(tmp_path):
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    return store, BilibiliFavoriteBatchRepository(store, namespace_id="default")


def test_batch_create_is_idempotent_and_tracks_cas_revision(tmp_path) -> None:
    _store, repository = _repository(tmp_path)
    arguments = {
        "batch_id": "favorite-batch-0001",
        "project_id": "default",
        "snapshot_ref": "crp://default/bilibili-favorite-snapshots/projects/default/snapshot-1/r1",
        "snapshot_revision": "r1",
        "items": _snapshot_items(),
        "created_at": "2026-09-05T00:00:00Z",
    }

    first = repository.create(**arguments)
    replay = repository.create(**arguments)
    updated = dict(first.payload, status="running", updated_at="2026-09-05T00:01:00Z")
    saved = repository.save(updated)

    assert first.replayed is False and replay.replayed is True
    assert saved.payload["revision"] == 2
    assert batch_public(saved.payload)["counts"] == {
        "pending": 3, "processing": 0, "admitted": 0, "failed": 0
    }


def test_batch_continues_after_partial_failure_and_retries_only_failed_items(
    tmp_path, monkeypatch,
) -> None:
    store, repository = _repository(tmp_path)
    batch = repository.create(
        batch_id="favorite-batch-0002",
        project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-2/r1",
        snapshot_revision="r1",
        items=_snapshot_items(),
        created_at="2026-09-05T00:00:00Z",
    )
    failed_bvids = {"BV17x411w7KC"}
    resolved_urls = []

    def resolve(_request, _container, _request_id, _project_id, url):
        resolved_urls.append(url)
        bvid = url.rstrip("/").rsplit("/", 1)[-1]
        if bvid in failed_bvids:
            raise ValueError("metadata_unavailable")
        resolved = SimpleNamespace(
            manifest=SimpleNamespace(platform="bilibili", source_id=f"bili-{bvid}"),
            manifest_ref=f"crp://default/source-manifests/projects/default/{bvid}",
            manifest_revision="r1",
            permission_snapshot=None,
        )
        return object(), resolved

    def admit(_request, _container, request_id, _project_id, manifest_ref, _platform):
        bvid = manifest_ref.rsplit("/", 1)[-1]
        resolved = SimpleNamespace(manifest_ref=manifest_ref)
        admission = SimpleNamespace(
            record=SimpleNamespace(payload={"id": f"media_hands:{bvid}:{request_id}"})
        )
        return object(), object(), resolved, admission

    monkeypatch.setattr(routes, "_resolve_with_selection", resolve)
    monkeypatch.setattr(routes, "_ensure_favorite_source_permission", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_admit_with_selection", admit)

    first = routes._process_favorite_batch(
        object(), object(), store, "default", repository, batch.payload, False
    )

    assert first["status"] == "partial_failure"
    assert first["counts"] == {
        "pending": 0, "processing": 0, "admitted": 2, "failed": 1
    }
    resolved_urls.clear()
    failed_bvids.clear()
    current = repository.get("favorite-batch-0002")
    retried = routes._process_favorite_batch(
        object(), object(), store, "default", repository, current.payload, True
    )

    assert retried["status"] == "completed"
    assert retried["counts"]["admitted"] == 3
    assert resolved_urls == ["https://www.bilibili.com/video/BV17x411w7KC/"]
    assert retried["items"][1]["attempts"] == 2


def test_batch_confirmation_records_one_idempotent_permission_per_video(tmp_path) -> None:
    store, repository = _repository(tmp_path)
    assert repository is not None
    evidence_ref = (
        "crp://default/source-resolution-evidence/projects/default/"
        "bili-BV1xx411c7mD--view-v1/r1"
    )
    resolved = SimpleNamespace(
        permission_snapshot=None,
        manifest_ref="crp://default/source-manifests/projects/default/bili-manifest-1",
        manifest_revision="r1",
        manifest=SimpleNamespace(
            source_id="bili-BV1xx411c7mD-p1",
            permission=SimpleNamespace(evidence_refs=(evidence_ref,)),
        ),
    )

    routes._ensure_favorite_source_permission(
        store=store,
        namespace_id="default",
        project_id="default",
        batch_id="favorite-batch-0003",
        ordinal=0,
        resolved=resolved,
    )
    routes._ensure_favorite_source_permission(
        store=store,
        namespace_id="default",
        project_id="default",
        batch_id="favorite-batch-0003",
        ordinal=0,
        resolved=resolved,
    )

    permission = SourcePermissionAuthority(store, namespace_id="default").current_for_source(
        project_id="default",
        source_id="bili-BV1xx411c7mD-p1",
        metadata_evidence_ref=evidence_ref,
    )
    assert permission is not None
    assert permission.state == "granted"
    assert permission.revision == 1
    assert permission.command_id.startswith("fav-0-grant-")


def test_durable_command_replays_after_restart_without_duplicate_effect(tmp_path) -> None:
    store, repository = _repository(tmp_path)
    batch = repository.create(
        batch_id="favorite-batch-0010", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-10/r1",
        snapshot_revision="r1", items=_snapshot_items(), created_at="2026-09-05T00:00:00Z",
    )
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=runtime))
    first, replayed = admit_bilibili_favorite_batch_command(
        application=application, runtime_root=tmp_path, repository=repository,
        batch_payload=batch.payload, command_id="favorite-command-0010", kind="admit", retry_failed=False,
    )
    reopened = BilibiliFavoriteBatchRepository(store, namespace_id="default").get("favorite-batch-0010")
    assert reopened is not None
    second, replayed_again = admit_bilibili_favorite_batch_command(
        application=application, runtime_root=tmp_path, repository=repository,
        batch_payload=reopened.payload, command_id="favorite-command-0010", kind="admit", retry_failed=False,
    )
    assert replayed is False and replayed_again is True
    assert first["command"]["operation_id"] == second["command"]["operation_id"]
    assert runtime.log.get(first["command"]["operation_id"]).kind == EFFECT_KIND


def test_chunking_more_than_ten_items_keeps_unstarted_items_queued(tmp_path, monkeypatch) -> None:
    store, repository = _repository(tmp_path)
    items = [
        {"ordinal": index, "bvid": f"BV1xx411c{index:03d}", "url": f"https://www.bilibili.com/video/BV1xx411c{index:03d}/", "title": str(index)}
        for index in range(11)
    ]
    batch = repository.create(
        batch_id="favorite-batch-0011", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-11/r1",
        snapshot_revision="r1", items=items, created_at="2026-09-05T00:00:00Z",
    )
    resolved = SimpleNamespace(
        manifest=SimpleNamespace(platform="bilibili", source_id="bili-source"),
        manifest_ref="crp://default/source-manifests/projects/default/bili", manifest_revision="r1", permission_snapshot=object(),
    )
    monkeypatch.setattr(routes, "_resolve_with_selection", lambda *_args: (object(), resolved))
    monkeypatch.setattr(routes, "_admit_with_selection", lambda *_args: (object(), object(), resolved, SimpleNamespace(record=SimpleNamespace(payload={"id": "media-job"}))))
    first = routes._process_favorite_batch(object(), object(), store, "default", repository, batch.payload, False)
    assert first["counts"] == {"pending": 1, "processing": 0, "admitted": 10, "failed": 0}
    assert first["items"][-1]["state"] == "pending"
    assert first["admission_status"] == "not_started"


def test_core_effect_handler_completes_durable_batch_after_http_disconnect(tmp_path, monkeypatch) -> None:
    store, repository = _repository(tmp_path)
    batch = repository.create(
        batch_id="favorite-batch-0012", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-12/r1",
        snapshot_revision="r1", items=_snapshot_items(), created_at="2026-09-05T00:00:00Z",
    )
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    application = SimpleNamespace(state=SimpleNamespace(
        effect_runtime=runtime, container=SimpleNamespace(root_dir=tmp_path),
    ))
    monkeypatch.setattr(
        "backend.api.bilibili_favorite_batch_runtime.build_rebuild_object_store",
        lambda _root: (store, SimpleNamespace(namespace_id="default")),
    )
    register_job_execution_handler(application, tmp_path, runtime)
    assert EFFECT_KIND in runtime.handlers.kinds()

    def complete(_request, _container, _store, _namespace, repo, payload, _retry):
        items = [dict(item, state="admitted", job_id=f"media-job-{item['ordinal']}") for item in payload["items"]]
        return batch_public(repo.save(dict(payload, items=items, status="completed", updated_at="2026-09-05T00:01:00Z")).payload)

    monkeypatch.setattr(routes, "_process_favorite_batch", complete)
    accepted, _replayed = admit_bilibili_favorite_batch_command(
        application=application, runtime_root=tmp_path, repository=repository,
        batch_payload=batch.payload, command_id="favorite-command-0012", kind="admit", retry_failed=False,
    )
    effect = runtime.dispatch_operation(accepted["command"]["operation_id"], now=int(time.time()))
    completed = repository.get("favorite-batch-0012")
    assert effect.state.value == "SETTLED_OK"
    assert completed is not None and completed.payload["admission_status"] == "complete"


def test_checkpoint_stops_before_the_second_chunk(tmp_path, monkeypatch) -> None:
    store, repository = _repository(tmp_path)
    items = [
        {"ordinal": index, "bvid": f"BV1xx411c{index:03d}", "url": f"https://www.bilibili.com/video/BV1xx411c{index:03d}/", "title": str(index)}
        for index in range(11)
    ]
    batch = repository.create(
        batch_id="favorite-batch-0013", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-13/r1",
        snapshot_revision="r1", items=items, created_at="2026-09-05T00:00:00Z",
    )
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    application = SimpleNamespace(state=SimpleNamespace(
        effect_runtime=runtime, container=SimpleNamespace(root_dir=tmp_path),
    ))
    accepted, _ = admit_bilibili_favorite_batch_command(
        application=application, runtime_root=tmp_path, repository=repository,
        batch_payload=batch.payload, command_id="favorite-command-0013", kind="admit", retry_failed=False,
    )
    effect = runtime.log.get(accepted["command"]["operation_id"])
    calls: list[int] = []
    checkpoints: list[str] = []
    monkeypatch.setattr(
        "backend.api.bilibili_favorite_batch_runtime.build_rebuild_object_store",
        lambda _root: (store, SimpleNamespace(namespace_id="default")),
    )

    def one_chunk(_request, _container, _store, _namespace, _repo, _payload, _retry):
        calls.append(1)
        return {"has_pending": True}

    def checkpoint() -> None:
        checkpoints.append("checked")
        if len(checkpoints) == 2:
            raise EffectExecutionCancelled("cancel between chunks")

    monkeypatch.setattr(routes, "_process_favorite_batch", one_chunk)
    handler = BilibiliFavoriteBatchEffectHandler(
        tmp_path, tmp_path / "effects.sqlite3", application, lambda _effect: checkpoint,
    )
    with pytest.raises(EffectExecutionCancelled, match="between chunks"):
        handler(effect)
    assert calls == [1]
    assert checkpoints == ["checked", "checked"]


def test_processing_projection_requires_read_back_artifacts(tmp_path, monkeypatch) -> None:
    store, repository = _repository(tmp_path)
    batch = repository.create(
        batch_id="favorite-batch-0014", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-14/r1",
        snapshot_revision="r1", items=_snapshot_items(), created_at="2026-09-05T00:00:00Z",
    )
    admitted = repository.save(dict(
        batch.payload,
        status="completed",
        admission_status="complete",
        processing_status="queued",
        items=[dict(item, state="admitted", job_id=f"media-job-{item['ordinal']}") for item in batch.payload["items"]],
        updated_at="2026-09-05T00:01:00Z",
    ))
    monkeypatch.setattr(
        routes, "build_rebuild_job_repository",
        lambda *_args: SimpleNamespace(get=lambda _job_id: {"status": "completed", "published_outputs": []}),
    )
    projection = routes._favorite_batch_projection(tmp_path, store, admitted.payload)
    assert admitted.payload["processing_status"] == "queued"
    assert projection["processing_status"] == "unknown"


@pytest.mark.parametrize(("job_status", "expected"), [
    ("waiting_user", "waiting_user"),
    ("completed", "complete"),
])
def test_processing_projection_distinguishes_waiting_user_and_read_back_delivery(
    tmp_path, monkeypatch, job_status, expected,
) -> None:
    store, repository = _repository(tmp_path)
    batch = repository.create(
        batch_id=f"favorite-batch-status-{job_status}", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-status/r1",
        snapshot_revision="r1", items=_snapshot_items(), created_at="2026-09-05T00:00:00Z",
    )
    admitted = repository.save(dict(
        batch.payload, status="completed", admission_status="complete", processing_status="queued",
        items=[dict(item, state="admitted", job_id=f"media-job-{item['ordinal']}") for item in batch.payload["items"]],
        updated_at="2026-09-05T00:01:00Z",
    ))
    job = {"status": job_status, "published_outputs": [{"kind": "media_output", "object_id": "output-1"}]}
    monkeypatch.setattr(routes, "build_rebuild_job_repository", lambda *_args: SimpleNamespace(get=lambda _job_id: job))
    store.write("media_processing_outputs", "output-1", {"status": "ready"}, expected_revision=0)

    projection = routes._favorite_batch_projection(tmp_path, store, admitted.payload)

    assert projection["processing_status"] == expected
    assert all(child["openable"] is True for child in projection["child_outputs"])


def test_processing_projection_marks_empty_collection_without_success_claim(tmp_path, monkeypatch) -> None:
    store, repository = _repository(tmp_path)
    batch = repository.create(
        batch_id="favorite-batch-empty-001", project_id="default",
        snapshot_ref="crp://default/bilibili-favorite-snapshots/projects/default/snapshot-empty/r1",
        snapshot_revision="r1", items=[], created_at="2026-09-05T00:00:00Z",
    )
    admitted = repository.save(dict(
        batch.payload, status="completed", admission_status="complete", processing_status="not_started",
        updated_at="2026-09-05T00:01:00Z",
    ))
    monkeypatch.setattr(routes, "build_rebuild_job_repository", lambda *_args: SimpleNamespace(get=lambda _job_id: None))

    projection = routes._favorite_batch_projection(tmp_path, store, admitted.payload)

    assert projection["total"] == 0
    assert projection["processing_status"] == "empty"
