from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function was not found: {path}:{name}")


def _class_function(path: Path, class_name: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == name:
                    return member
    raise AssertionError(f"class function was not found: {path}:{class_name}.{name}")


def test_only_core_writes_effect_lease_columns_directly() -> None:
    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        if path == ROOT / "src/core/effect_log/core.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "UPDATE effect SET lease_owner" in text:
            violations.append(path.relative_to(ROOT).as_posix())
    assert violations == []


def test_model_success_uses_core_receipt_binding_and_legacy_job_store_cannot_settle() -> None:
    ai_store = (ROOT / "src/core/ai_kernel/sqlite_store.py").read_text(encoding="utf-8")
    job_store = (ROOT / "src/core/job_runner/sqlite_store.py").read_text(encoding="utf-8")
    assert ai_store.count("self._effect_runner.settle_ok(") >= 2
    # Job is a projection.  Legacy Job storage must not take a terminal Effect
    # transition, including for quarantined Media history; v2 domain handlers
    # return an EffectReceipt and Core execute_planned settles it below.
    assert "settle_verified_ok(" not in job_store
    assert "self._effect_runner.settle_ok(" not in job_store
    assert "settle_ok_with_receipt_in_connection(" not in ai_store
    assert "settle_ok_with_receipt_in_connection(" not in job_store


def test_media_v2_receipt_binding_is_core_owned() -> None:
    job_projection_path = ROOT / "src/core/job_runner/job_projection.py"
    media_execution_path = ROOT / "src/core/media_hands/effect_execution.py"
    job_runtime_path = ROOT / "src/backend/api/job_execution_runtime.py"
    core_path = ROOT / "src/core/effect_log/core.py"
    effect_runtime_path = ROOT / "src/core/effect_log/runtime.py"
    job_projection = job_projection_path.read_text(encoding="utf-8")
    media_execution = media_execution_path.read_text(encoding="utf-8")
    app_source = (ROOT / "src/backend/api/app.py").read_text(encoding="utf-8")
    core_execute = ast.unparse(_function(core_path, "execute_planned"))
    core_dispatch = ast.unparse(_function(effect_runtime_path, "dispatch_planned"))
    media_handle = ast.unparse(_function(media_execution_path, "handle"))
    media_probe = ast.unparse(_function(media_execution_path, "probe"))
    media_payload = ast.unparse(_function(media_execution_path, "_receipt_payload"))
    media_receipt = ast.unparse(_function(media_execution_path, "_effect_receipt"))
    root_registration = ast.unparse(_function(job_runtime_path, "register_job_execution_handler"))
    media_registration = ast.unparse(
        _function(job_runtime_path, "register_media_hands_v2_job_execution_handler")
    )

    assert "settle_ok_with_receipt_in_connection(" not in media_execution
    assert "register_job_execution_handler(" in app_source
    assert "register_media_hands_v2_job_execution_handler(" in root_registration
    assert "self.runner.execute_planned(" in core_dispatch
    assert "isinstance(result, EffectReceipt)" in core_execute
    assert "outcome = self.settle_ok(" in core_execute
    assert "self._require_owned(fresh, now=settled_at)" in core_execute
    assert "fresh.attempt != current[0].attempt" in core_execute
    assert "result.receipt_ref" in core_execute
    assert "result.receipt_kind" in core_execute
    assert "result.receipt_schema_version" in core_execute
    assert "return _effect_receipt(effect)" in media_handle
    assert (
        "EffectReceipt(_receipt_ref(effect.operation_id), RECEIPT_KIND, "
        "RECEIPT_SCHEMA, INTENT_SCHEMA)"
    ) in media_receipt
    for field in (
        "'receipt_ref': _receipt_ref(effect.operation_id)",
        "'receipt_kind': RECEIPT_KIND",
        "'receipt_schema_version': RECEIPT_SCHEMA",
        "'intent_schema_version': INTENT_SCHEMA",
    ):
        assert field in media_payload
    assert "EffectState.SETTLED_OK" in media_probe
    assert "receipt['receipt_ref']" in media_probe
    for required in (
        "effect_runtime.handlers.register",
        "effect_runtime.recoveries.register",
        "kind=EFFECT_KIND",
        "contract_version=EFFECT_V2",
        "receipt_kind=RECEIPT_KIND",
        "receipt_schema_version=RECEIPT_SCHEMA",
    ):
        assert required in media_registration

    for forbidden in (
        "settle_ok(",
        "settle_verified_ok(",
        "mark_unknown(",
        "begin_planned(",
    ):
        assert forbidden not in job_projection


def test_model_terminal_does_not_use_reservation_status_or_lease_as_authority() -> None:
    path = ROOT / "src/core/ai_kernel/sqlite_store.py"
    dispatch = ast.unparse(_function(path, "commit_model_attempt_dispatch_bundle"))
    source = ast.unparse(_function(path, "append_model_attempt_terminal_bundle"))
    assert 'reservation[4]) != "committed"' not in source
    assert "_row_model_attempt_lease_identity" not in source
    assert "SELECT state,lease_owner,attempt,lease_expires_at FROM effect" in source
    assert "WHERE attempt_id=? AND status='committed' AND terminal_receipt_ref IS NULL " in source
    assert "AND lease_owner_id IS ? AND lease_generation IS ?" in source
    assert "self._assert_model_attempt_run_lease" in source
    assert "EffectState.INFLIGHT.value" in source
    assert "self._effect_runner.owner_id" in source
    assert "lease_owner_id, lease_generation" not in dispatch
    assert "model attempt Effect was not found" in source


def test_ordinary_model_wire_is_executed_inside_core_runner_handler() -> None:
    store_path = ROOT / "src/core/ai_kernel/sqlite_store.py"
    runtime_path = ROOT / "src/core/ai_kernel/runtime.py"
    gateway_path = ROOT / "src/backend/shared/llm/litellm_gateway.py"
    execute = ast.unparse(_function(store_path, "execute_model_attempt_handler"))
    dispatch = ast.unparse(_function(runtime_path, "_store_model_attempt_dispatch"))
    complete = ast.unparse(
        _class_function(gateway_path, "LiteLLMCompletionGateway", "complete_text_with_usage")
    )

    assert "self._effect_runner.execute_planned" in execute
    assert "result_box['value']" in execute
    assert "execute_model_attempt_handler" in dispatch
    assert "def handle_wire" in complete
    assert "self._completion(**request)" in complete
    assert "terminal.succeeded" in complete
    assert "terminal.invoke(handle_wire)" in complete


def test_media_recipe_resume_projects_effect_state() -> None:
    path = ROOT / "src/core/job_runner/sqlite_store.py"
    resume = ast.unparse(_function(path, "media_recipe_resume_ready"))
    receipt = ast.unparse(_function(path, "get_media_recipe_step_receipt"))
    assert "_project_media_execution_evidence" in resume
    assert "_project_media_recipe_step_evidence" in resume
    assert "_project_media_recipe_step_evidence" in receipt


def test_plugin_hands_execution_uses_effect_lease_and_receipt_authority() -> None:
    path = ROOT / "src/core/plugin_hands/durable_lifecycle.py"
    source = path.read_text(encoding="utf-8")
    fence = ast.unparse(_function(path, "fence"))
    outcome = ast.unparse(_function(path, "record_outcome"))
    assert 'kind="plugin_hands_execution"' in source
    assert "EffectClass.AT_MOST_ONCE" in source
    assert "self._runner.begin_planned" in fence
    assert "claimed.lease_owner != self._runner.owner_id" in fence
    assert "self._runner.settle_ok" in outcome
    assert "self._runner.mark_unknown" in outcome
    assert "self._runner.settle_error" in outcome


def test_turn_coordination_lease_cannot_settle_model_effect() -> None:
    path = ROOT / "src/core/ai_kernel/sqlite_store.py"
    heartbeat = ast.unparse(_function(path, "renew_run_lease"))
    terminal = ast.unparse(_function(path, "append_model_attempt_terminal_bundle"))
    execute = ast.unparse(_function(path, "execute_model_attempt_handler"))
    assert "self._effect_runner.renew" in heartbeat
    assert "self._effect_runner.settle_ok" not in terminal
    assert "self._effect_runner.execute_planned" in execute
    assert "SELECT state,lease_owner,attempt,lease_expires_at FROM effect" in terminal


def test_image_generation_uses_core_effect_and_immutable_asset_probe() -> None:
    path = ROOT / "src/backend/api/image_generation_ai_runtime.py"
    composition = (ROOT / "src/backend/memory_app/kernel/ai_runtime.py").read_text(encoding="utf-8")
    source = path.read_text(encoding="utf-8")
    invoke = ast.unparse(_function(path, "invoke"))
    recover = ast.unparse(_function(path, "recover_completed_invocation"))
    probe = ast.unparse(_function(path, "_recover_from_asset"))
    legacy_reader = ast.unparse(_function(path, "get"))

    assert "ObjectStoreImageGenerationOperationEvidence(" not in composition
    assert "self._operations" not in invoke
    assert "gateway.generate" in invoke
    assert "_recover_from_asset" in recover
    assert "find_by_operation" in probe
    assert "gateway.generate" not in probe
    assert "self._objects.write" not in legacy_reader
    assert "def reserve(" not in source
    assert "def put(" not in source


def test_document_delivery_keeps_intent_immutable_and_projects_receipt() -> None:
    path = ROOT / "src/core/product_core/document_delivery.py"
    handle = ast.unparse(_function(path, "handle_effect"))
    verify = ast.unparse(_function(path, "verify_effect"))
    projection = ast.unparse(_function(path, "_resolved_payload"))

    assert "uow.put(_RECEIPTS" in handle
    assert "uow.put(_DELIVERIES" not in handle
    assert "self._resolved_payload" in verify
    assert "self.records.read(_RECEIPTS" in projection
    assert "'status': 'completed'" in projection


def test_document_pdf_uses_effect_lease_and_immutable_publication_fact() -> None:
    path = ROOT / "src/core/product_core/document_pdf_delivery.py"
    claim = ast.unparse(_function(path, "claim"))
    complete = ast.unparse(_function(path, "complete"))
    verify = ast.unparse(_function(path, "verify_effect"))
    probe_projection = ast.unparse(_function(path, "_resolved_with_receipt"))

    assert "self.runner.begin_planned" in claim
    assert "uow.put(_OPERATIONS" not in claim
    assert "uow.put(_PUBLICATIONS" in complete
    assert "uow.put(_RECEIPTS" in complete
    assert "self.runner.settle_ok" in complete
    assert "uow.put(_OPERATIONS" not in complete
    assert "uow.put(_OPERATIONS" not in verify
    assert "uow.read(_PUBLICATIONS" in verify
    assert "receipt_record.payload" in probe_projection


def test_memory_publication_receipt_is_fact_and_probe_never_reexecutes_writer() -> None:
    path = ROOT / "src/backend/api/memory_publication_effect_runtime.py"
    execute = ast.unparse(_function(path, "execute"))
    handle = ast.unparse(_function(path, "handle_v2"))
    probe = ast.unparse(_function(path, "probe_v2"))
    write = ast.unparse(_function(path, "_write_receipt"))

    assert "transaction.commit()" in execute
    assert "_write_receipt" in execute
    assert "return EffectReceipt" in handle
    assert "execute(effect)" not in probe
    assert "_recover_receipt_from_publication" in probe
    assert "_write_immutable" in write


def test_external_extension_terminal_receipt_has_no_execution_state_machine() -> None:
    path = ROOT / "src/core/external_extension_runtime/terminal_receipts.py"
    source = path.read_text(encoding="utf-8")
    store = ast.unparse(_function(path, "record_effect_receipt"))
    immutable = ast.unparse(_function(path, "_put_immutable"))
    handle = ast.unparse(_function(path, "__call__"))

    assert "self._put_immutable" in store
    assert "expected_revision=0" in immutable
    assert "return EffectReceipt" in handle
    assert "outcome = self._executor.probe(intent, effect)" in source


def test_source_retention_purge_uses_effect_lease_and_immutable_facts() -> None:
    path = ROOT / "src/core/product_core/source_retention_purge.py"
    execute = ast.unparse(_class_function(path, "ExecuteSourceRetentionPurge", "execute"))
    handle = ast.unparse(_class_function(path, "ExecuteSourceRetentionPurge", "_execute_claimed"))
    probe = ast.unparse(_class_function(path, "ExecuteSourceRetentionPurge", "verify_effect"))

    assert "self._runner.log.plan_v2" in execute
    assert "gate_fact=gate_fact" in execute
    assert "return _effect_receipt" in handle
    assert "self._runner.execute_planned" in execute
    assert "expected_revision=0" in handle
    assert "_STEP_COLLECTION" in handle
    assert "_RECEIPT_COLLECTION" in handle
    assert "self._store.delete" in handle
    assert "self._store.delete" not in probe
    assert "status" not in ast.unparse(_function(path, "_validate_terminal_receipt"))


def test_original_asset_retention_uses_effect_lease_and_immutable_facts() -> None:
    path = ROOT / "src/core/product_core/original_asset_retention.py"
    execute = ast.unparse(_class_function(path, "ExecuteOriginalAssetRetentionPurge", "execute"))
    handle = ast.unparse(_class_function(path, "ExecuteOriginalAssetRetentionPurge", "_execute_claimed"))
    probe = ast.unparse(_class_function(path, "ExecuteOriginalAssetRetentionPurge", "verify_effect"))
    write_step = ast.unparse(_class_function(path, "ExecuteOriginalAssetRetentionPurge", "_write_step"))
    terminal = ast.unparse(_function(path, "_validate_terminal_receipt"))

    assert "self._runner.log.plan_v2" in execute
    assert "gate_fact=gate_fact" in execute
    assert "return _effect_receipt" in handle
    assert "self._runner.execute_planned" in execute
    assert "self._step" in handle
    assert "self._write_step" in handle
    assert "_STEP_COLLECTION" in write_step
    assert "_RECEIPT_COLLECTION" in handle
    assert "expected_revision=0" in write_step
    assert "os.replace" in handle
    assert "self._store.delete" in handle
    assert "self._store.delete" not in probe
    assert "status" not in terminal
