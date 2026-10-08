from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.shared_trust_audit_activation_startup import (
    backfill_shared_trust_activation_effects,
    dispatch_shared_trust_activation_effects,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner
from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME
from core.shared_trust_audit_activation_service import SharedTrustAuditActivationSagaService
from core.storage_provider import (
    AggregateAuthorityEvidence,
    AggregateAuthorityTransition,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
    SharedTrustAuditActivationEvidence,
)


ROOT = Path(__file__).resolve().parents[2]
MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)
TARGET = "sqlite:structured-records-v1"


def recover_shared_trust_audit_activation_sagas(application, root, *, max_operations=100):
    (root / ".rebuild-data").mkdir(parents=True, exist_ok=True)
    effects = EffectLog(root / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    backfill_shared_trust_activation_effects(root, effects, max_operations=max_operations)
    EffectReaper(effects).recover_expired(now=2**31)
    return dispatch_shared_trust_activation_effects(
        application, root, EffectRunner(effects, owner_id="test-shared-trust"),
        max_operations=max_operations,
    )


def _evidence(activation_id: str = "activation-startup-v1", *, namespace_id: str = "default"):
    return SharedTrustAuditActivationEvidence(
        namespace_id=namespace_id,
        activation_id=activation_id,
        member_migrations={member: f"{member}-v1" for member in MEMBERS},
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
    )


def _setup(root: Path, evidence: SharedTrustAuditActivationEvidence | None = None):
    evidence = evidence or _evidence()
    rebuild_root = root / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    with records.begin() as uow:
        for member in MEMBERS:
            uow.put(
                "aggregate_authority_targets",
                f"{evidence.namespace_id}~{member}",
                {
                    "namespace_id": evidence.namespace_id,
                    "aggregate": member,
                    "migration_id": evidence.member_migrations[member],
                    "source_fingerprint": evidence.source_fingerprint,
                    "target_fingerprint": evidence.target_fingerprint,
                    "target_identity": TARGET,
                },
                expected_revision=0,
            )
        uow.commit()
    for member in MEMBERS:
        initial = authority.create_json_active(
            namespace_id=evidence.namespace_id, aggregate=member, reason="test setup"
        )
        authority.transition(
            namespace_id=evidence.namespace_id,
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            reason="test setup",
            evidence=AggregateAuthorityEvidence(
                evidence.member_migrations[member],
                evidence.source_fingerprint,
                evidence.target_fingerprint,
                TARGET,
            ),
        )
    operations = SQLiteSharedTrustAuditActivationSagaStore(records)
    return records, authority, operations, evidence


def _service(records, authority, operations):
    return SharedTrustAuditActivationSagaService(
        operations=operations, records=records, authority=authority
    )


def test_prepared_activation_recovers_once_and_is_revision_idempotent(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    prepared = operations.prepare(evidence)

    first = recover_shared_trust_audit_activation_sagas(FastAPI(), tmp_path)
    second = recover_shared_trust_audit_activation_sagas(FastAPI(), tmp_path)

    assert (first.scanned, first.recovered, first.failed) == (1, 1, 0)
    assert (second.scanned, second.attempted) == (0, 0)
    assert operations.get(prepared.operation_id).state == "finalized"
    assert [authority.get("default", member).revision for member in MEMBERS] == [3] * 6


def test_attested_or_authority_activated_operation_finishes_on_startup(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    service = _service(records, authority, operations)
    prepared = operations.prepare(evidence, now="2026-07-12T00:00:00Z")
    service._write_attestation(prepared)
    attested = operations.advance(
        prepared.operation_id, prepared.revision, "attestation_written"
    )

    first = recover_shared_trust_audit_activation_sagas(FastAPI(), tmp_path)
    assert first.recovered == 1
    assert operations.get(attested.operation_id).state == "finalized"

    records, authority, operations, evidence = _setup(tmp_path / "authority-activated")
    service = _service(records, authority, operations)
    prepared = operations.prepare(evidence)
    service._write_attestation(prepared)
    attested = operations.advance(
        prepared.operation_id, prepared.revision, "attestation_written"
    )
    staged = tuple(authority.get("default", member) for member in MEMBERS)
    authority.transition_many(
        tuple(
            AggregateAuthorityTransition(
                "default", record.aggregate, record.revision, "sqlite_active", "test", record.evidence
            )
            for record in staged
        )
    )
    operations.advance(attested.operation_id, attested.revision, "authorities_activated")

    report = recover_shared_trust_audit_activation_sagas(FastAPI(), tmp_path / "authority-activated")
    assert report.recovered == 1
    assert operations.get(prepared.operation_id).state == "finalized"
    assert [authority.get("default", member).revision for member in MEMBERS] == [3] * 6


def test_bad_operation_isolated_missing_authority_and_batch_are_bounded(tmp_path: Path) -> None:
    records, authority, operations, evidence = _setup(tmp_path)
    good = operations.prepare(evidence)
    bad = operations.prepare(_evidence("activation-bad", namespace_id="other"))

    report = recover_shared_trust_audit_activation_sagas(FastAPI(), tmp_path, max_operations=2)
    assert (report.scanned, report.attempted, report.deferred) == (2, 2, 0)
    assert {item.outcome for item in report.items} == {
        "recovered",
        "failed",
    }
    assert operations.get(good.operation_id).state == "finalized"
    assert operations.get(bad.operation_id).state == "prepared"

    missing_root = tmp_path / "missing-authority"
    missing_records = SQLiteStructuredRecordStore(
        missing_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    missing_operations = SQLiteSharedTrustAuditActivationSagaStore(missing_records)
    missing = missing_operations.prepare(_evidence("activation-missing"))
    report = recover_shared_trust_audit_activation_sagas(FastAPI(), missing_root)
    assert report.items[0].error_code == "authority_store_missing"
    assert missing_operations.get(missing.operation_id).state == "prepared"
    assert not (missing_root / ".rebuild-data" / AUTHORITY_DATABASE_NAME).exists()


def test_existing_fastapi_lifespan_recovers_after_controlled_child_process_exit(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path
        from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME
        from core.storage_provider import AggregateAuthorityEvidence, SQLiteAggregateAuthorityStore, SQLiteSharedTrustAuditActivationSagaStore, SQLiteStructuredRecordStore, SharedTrustAuditActivationEvidence
        root = Path(sys.argv[1]); members = ('memory_atoms', 'memory_publications', 'memory_scenarios', 'memory_series_memory', 'memory_transitions', 'project_skills')
        evidence = SharedTrustAuditActivationEvidence('default', 'activation-process', {member: f'{member}-v1' for member in members}, 'a' * 64, 'b' * 64)
        records = SQLiteStructuredRecordStore(root / '.rebuild-data' / STRUCTURED_DATABASE_NAME)
        authority = SQLiteAggregateAuthorityStore(root / '.rebuild-data' / AUTHORITY_DATABASE_NAME)
        with records.begin() as uow:
            for member in members:
                uow.put('aggregate_authority_targets', f'default~{member}', {'namespace_id': 'default', 'aggregate': member, 'migration_id': evidence.member_migrations[member], 'source_fingerprint': evidence.source_fingerprint, 'target_fingerprint': evidence.target_fingerprint, 'target_identity': evidence.target_identity}, expected_revision=0)
            uow.commit()
        for member in members:
            initial = authority.create_json_active(namespace_id='default', aggregate=member, reason='process')
            authority.transition(namespace_id='default', aggregate=member, expected_revision=initial.revision, to_state='sqlite_staged', reason='process', evidence=AggregateAuthorityEvidence(evidence.member_migrations[member], evidence.source_fingerprint, evidence.target_fingerprint, evidence.target_identity))
        SQLiteSharedTrustAuditActivationSagaStore(records).prepare(evidence)
        os._exit(97)
        """
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    crashed = subprocess.run(
        [str(ROOT / "runtime" / "python.exe"), "-c", script, str(tmp_path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 97, (crashed.stdout, crashed.stderr)

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert "shared_trust_activation" in client.app.state.effect_runtime.handlers.kinds()
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    operation = SQLiteSharedTrustAuditActivationSagaStore(records).list_recoverable()
    assert operation == ()
