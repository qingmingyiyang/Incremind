"""Production facade for the Memory Projection rebuild Effect-v2.

The developer command owns admission.  Core Effect Runtime owns execution,
leases, settlement, and restart recovery.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.security.automation_grants import (
    AutomationGrantBinding,
    AutomationGrantRepository,
    canonical_parameter_digest,
)
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.effect_log import (
    EFFECT_V2,
    EffectClass,
    EffectHandlerRegistration,
    EffectRecoveryRegistration,
)
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.memory_projection_authority import (
    CurrentMemoryProjectionAuthority,
)
from core.product_core.memory_projection_rebuild_effect_admission import (
    EFFECT_KIND,
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
    MemoryProjectionRebuildEffectAdmissionFactory,
    SQLiteMemoryProjectionRebuildEffectAdmission,
)
from core.product_core.memory_projection_rebuild_effect_execution import (
    MemoryProjectionRebuildEffectExecutionHandler,
    MemoryProjectionRebuildEffectExecutionProbe,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)


def register_memory_projection_rebuild_effect_runtime(
    runtime_root: Path, effect_runtime,
) -> None:
    """Register the domain Handler and Probe in the application's Core runtime."""

    root = Path(runtime_root)

    def domain_dependencies():
        return _domain_dependencies(root)

    def handle(effect):
        authority, projections = domain_dependencies()
        return MemoryProjectionRebuildEffectExecutionHandler(
            effect_runtime.log.database, authority, projections,
        ).handle(effect)

    def probe(effect):
        authority, projections = domain_dependencies()
        return MemoryProjectionRebuildEffectExecutionProbe(
            effect_runtime.log.database, authority, projections,
        ).probe(effect)

    effect_runtime.handlers.register(
        EffectHandlerRegistration(
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            handler=handle,
            probe=probe,
            contract_version=EFFECT_V2,
            intent_schema_version=INTENT_SCHEMA,
            receipt_kind=RECEIPT_KIND,
            receipt_schema_version=RECEIPT_SCHEMA,
        )
    )
    effect_runtime.recoveries.register(
        EffectRecoveryRegistration(
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            probe=probe,
            verify=probe,
            contract_version=EFFECT_V2,
        )
    )


def admit_memory_projection_rebuild(
    effect_runtime,
    authority: object,
    *,
    project_id: str,
    admitted_at: int,
    repair: bool = False,
    expected_authority_fingerprint: str | None = None,
):
    """Persist v2 Gate, Intent, Effect and request in one caller transaction."""

    snapshot = _load_authority_snapshot(authority, project_id=project_id)
    admission = MemoryProjectionRebuildEffectAdmissionFactory(
        admitted_at=admitted_at,
    ).build(snapshot)
    if (
        expected_authority_fingerprint is not None
        and admission.request["authority_fingerprint"]
        != expected_authority_fingerprint
    ):
        raise ValueError("memory projection authority changed before admission")
    if repair:
        admission = _repair_admission(admission)
    with sqlite3.connect(effect_runtime.log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            effect, created = SQLiteMemoryProjectionRebuildEffectAdmission(
                effect_runtime.log,
            ).admit_in_connection(connection, admission)
        except Exception:
            connection.rollback()
            raise
        connection.commit()
    return effect, created


def memory_projection_rebuild_automation_binding(
    authority: object,
    *,
    project_id: str,
    admitted_at: int,
    repair: bool = False,
    expected_authority_fingerprint: str | None = None,
) -> AutomationGrantBinding:
    """Derive an exact grant binding from the same frozen Effect-v2 intent."""

    snapshot = _load_authority_snapshot(authority, project_id=project_id)
    binding, _fingerprint = _automation_binding_from_snapshot(
        snapshot,
        admitted_at=admitted_at,
        repair=repair,
        expected_authority_fingerprint=expected_authority_fingerprint,
    )
    return binding


def preview_memory_projection_rebuild_automation(
    authority: object,
    *,
    project_id: str,
    admitted_at: int,
    repair: bool = False,
) -> tuple[AutomationGrantBinding, str]:
    """Derive one displayable automation binding without creating a Grant.

    A preview reads the same authority snapshot as admission but neither opens
    the Grant repository nor plans an Effect.  Cancelling the UI therefore
    leaves no approval or execution state behind.
    """

    snapshot = _load_authority_snapshot(authority, project_id=project_id)
    return _automation_binding_from_snapshot(
        snapshot,
        admitted_at=admitted_at,
        repair=repair,
        expected_authority_fingerprint=None,
    )


def _automation_binding_from_snapshot(
    snapshot: MemoryProjectionAuthoritySnapshot,
    *,
    admitted_at: int,
    repair: bool,
    expected_authority_fingerprint: str | None,
) -> tuple[AutomationGrantBinding, str]:
    admission = MemoryProjectionRebuildEffectAdmissionFactory(
        admitted_at=admitted_at,
    ).build(snapshot)
    if repair:
        admission = _repair_admission(admission)
    fingerprint = str(admission.request["authority_fingerprint"])
    if expected_authority_fingerprint is not None and fingerprint != expected_authority_fingerprint:
        raise ValueError("memory projection authority changed before grant binding")
    return AutomationGrantBinding(
        project_id=snapshot.project_id,
        operation_id=admission.intent.operation_id,
        parameter_digest=canonical_parameter_digest({
            "project_id": snapshot.project_id,
            "repair": repair,
            "authority_fingerprint": fingerprint,
        }),
        effect_kind=EFFECT_KIND,
        capability_revision=2,
        target=snapshot.project_id,
        boundary_profile_id=str(admission.intent.rev_set["boundary"]),
        boundary_revision=2,
    ), fingerprint


def execute_granted_memory_projection_rebuild(
    effect_runtime,
    authority: object,
    grants: AutomationGrantRepository,
    *,
    project_id: str,
    admitted_at: int,
    grant_id: str,
    expected_grant_revision: int,
    repair: bool = False,
    expected_authority_fingerprint: str | None = None,
):
    """Claim the exact grant before admission, then bind its terminal Receipt.

    The grant database is intentionally separate from the Effect database.  A
    claim is therefore consumed before Effect admission; any later failure is
    fail-closed and cannot make the authorization reusable.
    """

    binding = memory_projection_rebuild_automation_binding(
        authority,
        project_id=project_id,
        admitted_at=admitted_at,
        repair=repair,
        expected_authority_fingerprint=expected_authority_fingerprint,
    )
    claim = grants.claim(
        grant_id=grant_id,
        binding=binding,
        expected_revision=expected_grant_revision,
    )
    effect, _created = admit_memory_projection_rebuild(
        effect_runtime,
        authority,
        project_id=project_id,
        admitted_at=admitted_at,
        repair=repair,
        expected_authority_fingerprint=expected_authority_fingerprint,
    )
    if effect.operation_id != binding.operation_id:
        raise ValueError("automation grant operation identity drifted")
    try:
        settled = effect_runtime.dispatch_operation(effect.operation_id, now=admitted_at)
    except Exception:
        # The admitted Effect is now the only execution/recovery authority.
        # Do not retry, re-claim, or hide its identity merely because this
        # synchronous dispatch pass did not reach a terminal state.
        settled = effect_runtime.log.get(effect.operation_id)
    if settled.state.value != "SETTLED_OK" or not isinstance(settled.result_ref, str):
        return settled, claim.grant
    completed = grants.complete(claim_id=claim.claim_id, receipt_ref=settled.result_ref)
    return settled, completed


def _load_authority_snapshot(
    authority: object,
    *,
    project_id: str,
) -> MemoryProjectionAuthoritySnapshot:
    load = getattr(authority, "load", None)
    if not callable(load):
        raise TypeError("memory projection authority loader is unavailable")
    snapshot = load(project_id)
    if not isinstance(snapshot, MemoryProjectionAuthoritySnapshot):
        raise TypeError("memory projection authority returned an invalid snapshot")
    if snapshot.project_id != project_id:
        raise ValueError("memory projection authority project identity drifted")
    return snapshot


def memory_projection_rebuild_authority(runtime_root: Path):
    """Expose the existing production authority for a narrow route adapter."""

    return _domain_dependencies(Path(runtime_root))[0]


def _repair_admission(admission):
    """Give a confirmed corrupt-artifact repair its own stable Effect root.

    A settled refresh Receipt cannot safely be reused after its derived artifact
    has been discarded.  The repair identity remains deterministic for the
    exact frozen authority and cannot alter the authority or policy evidence.
    """

    original = admission.request["request_id"]
    request_id = "mpr_repair_" + sha256(
        f"{original}\\nrepair-v2".encode("utf-8"),
    ).hexdigest()[:48]
    request_ref = f"facts:memory-projection-rebuild/request/{request_id}"
    request = MappingProxyType({**admission.request, "request_id": request_id, "request_ref": request_ref})
    authorization = replace(
        admission.authorization,
        budget_after={**admission.authorization.budget_after, "request_ref": request_ref},
    )
    intent = replace(
        admission.intent,
        root_id=request_id,
        intent_ref=f"intent:memory-projection-rebuild/{request_id}",
        gate_decision_id=f"gate:memory-projection-rebuild/{request_id}",
        payload={**admission.intent.payload, "request_ref": request_ref},
    )
    return replace(
        admission,
        authorization=authorization,
        gate_decision_id=intent.gate_decision_id,
        intent=intent,
        request=request,
    )


def _domain_dependencies(
    runtime_root: Path,
) -> tuple[CurrentMemoryProjectionAuthority, ObjectStoreMemoryProjectionRepository]:
    store, settings = build_rebuild_object_store(runtime_root)
    factory = AggregateRepositoryFactory(
        runtime_root=runtime_root,
        namespace_id=settings.namespace_id,
        json_store=store,
    )
    memory_resolution = factory.memory_publication_authority_resolution()
    skill_resolution = factory.project_skill_repository_resolution()
    memory = (
        SQLiteMemoryReader(memory_resolution.records)
        if memory_resolution.records is not None
        else ObjectStoreMemoryStore(store)
    )
    return (
        CurrentMemoryProjectionAuthority(
            memory=memory,
            project_skills=skill_resolution.repository,
            memory_authority_identity=memory_resolution.authority_identity,
            project_skill_authority_identity=skill_resolution.authority_identity,
        ),
        ObjectStoreMemoryProjectionRepository(store),
    )
