from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.effect_log import (
    EFFECT_V2, EffectClass, EffectHandlerRegistration, EffectHandlerRegistry,
    EffectLog, EffectState,
)
from core.media_hands.effect_contract import EFFECT_KIND, RECEIPT_KIND, RECEIPT_SCHEMA
from core.job_runner.job_projection import JobProjectionBuilder, initialize_job_projection_schema
from core.media_hands.effect_execution import (
    MediaHandsEffectExecutionError,
    MediaHandsEffectExecutionHandler,
    MediaHandsEffectExecutionProbe,
    MediaHandsExecutionAuthorityDrift,
)
from core.media_hands.handler import (
    MediaHandsOperationHandler,
    MediaOperationReceipt,
    SourcePermissionRevokedError,
)
from core.media_hands.job_admission import (
    MediaHandsAdmissionCommand,
    MediaHandsJobAdmissionFactory,
)
from core.job_runner import SQLiteJobStore


class _Provider:
    provider_id = "fixture-media-provider"
    provider_revision = "fixture-r1"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return MediaOperationReceipt(
            output={
                "kind": "asset", "uri": "crp://default/assets/source-1-analysis",
                "object_id": "source-1-analysis", "published": True,
            },
            checkpoint={
                "resume_step": "execute_operation",
                "checkpoint_uri": f"crp://default/jobs/{request.job_id}/checkpoints/execute.json",
                "state_hash": "sha256:" + "a" * 64,
                "updated_at": "2026-08-30T00:00:01Z",
            },
            consumed={
                "max_download_bytes": 1, "max_media_cpu_ms": 1, "max_asr_audio_ms": 1,
                "max_vision_frames": 0, "max_model_input_tokens": 0,
                "max_model_output_tokens": 0, "max_wall_ms": 1,
            },
            execution_receipt_ref="crp://default/media-receipts/source-1.json",
            log_refs=(f"crp://default/logs/jobs/{request.job_id}/execute.log",),
        )


class _PermissionChecker:
    def assert_active(self, _snapshot) -> None:
        return None


class _Verifier:
    def __init__(self) -> None:
        self.reject = False

    def assert_output_committed(self, *, output, request) -> None:
        if self.reject:
            raise ValueError("canonical output is missing")
        assert output["object_id"] == "source-1-analysis"
        assert request.source_id == "source-1"


def _job() -> dict[str, object]:
    return {
        "id": "media_hands:source-1:analyze_source",
        "source_id": "source-1",
        "job_type": "media_hands",
        "execution_version": EFFECT_V2,
        "attempt": 0,
        "checkpoint": None,
        "media_hands": {
            "manifest": {
                "ref": "crp://jobs/source-manifests/source-1", "revision": "manifest-r1",
            },
            "permission_snapshot": {
                "project_id": "project-1", "manifest_ref": "crp://jobs/source-manifests/source-1",
                "manifest_revision": "manifest-r1", "grant_ref": "crp://jobs/source-permissions/project-1/source-1/r1",
                "grant_revision": "grant-r1", "revocation_generation": 0,
            },
            "selection": {"ref": "crp://selections/media/source-1", "revision": "selection-r1", "mode": "hands"},
            "operation": "analyze_source", "policy": {"revision": "policy-r1"},
            "budget": {
                "max_download_bytes": 10, "max_media_cpu_ms": 20, "max_asr_audio_ms": 30,
                "max_vision_frames": 4, "max_model_input_tokens": 5,
                "max_model_output_tokens": 6, "max_wall_ms": 40,
            },
            "credential_use_binding": None,
        },
    }


def _handler() -> tuple[MediaHandsOperationHandler, _Provider, _Verifier]:
    provider = _Provider()
    verifier = _Verifier()
    return MediaHandsOperationHandler(provider, _PermissionChecker(), verifier), provider, verifier


def _effect(tmp_path):
    handler, provider, verifier = _handler()
    job = _job()
    admission = MediaHandsJobAdmissionFactory(admitted_at=100).build(
        job_payload=job,
        command=MediaHandsAdmissionCommand(
            request_id="request-media-1", project_id="project-1",
            selection_ref="crp://selections/media/source-1", selection_revision="selection-r1",
            selection_mode="hands",
        ),
        handler=handler,
    )
    log = EffectLog(tmp_path / "effects.sqlite")
    effect, _ = log.plan_v2(
        admission.intent, gate_decision_id=admission.authorization.gate_decision_id,
        gate_fact=admission.authorization.gate_fact, now=100,
    )
    with sqlite3.connect(log.database) as connection:
        initialize_job_projection_schema(connection)
        builder = JobProjectionBuilder()
        builder.register_node_in_connection(
            connection, job_id=str(job["id"]), node_kind="attempt", node_key="execution",
            attempt=0, effect_operation_id=effect.operation_id,
        )
        builder.append_fact_in_connection(
            connection, job_id=str(job["id"]), effect_operation_id=effect.operation_id,
            payload=job, recorded_at="2026-08-30T00:00:00+00:00",
        )
        connection.commit()
    return log, effect, handler, provider, verifier


def test_receipt_before_settle_recovery_and_exact_replay(tmp_path) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)
    execution = MediaHandsEffectExecutionHandler(log.database, handler)

    receipt = execution.handle(effect)
    assert receipt.receipt_kind == RECEIPT_KIND
    assert receipt.receipt_schema_version == RECEIPT_SCHEMA
    assert log.get(effect.operation_id).state is EffectState.PLANNED
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.SETTLED_OK, receipt.receipt_ref,
    )
    assert execution.handle(effect) == receipt
    assert provider.calls == 1
    with sqlite3.connect(log.database) as connection:
        row = connection.execute(
            "SELECT receipt_json FROM media_hands_effect_domain_receipt"
        ).fetchone()
        assert row is not None
        assert json.loads(str(row[0]))["provider_revision_identity"] == effect.rev_set["provider"]


def test_probe_reports_not_completed_without_receipt(tmp_path) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.PLANNED, f"facts:media-hands-effect-retry/{effect.operation_id}",
    )
    assert provider.calls == 0


def test_probe_fails_closed_on_canonical_evidence_contradiction(tmp_path) -> None:
    log, effect, handler, _provider, verifier = _effect(tmp_path)
    MediaHandsEffectExecutionHandler(log.database, handler).handle(effect)
    verifier.reject = True
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.UNKNOWN, "error:media-hands-effect-evidence-drift",
    )


@pytest.mark.parametrize(("field", "value"), (("provider_revision", "fixture-r2"), ("provider_id", "other-provider")))
def test_provider_identity_drift_fails_closed_before_provider_invocation(tmp_path, field, value) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)
    setattr(provider, field, value)
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.UNKNOWN, "error:media-hands-effect-evidence-drift",
    )
    with pytest.raises(MediaHandsEffectExecutionError, match="provider identity drifted"):
        MediaHandsEffectExecutionHandler(log.database, handler).handle(effect)
    assert provider.calls == 0


def test_reservation_before_provider_crash_is_unknown_and_never_replays_provider(tmp_path) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)
    crashed = MediaHandsEffectExecutionHandler(
        log.database, handler,
        after_reservation_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        crashed.handle(effect)
    assert provider.calls == 0
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.UNKNOWN, "error:media-hands-provider-attempt-reserved",
    )
    with pytest.raises(Exception, match="already reserved"):
        MediaHandsEffectExecutionHandler(log.database, handler).handle(effect)
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (("provider_revision", "fixture-r2"), ("provider_id", "other-provider")),
)
def test_provider_drift_after_reservation_never_invokes_provider(
    tmp_path, field, value,
) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)

    def drift_provider() -> None:
        setattr(provider, field, value)

    execution = MediaHandsEffectExecutionHandler(
        log.database, handler, after_reservation_write=drift_provider,
    )
    with pytest.raises(Exception, match="provider identity drifted"):
        execution.handle(effect)
    assert provider.calls == 0
    with sqlite3.connect(log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_provider_reservation"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 0
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect)[0] is EffectState.UNKNOWN


def test_permission_revoked_after_reservation_never_invokes_provider(tmp_path) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)

    def revoke_permission() -> None:
        def reject(_snapshot) -> None:
            raise SourcePermissionRevokedError("revoked after reservation")

        handler._permission_checker.assert_active = reject

    execution = MediaHandsEffectExecutionHandler(
        log.database, handler, after_reservation_write=revoke_permission,
    )
    with pytest.raises(Exception, match="revoked after reservation"):
        execution.handle(effect)
    assert provider.calls == 0
    with sqlite3.connect(log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_provider_reservation"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 0
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.UNKNOWN, "error:media-hands-provider-attempt-reserved",
    )


def test_live_authority_drift_after_reservation_never_invokes_provider(tmp_path) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)
    authority = {"current": True}

    def authorize(_effect, _job) -> None:
        if not authority["current"]:
            raise MediaHandsExecutionAuthorityDrift("live authority revision drifted")

    execution = MediaHandsEffectExecutionHandler(
        log.database,
        handler,
        execution_authorizer=authorize,
        after_reservation_write=lambda: authority.update(current=False),
    )
    with pytest.raises(MediaHandsExecutionAuthorityDrift, match="revision drifted"):
        execution.handle(effect)
    assert provider.calls == 0
    with sqlite3.connect(log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_provider_reservation"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 0
    assert MediaHandsEffectExecutionProbe(
        log.database, handler, execution_authorizer=authorize,
    ).probe(effect) == (
        EffectState.UNKNOWN, "error:media-hands-provider-attempt-reserved",
    )


def test_provider_completed_receipt_transaction_crash_is_unknown_and_never_replays(tmp_path) -> None:
    log, effect, handler, provider, _verifier = _effect(tmp_path)
    crashed = MediaHandsEffectExecutionHandler(
        log.database, handler, after_receipt_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        crashed.handle(effect)
    with sqlite3.connect(log.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_hands_effect_domain_receipt").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM media_hands_effect_provider_reservation").fetchone()[0] == 1
    assert MediaHandsEffectExecutionProbe(log.database, handler).probe(effect) == (
        EffectState.UNKNOWN, "error:media-hands-provider-attempt-reserved",
    )
    with pytest.raises(Exception, match="already reserved"):
        MediaHandsEffectExecutionHandler(log.database, handler).handle(effect)
    assert provider.calls == 1


def test_receipt_committer_is_atomic_with_parent_receipt(tmp_path) -> None:
    log, effect, handler, _provider, _verifier = _effect(tmp_path)

    def commit_child(connection, current_effect, job, receipt) -> None:
        connection.execute("CREATE TABLE IF NOT EXISTS postprocess_outbox(parent_operation_id TEXT PRIMARY KEY)")
        connection.execute(
            "INSERT INTO postprocess_outbox(parent_operation_id) VALUES(?)",
            (current_effect.operation_id,),
        )
        assert job["id"] == effect.root_id
        assert receipt["receipt_ref"].startswith("receipt:media-hands/")

    execution = MediaHandsEffectExecutionHandler(
        log.database,
        handler,
        receipt_committer=commit_child,
        after_receipt_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        execution.handle(effect)
    with sqlite3.connect(log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_hands_effect_domain_receipt"
        ).fetchone()[0] == 0
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='postprocess_outbox'"
        ).fetchone()
        assert table is None or connection.execute(
            "SELECT COUNT(*) FROM postprocess_outbox"
        ).fetchone()[0] == 0


def test_bilibili_parent_receipt_atomically_admits_postprocess_child(
    tmp_path, monkeypatch,
) -> None:
    from backend.api import bilibili_media_postprocess_runtime as postprocess_runtime
    from backend.api.bilibili_media_postprocess_runtime import (
        BilibiliReceiptPostprocessAdmission,
    )

    log, effect, _handler_value, _provider, _verifier = _effect(tmp_path)

    class _Artifacts:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve_source_ref(self, *, source_ref, project_id):
            assert source_ref == "crp://jobs/source-manifests/source-1"
            assert project_id == "project-1"
            return SimpleNamespace(
                revision="manifest-r1",
                manifest=SimpleNamespace(platform="bilibili", source_id="source-1"),
            )

    monkeypatch.setattr(postprocess_runtime, "SourceManifestArtifactRepository", _Artifacts)
    receipt = {
        "receipt_ref": f"receipt:media-hands/{effect.operation_id}",
        "output": {
            "kind": "document",
            "uri": "crp://default/documents/bili-document-1.md",
            "object_id": "bili-document-1",
            "published": True,
        },
    }
    committer = BilibiliReceiptPostprocessAdmission(
        log.database, object(), "default",
    )
    with sqlite3.connect(log.database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        committer(connection, effect, _job(), receipt)
        connection.commit()

    child = SQLiteJobStore(Path(log.database)).read(
        "bilibili-postprocess:media_hands:source-1:analyze_source"
    )
    assert child is not None
    assert child.payload["status"] == "pending"
    assert child.payload["postprocess_input"]["parent_receipt_ref"] == receipt["receipt_ref"]


def test_module_never_imports_job_lifecycle_or_worker() -> None:
    source = (Path(__file__).parents[3] / "src/core/media_hands/effect_execution.py").read_text(encoding="utf-8")
    imported = {
        node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
    }
    assert not any("sqlite_worker" in name or "sqlite_lifecycle" in name for name in imported)


def test_media_effect_kind_is_unique_in_core_handler_registry() -> None:
    registry = EffectHandlerRegistry()
    probe = lambda _effect: (EffectState.PLANNED, None)
    handler = lambda _effect: None
    registry.register(EffectHandlerRegistration(
        kind="job_execution", effect_class=EffectClass.QUERYABLE, handler=handler,
        probe=probe, contract_version=EFFECT_V2, intent_schema_version="other-v2",
        receipt_kind="other.receipt", receipt_schema_version="other-receipt-v2",
    ))
    registry.register(EffectHandlerRegistration(
        kind=EFFECT_KIND, effect_class=EffectClass.QUERYABLE, handler=handler,
        probe=probe, contract_version=EFFECT_V2, intent_schema_version="media-hands-job-execution/v2",
        receipt_kind=RECEIPT_KIND, receipt_schema_version=RECEIPT_SCHEMA,
    ))
    assert EFFECT_KIND != "job_execution"
