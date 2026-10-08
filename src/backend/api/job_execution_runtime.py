from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from core.effect_log import (
    EFFECT_V2,
    EffectClass,
    EffectHandlerDeferred,
    EffectHandlerRegistration,
    EffectRecoveryRegistration,
    EffectState,
)
from core.effect_log.runtime import EffectLeaseCheckpoint
_LEGACY_JOB_EXECUTION_REASON = "job_execution.legacy_readonly"


def register_job_execution_handler(application, runtime_root: Path, effect_runtime) -> None:
    """Register immutable legacy history as an execution quarantine.

    Executable domains register their own Effect-v2 Handler and probe below.
    The generic ``job_execution`` kind cannot derive execution or recovery
    truth from a Job projection.
    """

    root = Path(runtime_root)
    _migrate_legacy_job_history(root)

    def handle(effect) -> str:
        del effect
        raise RuntimeError(_LEGACY_JOB_EXECUTION_REASON)

    def probe(effect):
        del effect
        return EffectState.UNKNOWN, _LEGACY_JOB_EXECUTION_REASON

    effect_runtime.handlers.register(
        EffectHandlerRegistration(
            kind="job_execution",
            effect_class=EffectClass.QUERYABLE,
            handler=handle,
            probe=probe,
        )
    )
    effect_runtime.recoveries.register(
        EffectRecoveryRegistration(
            kind="job_execution",
            effect_class=EffectClass.QUERYABLE,
            probe=probe,
            verify=probe,
        )
    )
    register_candidate_v2_job_execution_handler(
        application, root, effect_runtime,
    )
    register_media_hands_v2_job_execution_handler(
        application, root, effect_runtime,
    )
    register_index_rebuild_v2_job_execution_handler(
        application, root, effect_runtime,
    )
    register_workbench_content_transform_v2_handler(
        application, root, effect_runtime,
    )
    register_bilibili_postprocess_v2_handler(
        application, root, effect_runtime,
    )
    from backend.api.bilibili_favorite_batch_runtime import (
        register_bilibili_favorite_batch_v2_handler,
    )
    register_bilibili_favorite_batch_v2_handler(
        application, root, effect_runtime,
    )
    from backend.api.memory_projection_effect_runtime import (
        register_memory_projection_rebuild_effect_runtime,
    )

    register_memory_projection_rebuild_effect_runtime(root, effect_runtime)


def _migrate_legacy_job_history(runtime_root: Path) -> None:
    """Run the explicit, non-executable Job history upgrade at app startup."""

    from backend.api.job_runtime import build_rebuild_job_repository
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store

    object_store, _settings = build_rebuild_object_store(runtime_root)
    repository = build_rebuild_job_repository(runtime_root, object_store)
    imported_at = datetime.now(timezone.utc).isoformat()
    repository.sqlite.import_legacy_job_store_history(
        migration_id="sqlite-job-store-history-v1",
        imported_at=imported_at,
    )
    repository.import_legacy_history(
        migration_id="object-store-job-history-v1",
        imported_at=imported_at,
    )


def register_candidate_v2_job_execution_handler(
    application, runtime_root: Path, effect_runtime,
) -> None:
    """Register the proposal-only v2 candidate domain behind Core Runner.

    This handler never composes ``SQLiteJobRuntimeLifecycle``. The mapped Job
    remains a read model while the domain Effect, Receipt and Core lease own
    execution and recovery.
    """

    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from core.product_core.candidate_effect_contract import (
        EFFECT_KIND,
        INTENT_SCHEMA,
        RECEIPT_KIND,
        RECEIPT_SCHEMA,
    )
    from core.product_core.candidate_effect_execution import (
        CandidateEffectExecutionHandler,
        CandidateEffectExecutionProbe,
    )
    from core.product_core.source_output_memory_candidate import (
        CreateMemoryCandidateFromSourceOutput,
    )

    del application
    root = Path(runtime_root)
    object_store, settings = build_rebuild_object_store(root)
    creator = CreateMemoryCandidateFromSourceOutput(
        object_store, namespace_id=settings.namespace_id,
    )
    handler = CandidateEffectExecutionHandler(effect_runtime.log.database, creator)
    probe = CandidateEffectExecutionProbe(effect_runtime.log.database, creator)
    effect_runtime.handlers.register(
        EffectHandlerRegistration(
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            handler=handler,
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


def register_media_hands_v2_job_execution_handler(
    application, runtime_root: Path, effect_runtime,
) -> None:
    """Register Media transport in the application's single Core Runtime.

    Resolution remains lazy so app startup never creates a second scheduler or
    legacy Job Worker.  An unavailable Provider releases a PLANNED Effect; a
    frozen Provider mismatch is handled by the domain Probe as UNKNOWN.
    """

    from backend.api.job_runtime import build_rebuild_job_repository
    from backend.api.media_hands_runtime import configure_media_hands_runtime
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from core.media_hands.effect_contract import (
        EFFECT_KIND,
        INTENT_SCHEMA,
        RECEIPT_KIND,
        RECEIPT_SCHEMA,
    )
    from core.media_hands.effect_execution import (
        MediaHandsEffectExecutionHandler,
        MediaHandsEffectExecutionProbe,
        MediaHandsExecutionAuthorityDrift,
    )
    from core.media_hands.policy_authority import MediaHandsPolicyAuthorityError
    from core.media_hands.policy_source import (
        MediaHandsPolicySourceError,
        load_media_hands_policy,
    )
    from backend.api.media_ingress_selection_authority import MediaIngressSelectionError

    root = Path(runtime_root)

    def resolve_domain_runtime():
        object_store, _settings = build_rebuild_object_store(root)
        resolution = configure_media_hands_runtime(
            application,
            runtime_root=root,
            object_store=object_store,
            repository=build_rebuild_job_repository(root, object_store),
        )
        return resolution.runtime

    def assert_live_authority(effect, job: Mapping[str, object], runtime) -> None:
        media = job.get("media_hands")
        if not isinstance(media, Mapping):
            raise MediaHandsExecutionAuthorityDrift(
                "Media Hands immutable authority evidence is unavailable"
            )
        policy_fact = media.get("policy")
        selection_fact = media.get("selection")
        if not isinstance(policy_fact, Mapping) or not isinstance(selection_fact, Mapping):
            raise MediaHandsExecutionAuthorityDrift(
                "Media Hands immutable authority evidence is unavailable"
            )

        expected_policy_revision = effect.rev_set.get("policy")
        try:
            if runtime.policy_authority is None:
                current_policy = runtime.policy
            else:
                current_policy_record = runtime.policy_authority.current()
                if current_policy_record is None:
                    raise MediaHandsExecutionAuthorityDrift(
                        "Media Hands live policy is unavailable"
                    )
                current_policy = load_media_hands_policy(current_policy_record.snapshot)
        except (MediaHandsPolicyAuthorityError, MediaHandsPolicySourceError) as error:
            raise MediaHandsExecutionAuthorityDrift(
                "Media Hands live policy drifted"
            ) from error
        if (
            not isinstance(expected_policy_revision, str)
            or current_policy.revision != expected_policy_revision
            or policy_fact.get("revision") != expected_policy_revision
        ):
            raise MediaHandsExecutionAuthorityDrift(
                "Media Hands live policy revision drifted"
            )

        try:
            current_selection = runtime.selection_authority.require_hands()
        except MediaIngressSelectionError as error:
            raise MediaHandsExecutionAuthorityDrift(
                "Media Hands ingress selection is no longer active"
            ) from error
        expected_selection_revision = f"media-ingress-r{current_selection.revision}"
        frozen_boundary = effect.rev_set.get("boundary")
        frozen_selection_revision = (
            frozen_boundary.split("@", 1)[0]
            if isinstance(frozen_boundary, str)
            else None
        )
        if (
            frozen_selection_revision != expected_selection_revision
            or dict(selection_fact) != {
                "ref": current_selection.public_ref,
                "revision": expected_selection_revision,
                "mode": "hands",
            }
        ):
            raise MediaHandsExecutionAuthorityDrift(
                "Media Hands ingress selection revision drifted"
            )

    def handle(effect):
        runtime = resolve_domain_runtime()
        if runtime is None:
            raise EffectHandlerDeferred("media_hands.effect_runtime_unavailable")
        object_store, settings = build_rebuild_object_store(root)
        from backend.api.bilibili_media_postprocess_runtime import (
            BilibiliReceiptPostprocessAdmission,
        )

        postprocess_admission = BilibiliReceiptPostprocessAdmission(
            effect_runtime.log.database, object_store, settings.namespace_id,
        )
        return MediaHandsEffectExecutionHandler(
            effect_runtime.log.database,
            runtime.handler,
            lambda current_effect, job: assert_live_authority(
                current_effect, job, runtime,
            ),
            execution_checkpoint_factory=lambda current_effect: (
                EffectLeaseCheckpoint.for_claim(effect_runtime, current_effect).checkpoint
            ),
            receipt_committer=postprocess_admission,
        ).handle(effect)

    def probe(effect):
        runtime = resolve_domain_runtime()
        if runtime is None:
            return (
                EffectState.PLANNED,
                f"facts:media-hands-runtime-unavailable/{effect.operation_id}",
            )
        return MediaHandsEffectExecutionProbe(
            effect_runtime.log.database,
            runtime.handler,
            lambda current_effect, job: assert_live_authority(
                current_effect, job, runtime,
            ),
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


def register_index_rebuild_v2_job_execution_handler(
    application, runtime_root: Path, effect_runtime,
) -> None:
    """Register Index rebuild Effect-v2 with the application's Core runtime."""

    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.api.library_query_runtime import load_current_recall_entries
    from core.product_core.index_rebuild_effect_adapter import (
        IndexRebuildEffectAdapter,
    )
    from core.product_core.index_rebuild_effect_admission import (
        EFFECT_KIND,
        INTENT_SCHEMA,
        RECEIPT_KIND,
        RECEIPT_SCHEMA,
    )

    del application
    root = Path(runtime_root)
    object_store, _settings = build_rebuild_object_store(root)
    adapter = IndexRebuildEffectAdapter(
        object_store,
        root,
        lambda: tuple(load_current_recall_entries(root, object_store)),
    )
    handler = adapter.handler(effect_runtime.log.database)
    probe = adapter.probe(effect_runtime.log.database)
    effect_runtime.handlers.register(
        EffectHandlerRegistration(
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            handler=handler,
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


def register_workbench_content_transform_v2_handler(
    application, runtime_root: Path, effect_runtime,
) -> None:
    """Register local document/video transforms in the single Core Reaper."""

    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.api.workbench_content_transform_runtime import (
        build_workbench_content_transform_domain,
    )
    from core.product_core.workbench_content_transform_effect_contract import (
        EFFECT_KIND,
        INTENT_SCHEMA,
        RECEIPT_KIND,
        RECEIPT_SCHEMA,
    )
    from core.product_core.workbench_content_transform_execution import (
        WorkbenchContentTransformEffectHandler,
        WorkbenchContentTransformEffectProbe,
    )

    del application
    root = Path(runtime_root)
    object_store, settings = build_rebuild_object_store(root)
    domain = build_workbench_content_transform_domain(
        root, object_store, settings.namespace_id,
    )
    handler = WorkbenchContentTransformEffectHandler(
        effect_runtime.log.database,
        domain.execute_item,
        domain.verify_output,
        lambda effect: EffectLeaseCheckpoint.for_claim(effect_runtime, effect).checkpoint,
    )
    probe = WorkbenchContentTransformEffectProbe(
        effect_runtime.log.database, domain.verify_output,
    )
    effect_runtime.handlers.register(
        EffectHandlerRegistration(
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            handler=handler,
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


def register_bilibili_postprocess_v2_handler(
    application, runtime_root: Path, effect_runtime,
) -> None:
    """Register the receipt-bound local Bilibili summary/candidate child Job."""

    from backend.api.bilibili_media_postprocess_runtime import (
        build_bilibili_postprocess_domain,
    )
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from core.product_core.bilibili_media_postprocess import (
        EFFECT_KIND,
        INTENT_SCHEMA,
        RECEIPT_KIND,
        RECEIPT_SCHEMA,
        BilibiliPostprocessEffectHandler,
        BilibiliPostprocessEffectProbe,
    )

    del application
    root = Path(runtime_root)
    object_store, settings = build_rebuild_object_store(root)
    domain = build_bilibili_postprocess_domain(root, object_store, settings.namespace_id)
    handler = BilibiliPostprocessEffectHandler(
        effect_runtime.log.database,
        domain.execute,
        domain.verify,
        lambda effect: EffectLeaseCheckpoint.for_claim(effect_runtime, effect).checkpoint,
    )
    probe = BilibiliPostprocessEffectProbe(effect_runtime.log.database, domain.verify)
    effect_runtime.handlers.register(
        EffectHandlerRegistration(
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            handler=handler,
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
