from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import pytest

from backend.api.context_binding_composition import (
    ContextBindingCompositionRequest,
    ContextBindingCompositionService,
    SQLiteCompilationFactRepository,
)
from backend.api.context_binding_runtime import ContextBindingRegistry
from backend.api.context_graph_snapshot_runtime import ContextGraphSnapshotRepository
from core.context_graph import (
    CapabilityPackageLoader,
    ContextCompiler,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextPermissionGrant,
    ContextProvenance,
    FrozenContextRevisions,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteStructuredRecordUnitOfWork


ROOT = Path(__file__).resolve().parents[4]


def _revisions(**changes: str) -> FrozenContextRevisions:
    values = {
        "capability_revision": "2.5.0", "boundary_revision": "boundary-1",
        "provider_revision": "provider-1", "model_route_revision": "route-1",
        "compiler_revision": "2.0.0",
    }
    values.update(changes)
    return FrozenContextRevisions(**values)


def _snapshot(revision: str = "r1", *, project: str = "project-a", content: str = "Evidence A") -> ContextGraphSnapshot:
    return ContextGraphSnapshot(
        "1.0.0", "graph-a", revision, project, "fixture", revision,
        "2026-08-30T00:00:00Z", (ContextGraphNode(
            "node-a", "note", "Note", "ref://note", revision, ("source://note",), "verified",
            "2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z", metadata={"content": content},
        ),), (), ("node-a",), 1,
        ContextProvenance("fixture", revision, "2026-08-30T00:00:00Z", "test", "1", "ref://source"),
    )


def _service(tmp_path: Path, revisions: FrozenContextRevisions | None = None):
    snapshots = ContextGraphSnapshotRepository(SQLiteStructuredRecordStore(tmp_path / "snapshots.sqlite3"))
    loader = CapabilityPackageLoader()
    manifest = next(item for item in loader.discover(ROOT / "src" / "core" / "capability_packages") if item.capability_id == "thought_graph_context")
    loader.install(manifest)
    current = revisions or _revisions(capability_revision=manifest.capability_revision)
    current = replace(current, capability_revision=manifest.capability_revision)
    service = ContextBindingCompositionService(
        snapshots, ContextBindingRegistry(tmp_path), loader, ContextCompiler(),
        lambda *_: current, lambda: "2026-08-30T12:00:00Z",
        facts=SQLiteCompilationFactRepository(SQLiteStructuredRecordStore(tmp_path / "facts.sqlite3"), lambda: "2026-08-30T12:00:00Z"),
    )
    return service, snapshots, current


def _append(repository, snapshot: ContextGraphSnapshot, predecessor: str | None, *, allowed=("ref://note",)):
    return repository.append(
        snapshot, predecessor, capability_id="thought_graph_context",
        capability_revision="1.0.0",
        permission_grant=ContextPermissionGrant(snapshot.project_id, "permission-1", frozenset(allowed)),
        permission_evidence_refs=("evidence://permission-1",),
    )


def _request(revision="r1", **changes):
    values = dict(project_id="project-a", graph_id="graph-a", graph_revision=revision,
                  binding_id="binding-1", token_budget=500,
                  acknowledge_staleness=False, actor_id="actor-a")
    values.update(changes)
    return ContextBindingCompositionRequest(**values)


def test_first_success_uses_current_baseline_not_import_registration_revisions(tmp_path: Path):
    service, snapshots, _ = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    result = service.create(_request())
    assert result.ok and result.binding is not None
    assert result.binding.capability_revision == _.capability_revision
    assert result.binding.budget_explanation["staleness"]["confirmed_by"] is None


def test_graph_change_requires_ack_before_registry_write_then_records_actor_and_full_preview(tmp_path: Path):
    service, snapshots, _ = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    _append(snapshots, _snapshot("r2", content="Changed evidence"), "r1")
    refused = service.create(_request("r2"))
    assert refused.error and refused.error.code == "staleness_confirmation_required"
    assert refused.preview and refused.preview.affected_node_ids == ("node-a",)
    accepted = service.create(_request("r2", acknowledge_staleness=True))
    assert accepted.ok and accepted.binding
    stale = accepted.binding.budget_explanation["staleness"]
    assert stale["confirmed_by"] == "actor-a"
    assert stale["confirmed_at"] == "2026-08-30T12:00:00Z"
    assert stale["affected_node_ids"] == ("node-a",)
    assert stale["replay_order"] == ("node-a",)


def test_client_cannot_forge_baseline_and_scope_or_permission_fail_closed(tmp_path: Path):
    service, snapshots, _ = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    # Stored snapshot authority is validated on append, so permission denial is
    # exercised through an immutable record whose grant has been corrupted.
    record = snapshots.revision("project-a", "graph-a", "r1")
    assert record is not None
    object.__setattr__(record, "permission_grant", ContextPermissionGrant("project-a", "permission-2", frozenset()))
    original = snapshots.revision
    snapshots.revision = lambda *args, **kwargs: record if (kwargs.get("graph_revision") or args[-1]) == "r1" else original(*args, **kwargs)  # type: ignore[method-assign]
    assert service.create(_request()).error.code == "permission_denied"
    snapshots.revision = original  # type: ignore[method-assign]
    assert service.create(_request(project_id="project-b")).error.code == "graph_revision_unavailable"
    assert service.create(_request(graph_revision="forged-baseline")).error.code == "graph_revision_unavailable"
    assert service.create(_request(allow_remote="yes")).error.code == "invalid_request"  # type: ignore[arg-type]


def test_capability_and_current_revision_drift_fail_before_create(tmp_path: Path):
    service, snapshots, current = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    service._current_revisions = lambda *_: replace(current, capability_revision="9.9.9")
    assert service.create(_request()).error.code == "capability_revision_drift"
    service._current_revisions = lambda *_: replace(current, compiler_revision="3.0.0")
    assert service.create(_request()).error.code == "unsupported_current_compiler"


def test_existing_binding_is_idempotent_and_detects_content_drift(tmp_path: Path):
    service, snapshots, _ = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    first = service.create(_request())
    repeat = service.create(_request())
    assert first.ok and repeat.ok and repeat.idempotent
    service._clock = lambda: "different-but-irrelevant"
    # Different graph content behind a reused binding identity cannot silently replace it.
    _append(snapshots, _snapshot("r2", content="changed"), "r1")
    drift = service.create(_request("r2", acknowledge_staleness=True))
    assert drift.error and drift.error.code == "binding_identity_drift"


def test_provider_or_compiler_fact_drift_is_stale_and_unsupported_current_compiler_fails(tmp_path: Path):
    service, snapshots, initial = _service(tmp_path, _revisions(provider_revision="provider-1"))
    _append(snapshots, _snapshot(), None)
    assert service.create(_request()).ok
    service._current_revisions = lambda *_: replace(initial, provider_revision="provider-2")
    stale = service.create(_request(binding_id="binding-2"))
    assert stale.error and stale.error.code == "staleness_confirmation_required"
    assert stale.preview and stale.preview.stale_reasons == (("node-a", "revision_drift:provider_revision"),)
    accepted = service.create(_request(binding_id="binding-2", acknowledge_staleness=True))
    assert accepted.ok
    # The immutable fact authority advances per successful binding, so another
    # current baseline drift is still previewed rather than silently accepted.
    service._current_revisions = lambda *_: replace(initial, provider_revision="provider-3")
    assert service.create(_request(binding_id="binding-3")).error.code == "staleness_confirmation_required"
    service._current_revisions = lambda *_: replace(initial, compiler_revision="9.0.0")
    assert service.create(_request(binding_id="binding-3")).error.code == "unsupported_current_compiler"


def test_sqlite_fact_restart_persists_acknowledged_drift_and_tamper_fails_closed(tmp_path: Path):
    service, snapshots, current = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    assert service.create(_request()).ok
    restarted = SQLiteCompilationFactRepository(SQLiteStructuredRecordStore(tmp_path / "facts.sqlite3"), lambda: "2026-08-30T13:00:00Z")
    assert restarted.revision("project-a", "graph-a", "r1") == current
    assert restarted.binding_fact("project-a", "graph-a", "r1", "binding-1", 1, current)
    service._facts = restarted
    service._current_revisions = lambda *_: replace(current, provider_revision="provider-2")
    assert service.create(_request(binding_id="binding-2")).error.code == "staleness_confirmation_required"
    assert service.create(_request(binding_id="binding-2", acknowledge_staleness=True)).ok
    after_restart = SQLiteCompilationFactRepository(SQLiteStructuredRecordStore(tmp_path / "facts.sqlite3"), lambda: "now")
    assert after_restart.revision("project-a", "graph-a", "r1").provider_revision == "provider-2"
    assert after_restart.binding_fact("project-a", "graph-a", "r1", "binding-1", 1, current)
    assert after_restart.binding_fact("project-a", "graph-a", "r1", "binding-2", 1, replace(current, provider_revision="provider-2"))
    records = SQLiteStructuredRecordStore(tmp_path / "facts.sqlite3")
    fact = records.list("context_compilation_facts")[-1]
    with records.begin() as uow:
        uow.put("context_compilation_facts", fact.object_id, {"schema_version": "9.0.0"}, expected_revision=fact.revision)
        uow.commit()
    with pytest.raises(ValueError, match="compilation_fact_payload_invalid"):
        restarted.revision("project-a", "graph-a", "r1")


def test_sqlite_fact_transaction_failure_never_advances_head(tmp_path: Path, monkeypatch):
    facts = SQLiteCompilationFactRepository(SQLiteStructuredRecordStore(tmp_path / "facts.sqlite3"), lambda: "now")
    facts.record("project-a", "graph-a", "r1", _revisions(), "binding-1", 1)
    original = SQLiteStructuredRecordUnitOfWork.put
    def fail_head(self, collection, *args, **kwargs):
        if collection == "context_compilation_fact_heads":
            raise RuntimeError("injected head failure")
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_head)
    with pytest.raises(RuntimeError, match="injected"):
        facts.record("project-a", "graph-a", "r1", replace(_revisions(), provider_revision="provider-2"), "binding-2", 2)
    assert facts.revision("project-a", "graph-a", "r1").provider_revision == "provider-1"


def test_sqlite_fact_head_to_fact_identity_tamper_fails_closed(tmp_path: Path):
    records = SQLiteStructuredRecordStore(tmp_path / "facts.sqlite3")
    facts = SQLiteCompilationFactRepository(records, lambda: "now")
    facts.record("project-a", "graph-a", "r1", _revisions(), "binding-1", 1)
    head = records.list("context_compilation_fact_heads")[0]
    tampered = dict(head.payload)
    tampered["registry_revision"] = 2
    with records.begin() as uow:
        uow.put("context_compilation_fact_heads", head.object_id, tampered, expected_revision=head.revision)
        uow.commit()
    with pytest.raises(ValueError, match="compilation_fact_head_identity_drift"):
        facts.revision("project-a", "graph-a", "r1")
    with pytest.raises(ValueError, match="compilation_fact_head_identity_drift"):
        facts.latest("project-a", "graph-a")


def test_registry_binding_without_committed_fact_has_no_membership(tmp_path: Path, monkeypatch):
    service, snapshots, current = _service(tmp_path)
    _append(snapshots, _snapshot(), None)
    original = SQLiteStructuredRecordUnitOfWork.put
    def fail_head(self, collection, *args, **kwargs):
        if collection == "context_compilation_fact_heads":
            raise RuntimeError("injected fact failure")
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", fail_head)
    failed = service.create(_request())
    assert failed.error and failed.error.code == "composition_invalid"
    binding = service._registry.resolve("crp://context-bindings/project-a/binding-1", project_id="project-a")
    assert binding.binding == failed.binding or binding.binding_id == "binding-1"
    assert not service._facts.binding_fact("project-a", "graph-a", "r1", "binding-1", binding.registry_revision, current)
