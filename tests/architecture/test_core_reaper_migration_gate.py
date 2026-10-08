from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _create_app_source() -> str:
    path = ROOT / "src/backend/api/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    return ast.unparse(function)


def test_production_has_one_external_recovery_startup_scheduler() -> None:
    source = _create_app_source()
    assert source.count("add_event_handler('startup', recover_external_effects)") == 1
    for forbidden in (
        "add_event_handler('startup', recover_plugin_hands)",
        "add_event_handler('startup', recover_rebuild_jobs)",
        "add_event_handler('startup', scan_due_ai_turns)",
        "add_event_handler('startup', resume_safe_ai_turns)",
        "add_event_handler('startup', recover_prepared_document_deliveries)",
    ):
        assert forbidden not in source


def test_legacy_recovery_bridge_is_explicit_and_must_reach_zero_for_phase_exit() -> None:
    source = _create_app_source()
    assert "register_legacy" not in source
    runtime = (ROOT / "src/core/effect_log/runtime.py").read_text(encoding="utf-8")
    exports = (ROOT / "src/core/effect_log/__init__.py").read_text(encoding="utf-8")
    assert "LegacyRecovery" not in runtime
    assert "register_legacy" not in runtime
    assert "LegacyRecovery" not in exports


def test_library_write_route_cannot_drain_publication_outbox_directly() -> None:
    route = (ROOT / "src/backend/api/routes/library_lifecycle.py").read_text(encoding="utf-8")
    startup = (
        ROOT / "src/backend/api/external_agent_publication_change_startup.py"
    ).read_text(encoding="utf-8")
    assert "recover_external_agent_publication_changes" not in route
    assert "def recover_external_agent_publication_changes" not in startup


def test_plugin_hands_construction_cannot_run_domain_effect_recovery() -> None:
    runtime = (ROOT / "src/backend/api/plugin_hands_runtime.py").read_text(encoding="utf-8")
    lifecycle = (
        ROOT / "src/core/plugin_hands/durable_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert ".reconcile_lazy(" not in runtime
    assert ".cleanup_only_lazy(" not in runtime
    assert "recover_plugin_hands_runtime" not in runtime
    assert "self._recover_effect_from_retained_fact" not in lifecycle
    assert "self._mark_effect_unknown" not in lifecycle
    assert "backfill_plugin_hands_execution_effects" in lifecycle
    assert "cleanup_plugin_hands_workspaces" not in _create_app_source()


def test_job_execution_has_no_domain_recovery_scheduler_or_direct_production_entry() -> None:
    lifecycle = (
        ROOT / "src/core/job_runner/sqlite_lifecycle.py"
    ).read_text(encoding="utf-8")
    handler = (
        ROOT / "src/backend/api/job_execution_runtime.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "def schedule_ready", "def recover_pending", "def _schedule",
        "def _run_after_delay", "contention_retry_seconds",
    ):
        assert forbidden not in lifecycle
    assert "execution_dispatcher" in lifecycle
    # Legacy Media is a read-only history projection.  It must resolve through
    # the generic QUERYABLE probe as UNKNOWN, never compose or enter the old
    # Job lifecycle.  New Media is registered separately as an Effect-v2
    # handler and dispatched by the Core recovery coordinator.
    assert '_LEGACY_JOB_EXECUTION_REASON = "job_execution.legacy_readonly"' in handler
    assert "return EffectState.UNKNOWN, _LEGACY_JOB_EXECUTION_REASON" in handler
    assert "raise RuntimeError(_LEGACY_JOB_EXECUTION_REASON)" in handler
    assert "composition.lifecycle.run_now(" not in handler
    assert "compose_media_hands_lifecycle" not in handler
    assert "EffectHandlerDeferred" in handler


def test_ai_turn_recovery_is_registered_as_post_reaper_coordination() -> None:
    source = _create_app_source()
    assert "register_coordination(CoordinationTaskRegistration(kind='ai-turn-scan'" in source
    assert "register_coordination(CoordinationTaskRegistration(kind='ai-turn-resume'" in source
    assert "register_job_execution_handler(" in source
    assert "job-ready-dispatch" not in source
    assert "kind='document_delivery'" in source
    assert "handler=document_delivery.handle_effect" in source
    assert "document-ready-dispatch" not in source
    assert "register_coordination(CoordinationTaskRegistration(kind='legacy-series-quarantine'" in source
    assert "register_coordination(CoordinationTaskRegistration(kind='memory-import-interruption'" in source
    assert "register_backfill(EffectBackfillRegistration(kind='team-memory-source-forget'" in source
    assert "register_team_memory_source_forget_handler(" in source
    assert "team-memory-source-forget-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='external-document-apply'" in source
    assert "register_external_apply_handler(" in source
    assert "external-document-apply-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='external-project-skill-apply'" in source
    assert "register_external_project_skill_apply_handler(" in source
    assert "external-project-skill-apply-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='external-series-candidate'" in source
    assert "register_external_series_candidate_handler(" in source
    assert "external-series-candidate-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='project-skill-review-staging'" in source
    assert "register_project_skill_review_staging_handler(" in source
    assert "project-skill-review-staging-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='memory-review-staging'" in source
    assert "register_memory_review_staging_handler(" in source
    assert "memory-review-staging-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='shared-trust-activation'" in source
    assert "register_shared_trust_activation_handler(" in source
    assert "shared-trust-activation-dispatch" not in source
    assert "register_backfill(EffectBackfillRegistration(kind='external-agent-publication'" in source
    assert "register_preparation(RecoveryPreparationRegistration(kind='external-agent-publication-outbox-prepare'" in source
    assert "register_external_agent_publication_handler(" in source
    assert "external-agent-publication-dispatch" not in source
    assert "register_preparation(RecoveryPreparationRegistration(kind='plugin-hands-upgrade-runtime'" in source
    assert "register_backfill(EffectBackfillRegistration(kind='plugin-hands-upgrade'" in source
    assert "register_plugin_hands_upgrade_handler(" in source
    assert "plugin-hands-upgrade-dispatch" not in source


def test_ai_turn_lease_only_coordinates_turn_and_cannot_claim_external_effect() -> None:
    scanner = (
        ROOT / "src/backend/api/ai_turn_recovery_startup.py"
    ).read_text(encoding="utf-8")
    worker = (
        ROOT / "src/backend/api/ai_turn_recovery_worker.py"
    ).read_text(encoding="utf-8")
    store_path = ROOT / "src/core/ai_kernel/sqlite_store.py"
    store_tree = ast.parse(store_path.read_text(encoding="utf-8"))
    renew = next(
        node for node in ast.walk(store_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "renew_run_lease"
    )
    renew_source = ast.unparse(renew)

    assert "classify_recovery" in scanner
    for forbidden in ("model_gateway", "invoke_tool", "execute_planned", "begin_planned"):
        assert forbidden not in scanner
    assert "runtime.recover_accepted_turn" in worker
    assert "model_gateway" not in worker
    assert "lease_owner=?" in renew_source
    assert "self._effect_runner.owner_id" in renew_source
    assert "self._effect_runner.renew" in renew_source
    for forbidden in ("begin_planned", ".plan(", "settle_ok", "mark_unknown"):
        assert forbidden not in renew_source


def test_document_delivery_recovery_is_core_owned() -> None:
    source = _create_app_source()
    assert "kind='document_delivery'" in source
    assert "kind='document_pdf_delivery'" in source
    assert "recover_prepared" not in source
    assert "recover_expired(limit=64)" not in source


def test_retention_recovery_is_registered_with_core_reaper() -> None:
    source = _create_app_source()
    runtime = (
        ROOT / "src/backend/api/retention_effect_runtime.py"
    ).read_text(encoding="utf-8")
    assert "register_retention_effect_handlers(" in source
    assert runtime.count("EffectHandlerRegistration(") == 2
    assert runtime.count("EffectRecoveryRegistration(") == 2
    assert 'contract_version=EFFECT_V2' in runtime
    for forbidden in ("EffectReaper(", "EffectRecoveryService(", "Thread("):
        assert forbidden not in runtime


def test_only_core_effect_runner_can_mutate_effect_state() -> None:
    forbidden = (
        "transition_in_connection(",
        "settle_ok_with_receipt_in_connection(",
        "renew_or_take_over_lease_in_connection(",
    )
    offenders: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        if path == ROOT / "src/core/effect_log/core.py":
            continue
        source = path.read_text(encoding="utf-8")
        for primitive in forbidden:
            if primitive in source:
                offenders.append(f"{path.relative_to(ROOT)}:{primitive}")
    assert offenders == []


def test_job_and_turn_coordination_leases_are_not_effect_runner_owners() -> None:
    job_store = (ROOT / "src/core/job_runner/sqlite_store.py").read_text(encoding="utf-8")
    ai_store = (ROOT / "src/core/ai_kernel/sqlite_store.py").read_text(encoding="utf-8")
    assert 'owner_role="job-effect-runner"' in job_store
    assert 'owner_role="ai-effect-runner"' in ai_store
    assert "_external_effect_lease_owner" not in job_store
    assert "_model_effect_lease_owner" not in ai_store


def test_ai_tool_runtime_has_no_private_retry_loop() -> None:
    runtime = (ROOT / "src/core/ai_kernel/runtime.py").read_text(encoding="utf-8")
    composition = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    app = _create_app_source()
    assert "def _wait_for_retry" not in runtime
    assert "sleep(min(remaining" not in runtime
    assert "attempt=attempt + 1" not in runtime
    assert "effect_runner=session_store.effect_runner" in composition
    assert "effect_runner=shared_effect_runner" in composition
    for effect_class in (
        "PURE", "IDEMPOTENT", "QUERYABLE", "AT_MOST_ONCE", "NEEDS_REAUTH",
    ):
        assert effect_class in app
    assert "verify=ai_turn_effect_store.verify_tool_call_effect" in app
