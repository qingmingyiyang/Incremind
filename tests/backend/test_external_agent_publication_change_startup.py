from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.api.app as api_app
from backend.api.external_agent_publication_change_startup import (
    backfill_external_agent_publication_changes,
    backfill_external_agent_publication_effects,
    dispatch_external_agent_publication_effects,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner
from core.storage_provider import SQLiteStructuredRecordStore
from core.aggregate_repository_factory import STRUCTURED_DATABASE_NAME
from core.storage_provider.external_agent_publication_change import (
    ExternalAgentPublicationChangeOutbox,
    publication_outbox_collection,
)
from core.storage_provider.external_agent_publication_backfill import (
    publication_backfill_audit_collection,
)


def _enqueue(root: Path, *, identity: str = "publication-000001") -> None:
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    with records.begin() as uow:
        ExternalAgentPublicationChangeOutbox.enqueue(
            uow,
            publication_identity=identity,
            project_id="project-a",
            change_type="memory.published",
            object_ref="crp://memory/project-a/memory-001",
            object_revision="r1",
            occurred_at="2026-08-29T08:00:00+00:00",
        )
        uow.commit()


def _historical_current_scenario(root: Path) -> None:
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    with records.begin() as uow:
        uow.put(
            "memory_scenarios", "scenario-history-001",
            {
                "id": "scenario-history-001", "revision": 1,
                "trust_status": "user_confirmed", "project_id": "project-a",
            }, expected_revision=0,
        )
        uow.put(
            "memory_transitions", "transition-history-001",
            {
                "id": "transition-history-001", "object_type": "scenario",
                "object_id": "scenario-history-001",
                "created_at": "2026-08-29T08:00:00+00:00",
            }, expected_revision=0,
        )
        uow.put(
            "memory_publications", "publication-history-001",
            {
                "schema_version": "1.0.0", "id": "publication-history-001",
                "publication_id": "publication-history-001", "status": "published",
                "layer": "scenario", "object_type": "scenario",
                "published_object_id": "scenario-history-001", "published_revision": 1,
                "published_at": "2026-08-29T08:00:00+00:00",
                "transition_ref": "crp://default/memory-transitions/transition-history-001.json",
            }, expected_revision=0,
        )
        uow.commit()


def _run_core_recovery(root: Path):
    effects = EffectLog(root / ".rebuild-data" / "jobs.sqlite3")
    backfill_external_agent_publication_effects(root, effects)
    EffectReaper(effects).recover_expired(now=2_000_000_000)
    return dispatch_external_agent_publication_effects(
        root, EffectRunner(effects, owner_id="test-publication-dispatch"),
    )


def test_core_reaper_chain_drains_durable_publication_outbox(tmp_path: Path) -> None:
    _enqueue(tmp_path)

    delivered = _run_core_recovery(tmp_path)

    assert len(delivered) == 1
    assert delivered[0].publication_identity == "publication-000001"
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    record = records.read(publication_outbox_collection("project-a"), "publication-000001")
    assert record is not None and record.payload["state"] == "delivered"


@pytest.mark.parametrize("limit", [0, 101, True])
def test_startup_backfill_rejects_unbounded_or_invalid_limit(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="1 through 100"):
        backfill_external_agent_publication_changes(tmp_path, limit=limit)


def test_startup_recovery_is_registered_without_blocking_the_application(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _enqueue(tmp_path)
    application = api_app.create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(application):
        pass

    assert "external_agent_publication" in application.state.effect_runtime.handlers.kinds()
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    delivered = records.read(publication_outbox_collection("project-a"), "publication-000001")
    assert delivered is not None and delivered.payload["state"] == "delivered"


def test_startup_backfills_current_historical_authority_before_draining(tmp_path: Path) -> None:
    _historical_current_scenario(tmp_path)
    application = api_app.create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(application):
        pass

    backfill = application.state.external_agent_publication_backfill
    assert backfill is not None and backfill.receipts[0].publication_identity == "publication-history-001"
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    delivered = records.read(publication_outbox_collection("project-a"), "publication-history-001")
    assert delivered is not None and delivered.payload["state"] == "delivered"
    assert records.read(
        publication_backfill_audit_collection("project-a"), "publication-history-001",
    ) is not None


def test_store_composition_failure_is_quiet_and_does_not_include_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("cookie=session-secret")

    monkeypatch.setattr(
        "backend.api.external_agent_publication_change_startup.SQLiteStructuredRecordStore",
        broken,
    )

    with caplog.at_level("WARNING"):
        assert backfill_external_agent_publication_changes(tmp_path) is None

    assert "external_agent_publication_backfill_failed" in caplog.messages
    assert "session-secret" not in caplog.text
