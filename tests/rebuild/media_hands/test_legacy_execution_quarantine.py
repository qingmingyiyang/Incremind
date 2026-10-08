from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.job_execution_runtime import register_job_execution_handler
import backend.api.media_hands_composition as media_composition
from core.effect_log import EffectClass, EffectIntent, EffectState, build_effect_runtime
from core.job_runner import (
    SQLiteJobStore,
    SQLiteLegacyMediaExecutionBlocked,
    job_execution_operation_id,
)
from core.job_runner.legacy_history import LegacyJobHistoryProjection


def _legacy_media_job() -> dict[str, object]:
    return {
        "id": "legacy-media-quarantine-001",
        "job_type": "media_hands",
        "status": "pending",
        "attempt": 0,
        "steps": [{"name": "execute_operation", "status": "pending"}],
        "media_hands": {
            "operation": "analyze_source",
            "manifest": {"ref": "crp://jobs/manifests/legacy-001", "revision": "r1"},
            "budget": {"max_download_bytes": 1, "max_media_cpu_ms": 1},
            "permission_snapshot": {
                "project_id": "project-legacy-media",
                "manifest_ref": "crp://jobs/manifests/legacy-001",
                "manifest_revision": "r1",
                "grant_ref": "crp://jobs/permissions/legacy-001",
                "grant_revision": "r1",
                "revocation_generation": 0,
            },
            "policy": {"lane_max_concurrency": {}},
        },
    }


def _relevant_database_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND (name LIKE '%effect%' OR name LIKE '%evidence%' "
                "OR name LIKE '%receipt%' OR name LIKE '%projection%' "
                "OR name LIKE '%lease%') ORDER BY name"
            )
        )
        return {
            name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in names
        }


def _store_with_legacy_media_history(
    database: Path, *, status: str,
) -> SQLiteJobStore:
    store = SQLiteJobStore(database)
    payload = _legacy_media_job()
    payload["status"] = status
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        LegacyJobHistoryProjection().import_in_connection(
            connection,
            migration_id=f"legacy-media-{status}-v1",
            source_kind="sqlite-job-store",
            source_ref=f"legacy-media-{status}",
            job_id=str(payload["id"]),
            payload=payload,
            revision=1,
            imported_at="2026-08-30T00:00:00Z",
        )
        connection.commit()
    return store


def test_generic_job_execution_quarantines_legacy_media_hands_without_composition_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    runtime = build_effect_runtime(database, owner_id="core", lease_seconds=1)
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=runtime))
    register_job_execution_handler(application, tmp_path, runtime)
    monkeypatch.setattr(
        "backend.api.job_runtime.build_rebuild_job_repository",
        lambda *_args, **_kwargs: SimpleNamespace(
            sqlite=SimpleNamespace(
                read=lambda _job_id: SimpleNamespace(
                    payload=_legacy_media_job(), revision=1
                )
            )
        ),
    )
    operation_id = job_execution_operation_id("legacy-media-quarantine-001", 0)
    effect, _created = runtime.log.plan(
        EffectIntent(
            session_id="legacy-media-quarantine",
            root_id="legacy-media-quarantine-001",
            step_key="job_execution:0",
            kind="job_execution",
            effect_class=EffectClass.QUERYABLE,
            intent_ref="facts:legacy-media-quarantine-001",
            gate_decision_id="legacy-media-readonly",
            rev_set={"job_projection_schema": "1"},
            payload={"job_id": "legacy-media-quarantine-001", "attempt": 0},
            operation_id_override=operation_id,
        ),
        now=1,
    )
    probe = runtime.recoveries.probes()["job_execution"]

    original_import = builtins.__import__

    def reject_legacy_media_composition(name, *args, **kwargs):
        if name == "backend.api.media_hands_composition":
            raise AssertionError("legacy media execution imported its composition")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_legacy_media_composition)

    state, reason = probe(effect)

    assert state is EffectState.UNKNOWN
    assert reason == "job_execution.media_hands_legacy_readonly"
    with pytest.raises(
        RuntimeError,
        match="job_execution.media_hands_legacy_readonly",
    ):
        runtime.dispatch_operation(effect.operation_id, now=100)


def test_generic_job_store_cannot_create_legacy_media_execution_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        store.bind(connection)
        connection.commit()
    before = _relevant_database_counts(database)

    with pytest.raises(SQLiteLegacyMediaExecutionBlocked):
        store.create(_legacy_media_job())

    assert _relevant_database_counts(database) == before
    assert store.read("legacy-media-quarantine-001") is None


def test_generic_job_store_cannot_spoof_an_existing_media_projection_type(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    legacy = _legacy_media_job()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        store.bind(connection)
        connection.execute(
            "INSERT INTO job_projection(job_id,payload_json,revision,rebuilt_at) "
            "VALUES(?,?,?,?)",
            (
                str(legacy["id"]),
                json.dumps(legacy, sort_keys=True, separators=(",", ":")),
                1,
                "2026-08-30T00:00:00Z",
            ),
        )
        connection.commit()
    before = _relevant_database_counts(database)
    spoofed = dict(legacy)
    spoofed["job_type"] = "workbench_auto_intake"

    with pytest.raises(SQLiteLegacyMediaExecutionBlocked):
        store.save(spoofed, expected_revision=1)

    assert _relevant_database_counts(database) == before


@pytest.mark.parametrize(
    ("legacy_status", "display_status"),
    (("pending", "legacy_unknown"), ("running", "legacy_unknown"), ("completed", "completed")),
)
def test_composed_lifecycle_enqueues_legacy_media_only_to_core_effect_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_status: str,
    display_status: str,
) -> None:
    database = tmp_path / legacy_status / "jobs.sqlite3"
    store = _store_with_legacy_media_history(database, status=legacy_status)
    repository = SimpleNamespace(sqlite=store)
    application = SimpleNamespace(
        state=SimpleNamespace(
            effect_runtime=SimpleNamespace(dispatch_operation=lambda *_args, **_kwargs: None)
        )
    )
    resolution = SimpleNamespace(
        runtime=SimpleNamespace(
            handler=SimpleNamespace(
                job_type="media_hands",
            )
        )
    )
    monkeypatch.setattr(
        media_composition,
        "configure_media_hands_runtime",
        lambda *_args, **_kwargs: resolution,
    )
    monkeypatch.setattr(
        media_composition,
        "_compose_expert_media_job_wait_bridge",
        lambda *_args, **_kwargs: None,
    )

    composed = media_composition.compose_media_hands_lifecycle(
        application,
        runtime_root=tmp_path,
        object_store=object(),
        repository=repository,
        namespace_id="legacy-media-quarantine",
    )
    before = _relevant_database_counts(database)
    assert composed.lifecycle.enqueue("legacy-media-quarantine-001") is True
    assert composed.lifecycle.shutdown(timeout_seconds=1) == ()
    assert store.read("legacy-media-quarantine-001").payload["status"] == display_status
    assert _relevant_database_counts(database) == before


@pytest.mark.parametrize(
    "mutation",
    (
        lambda store: store.create_media_admitted(_legacy_media_job()),
        lambda store: store.acquire_media_execution(
            "legacy-media-quarantine-001", worker_id="legacy-worker",
            lease_token="legacy-media-lease-001", now="2026-08-30T00:00:01Z",
            expires_at="2026-08-30T00:10:01Z",
        ),
        lambda store: store.consume_media_budget(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z",
            consumed={"max_download_bytes": 0, "max_media_cpu_ms": 0},
        ),
        lambda store: store.complete_media_operation(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", step_name="execute_operation",
            published_outputs=(),
            consumed={"max_download_bytes": 0, "max_media_cpu_ms": 0},
        ),
        lambda store: store.assert_media_execution_active(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z",
        ),
        lambda store: store.reserve_media_execution_evidence(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
            provider_id="legacy-provider", provider_revision="r1",
        ),
        lambda store: store.mark_media_execution_unknown(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
        ),
        lambda store: store.finalize_media_execution_evidence(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
            receipt_ref="crp://jobs/receipts/legacy-execution-001",
        ),
        lambda store: store.finalize_media_execution_with_receipt(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
            receipt_ref="crp://jobs/receipts/legacy-execution-001",
            published_outputs=(),
            checkpoint={
                "resume_step": "execute_operation",
                "checkpoint_uri": "crp://jobs/legacy-media-quarantine-001/checkpoints/execute.json",
                "state_hash": "sha256:" + "a" * 64,
                "updated_at": "2026-08-30T00:00:01Z",
            },
            consumed={"max_download_bytes": 0, "max_media_cpu_ms": 0},
            log_refs=(),
        ),
        lambda store: store.reserve_media_recipe_step(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
            provider_id="legacy-provider", provider_revision="r1", step_name="fetch_audio",
            input_state_hash="sha256:" + "a" * 64,
        ),
        lambda store: store.mark_media_recipe_step_unknown(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
            step_name="fetch_audio",
        ),
        lambda store: store.complete_media_recipe_step(
            "legacy-media-quarantine-001", lease_token="legacy-media-lease-001",
            now="2026-08-30T00:00:01Z", execution_id="legacy-execution-001",
            step_name="fetch_audio", receipt=None,  # type: ignore[arg-type]
        ),
    ),
)
def test_legacy_media_store_mutations_fail_closed_without_durable_side_effects(
    tmp_path: Path, mutation,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = _store_with_legacy_media_history(database, status="pending")
    before = _relevant_database_counts(database)

    with pytest.raises(SQLiteLegacyMediaExecutionBlocked):
        mutation(store)

    assert _relevant_database_counts(database) == before
