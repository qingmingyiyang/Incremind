from __future__ import annotations

import io
import json
import time
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from core.application_skill import ApplicationSkillPackageLoader
from backend.api.external_extension_install_workflow import (
    ExternalExtensionInstallWorkflow,
    ExternalExtensionInstallWorkflowError,
)
from backend.api.external_extension_runtime_startup import register_external_extension_runtime
from backend.security.mcp_approved_server_migration import (
    MCPApprovedServerMigrationAuthority,
    MCPApprovedServerMigrationConflict,
)
from core.effect_log import EffectState, build_effect_runtime
from core.external_extensions import ResolvedSource
from core.external_extension_runtime.outbound_fetch import FetchedBytes
from core.external_extension_runtime.fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactError,
    ExternalExtensionFactStore,
    artifact_ref,
    intent_ref,
    resolution_ref,
)
from core.external_extension_runtime.artifact_evidence import ImmutableQuarantineArtifactStore
from core.external_extension_runtime.gate_authority import (
    ExternalExtensionGateAuthorizationError,
    ExternalExtensionGateRequest,
    ExternalExtensionGateAuthority,
)
from core.storage_provider import SQLiteStructuredRecordStore


COMMIT = "a" * 40
COMMIT_B = "b" * 40


@dataclass(frozen=True)
class _WorkflowTestServices:
    """Explicit test-only white-box dependencies for workflow fault injection."""

    workflow: ExternalExtensionInstallWorkflow
    facts: ExternalExtensionFactStore
    gate_authority: ExternalExtensionGateAuthority
    runtime: object
    active_packages: object

    def preview(self, *args, **kwargs): return self.workflow.preview(*args, **kwargs)
    def confirm(self, *args, **kwargs): return self.workflow.confirm(*args, **kwargs)
    def _preview_from_intake(self, *args, **kwargs): return self.workflow._preview_from_intake(*args, **kwargs)
    def _incomplete_preview(self, *args, **kwargs): return self.workflow._incomplete_preview(*args, **kwargs)


class _Fetcher:
    def __init__(self, archive: bytes, *, files: dict[str, bytes] | None = None) -> None:
        self.archive = archive
        self.files = files
        self.calls: list[str] = []

    def fetch(self, url: str, **_kwargs: object) -> FetchedBytes:
        self.calls.append(url)
        if url.startswith("https://api.github.com/"):
            return FetchedBytes(
                url,
                json.dumps({"sha": COMMIT}).encode("utf-8"),
                "application/json",
                "93.184.216.34",
            )
        commit = url.rsplit("/", 1)[-1]
        body = _archive(commit, files=self.files) if self.files is not None else _archive(commit)
        return FetchedBytes(url, body, "application/zip", "93.184.216.34")


def _archive(
    commit: str = COMMIT, *, files: dict[str, bytes] | None = None,
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        selected = files or {
            "SKILL.md": b"---\nname: demo\ndescription: fixture skill\ntrigger_boundary: explicit\nvalidation: fixture\nmaturity: stable\n---\nUse fixture.\n",
        }
        for path, content in selected.items():
            archive.writestr(f"extensions-{commit}/{path}", content)
    return stream.getvalue()


def _mcp_candidate() -> dict[str, object]:
    return {
        "schema_version": "1.1.0",
        "servers": [{
            "server_id": "fixture", "enabled": False,
            "approval_status": "approved", "approval_revision": 1,
            "transport_kind": "streamable_http",
            "host_connection": {
                "server_id": "fixture", "manifest_revision": 1,
                "endpoint_identity": "fixture-endpoint",
                "credential_subject_id": "fixture-subject",
                "transport_generation": 1, "catalog_revision": 1,
            },
            "connection_manifest": {
                "server_id": "fixture", "manifest_revision": 1,
                "endpoint_identity": "fixture-endpoint",
                "credential_subject_id": "fixture-subject",
                "transport_generation": 1, "approval_revision": 1,
                "approval_status": "approved",
                "endpoint_url": "https://mcp.example.test/rpc",
                "headers": None, "secret_header_refs": None,
                "timeout_seconds": 5.0, "max_response_bytes": 1048576,
                "max_sse_events": 128,
            },
            "tool_policies": [{
                "tool_name": "fixture.read", "tool_id": "fixture.read",
                "version": 1, "display_name": "Fixture read",
                "description": "Reviewed Tool", "effect": "read",
                "data_classes": ["fixture"],
                "input_schema_uri": "crp://schemas/fixture-input",
                "output_schema_uri": "crp://schemas/fixture-output",
                "receipt_schema_uri": None, "operation_semantics": "read_only",
                "execution_mode": "parallel", "resource_locks": ["mcp:fixture"],
                "idempotency": "never_retry",
                "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []},
                "verification_tool_id": None, "compensation_tool_id": None,
                "mutability": "read_only", "egress_class": "remote",
                "network_scope": ["mcp:fixture"],
                "data_egress_scope": ["fixture"], "timeout_ms": 1000,
                "required_scopes": [], "boundary_requirements": ["mcp_enabled"],
                "requires_approval": False, "tool_schema_revision": 1,
                "reviewed_input_schema": {"type": "object"},
                "reviewed_output_schema": None, "available": True,
                "remote_receipt_field": None, "reviewed_receipt_schema": None,
            }],
        }],
    }


def _workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    archive_files: dict[str, bytes] | None = None,
):
    runtime = build_effect_runtime(tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="workflow-test")
    fetcher = _Fetcher(_archive(), files=archive_files)
    monkeypatch.setattr(
        "backend.api.external_extension_runtime_startup.production_extension_acquisition_fetcher",
        lambda: fetcher,
    )
    handles = register_external_extension_runtime(tmp_path, runtime)
    workflow = handles.install_workflow
    # Explicit test-only white-box evidence store reopened from the same DB.
    # Production workflow retains only its private callable port.
    facts = ExternalExtensionFactStore(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3"),
        ImmutableQuarantineArtifactStore(
            tmp_path / ".rebuild-data" / "external-extension-quarantine",
        ),
    )
    workflow._clock = lambda: 100
    return _WorkflowTestServices(
        workflow, facts, ExternalExtensionGateAuthority(facts), runtime,
        handles.active_packages,
    ), fetcher


def _record_resolution(
    workflow: ExternalExtensionInstallWorkflow,
    *,
    proposal_reference: str,
    operation_id: str,
    revision: str = COMMIT,
) -> str:
    """Record a minimal durable resolution for FactStore/Gate negative tests."""

    facts = workflow.facts
    proposal = facts.load_proposal(proposal_reference)
    install = facts.load_intent(str(proposal["intent_ref"]))
    assert install.source_spec is not None
    source = ResolvedSource(
        "github_repository",
        install.source_spec.locator,
        revision,
        artifact_ref(install.intent_id, operation_id),
    )
    reference = intent_ref(install.intent_id)
    facts.record_resolution_observation(
        operation_id=operation_id,
        intent_reference=reference,
        source=source,
    )
    facts.record_resolution(
        operation_id=operation_id,
        intent_reference=reference,
        source=source,
    )
    return resolution_ref(operation_id)


def _tamper_record(
    workflow: ExternalExtensionInstallWorkflow,
    collection: str,
    object_id: str,
    payload: dict[str, object],
) -> None:
    """Simulate storage corruption so read-time ancestry validation is exercised."""

    records = workflow.facts._records
    current = records.read(collection, object_id)
    assert current is not None
    with records.begin() as uow:
        uow.put(collection, object_id, payload, expected_revision=current.revision)
        uow.commit()


def test_preview_is_local_then_two_confirmations_activate_exact_github_source_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
        requested_ref=COMMIT,
    )

    assert preview.status == "source_review_required"
    assert preview.intake_ref is None
    assert preview.effects == ()
    assert fetcher.calls == []
    acquired = workflow.confirm(
        preview.preview_id,
        project_id="project-001",
        confirmations=preview.confirmation_ids,
    )
    assert acquired.status in {"review_ready", "review_required"}
    assert acquired.intake_ref is not None
    review = workflow._preview_from_intake(acquired.intake_ref, ())
    active = workflow.confirm(
        acquired.intake_ref,
        project_id="project-001",
        confirmations=review.confirmation_ids,
    )
    replay = workflow.confirm(
        intake_ref=acquired.intake_ref,
        project_id="project-001",
        confirmations=review.confirmation_ids,
    )

    assert active.status == replay.status == "active"
    assert active.snapshot == replay.snapshot
    assert all(effect.state is EffectState.SETTLED_OK for effect in active.effects)
    assert [effect.operation_id for effect in active.effects] == [effect.operation_id for effect in replay.effects]
    assert fetcher.calls == [f"https://codeload.github.com/example/extensions/zip/{COMMIT}"]

    upgrade_preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
        requested_ref=COMMIT_B,
    )
    upgrade_acquired = workflow.confirm(
        upgrade_preview.preview_id,
        project_id="project-001",
        confirmations=upgrade_preview.confirmation_ids,
    )
    assert upgrade_acquired.intake_ref is not None
    upgrade_review = workflow._preview_from_intake(upgrade_acquired.intake_ref, ())
    upgraded = workflow.workflow.upgrade_from_intake(
        "demo",
        intake_ref=upgrade_acquired.intake_ref,
        project_id="project-001",
        confirmations=upgrade_review.confirmation_ids,
        expected_state_revision=active.snapshot.state_revision,
        actor="desktop:test-session",
        reason="Upgrade to the reviewed immutable repository revision.",
    )
    assert upgraded.status == "active"
    assert upgraded.snapshot is not None
    assert upgraded.snapshot.active_revision == 2
    assert fetcher.calls[-1] == (
        f"https://codeload.github.com/example/extensions/zip/{COMMIT_B}"
    )

    original_settle = workflow.runtime.runner.settle_ok

    def crash_after_uninstall_receipt(*_args: object, **_kwargs: object):
        raise RuntimeError("simulated crash after uninstall receipt")

    monkeypatch.setattr(workflow.runtime.runner, "settle_ok", crash_after_uninstall_receipt)
    with pytest.raises(RuntimeError, match="uninstall receipt"):
        workflow.workflow.execute_lifecycle_action(
            "demo",
            project_id="project-001",
            action="uninstall",
            expected_state_revision=upgraded.snapshot.state_revision,
            actor="desktop:test-session",
            reason="Uninstall this reviewed Skill from the current project.",
        )
    monkeypatch.setattr(workflow.runtime.runner, "settle_ok", original_settle)
    recovered = workflow.runtime.recover_expired(now=int(time.time()) + 1_000)
    assert any(item.state is EffectState.SETTLED_OK for item in recovered)
    lifecycle_service = workflow.workflow._operations._execute_lifecycle.__self__
    assert lifecycle_service.reconcile_terminal_projections(limit=100) >= 1
    uninstalled, _history = workflow.workflow.installation_status(
        "demo", project_id="project-001",
    )
    assert uninstalled.status == "uninstalled"
    managed = tmp_path / ".rebuild-data" / "external-extension-skills"
    assert not (managed / "project-001" / "demo" / "revision-2").exists()
    assert len(tuple((managed / ".uninstalled").iterdir())) == 1
    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="workflow-test-restarted",
    )
    restarted = register_external_extension_runtime(tmp_path, restarted_runtime)
    restarted_snapshot, restarted_history = (
        restarted.install_workflow.installation_status(
            "demo", project_id="project-001",
        )
    )
    assert restarted_snapshot.status == "uninstalled"
    assert [item.revision for item in restarted_history] == [2, 1]
    assert restarted.active_packages("project-001") == ()
    with pytest.raises(ExternalExtensionInstallWorkflowError, match="state revision drifted"):
        workflow.workflow.execute_lifecycle_action(
            "demo",
            project_id="project-001",
            action="uninstall",
            expected_state_revision=upgraded.snapshot.state_revision,
            actor="desktop:test-session",
            reason="Uninstall this reviewed Skill from the current project.",
        )


def test_preview_asks_for_search_and_rejects_unsafe_input_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, fetcher = _workflow(tmp_path, monkeypatch)

    ask = workflow.preview("安装技能 a useful summarizer", "project-001")
    rejected = workflow.preview("安装技能 https://example.com/tool.zip; run", "project-001")

    assert ask.status == "requires_exact_source"
    assert ask.risks == ("source_resolution_required",)
    assert rejected.status == "rejected"
    assert rejected.intake_ref is None
    assert fetcher.calls == []


@pytest.mark.parametrize(
    ("fixture_name", "source_path", "skill_id", "instruction_marker"),
    (
        (
            "openai-codex-path-types",
            ".codex/skills/path-types/SKILL.md",
            "path-types",
            "# Path Types",
        ),
        (
            "deepseek-harness-model-only",
            ".dsh/skills/model-only-skill/SKILL.md",
            "model-only-skill",
            "model-only snapshot instructions",
        ),
        (
            "agent-skills-pdf-processing",
            ".agents/skills/pdf-processing/SKILL.md",
            "pdf-processing",
            "Agent Skills specification example",
        ),
    ),
)
def test_pinned_upstream_skill_dialects_install_activate_and_load_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    source_path: str,
    skill_id: str,
    instruction_marker: str,
) -> None:
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "external_extension_upstreams"
        / fixture_name
        / "SKILL.md"
    )
    workflow, _fetcher = _workflow(
        tmp_path,
        monkeypatch,
        archive_files={source_path: fixture.read_bytes()},
    )
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
        requested_ref=COMMIT,
    )
    acquired = workflow.confirm(
        preview.preview_id,
        project_id="project-001",
        confirmations=preview.confirmation_ids,
    )
    assert acquired.intake_ref is not None
    review = workflow._preview_from_intake(acquired.intake_ref, ())
    active = workflow.confirm(
        acquired.intake_ref,
        project_id="project-001",
        confirmations=review.confirmation_ids,
    )

    assert active.status == "active"
    packages = workflow.active_packages("project-001")
    assert [package.skill_id for package in packages] == [skill_id]
    instructions = ApplicationSkillPackageLoader().load_instructions(packages[0])
    assert instruction_marker in instructions.markdown

    assert active.snapshot is not None
    workflow.workflow.execute_lifecycle_action(
        skill_id,
        project_id="project-001",
        action="uninstall",
        expected_state_revision=active.snapshot.state_revision,
        actor="desktop:test-session",
        reason="Verify the pinned upstream dialect has a complete lifecycle.",
    )
    uninstalled, _history = workflow.workflow.installation_status(
        skill_id, project_id="project-001",
    )
    assert uninstalled.status == "uninstalled"
    assert workflow.active_packages("project-001") == ()


def test_floating_github_ref_requires_exact_resolved_revision_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
    )

    pinned = workflow.confirm(
        preview.preview_id,
        project_id="project-001",
        confirmations=preview.confirmation_ids,
    )

    assert pinned.status == "pinned_revision_confirmation_required"
    assert pinned.intake_ref is None
    assert pinned.pending_action == "approve_resolved_revision_download"
    assert pinned.resolved_revision == COMMIT
    assert fetcher.calls == [
        "https://api.github.com/repos/example/extensions/commits/HEAD"
    ]

    acquired = workflow.confirm(
        preview.preview_id,
        project_id="project-001",
        confirmations=("approve_resolved_revision_download",),
    )
    assert acquired.intake_ref is not None
    assert acquired.status in {"review_ready", "review_required", "active"}
    assert acquired.resolved_revision == COMMIT
    assert fetcher.calls == [
        "https://api.github.com/repos/example/extensions/commits/HEAD",
        f"https://codeload.github.com/example/extensions/zip/{COMMIT}",
    ]

    if acquired.status != "active":
        review = workflow._preview_from_intake(acquired.intake_ref, ())
        active = workflow.confirm(
            acquired.intake_ref,
            project_id="project-001",
            confirmations=review.confirmation_ids,
        )
        assert active.status == "active"


def test_initial_source_approval_cannot_authorize_floating_revision_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
    )
    pinned = workflow.confirm(
        preview.preview_id,
        project_id="project-001",
        confirmations=preview.confirmation_ids,
    )
    proposal = workflow.facts.load_proposal(preview.preview_id)
    install = workflow.facts.load_intent(str(proposal["intent_ref"]))
    source_confirmation = workflow.facts.source_confirmation_for_intent(
        intent_ref(install.intent_id)
    )
    resolution_ref = workflow.facts.resolution_reference(
        pinned.effects[0].operation_id
    )
    assert resolution_ref is not None

    with pytest.raises(
        ExternalExtensionGateAuthorizationError,
        match="exact revision confirmation",
    ):
        workflow.gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_acquire",
            project_id="project-001",
            subject_ref=resolution_ref,
            authorization_ref=source_confirmation,
            policy_revision="external-extension-natural-language-policy-v1",
        ))

    assert fetcher.calls == [
        "https://api.github.com/repos/example/extensions/commits/HEAD"
    ]


def test_fact_store_rejects_revision_confirmation_without_initial_source_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions", "project-001",
    )
    resolution_reference = _record_resolution(
        workflow,
        proposal_reference=preview.preview_id,
        operation_id="unapproved-resolution-1001",
    )

    with pytest.raises(
        ExternalExtensionFactConflict,
        match="unique initial source confirmation",
    ):
        workflow.facts.confirm_resolved_revision(
            preview.preview_id,
            resolution_reference,
            confirmation_id="revision-confirmation-1001",
            confirmation_ids=("approve_resolved_revision_download",),
            actor="local-operator",
            reason="Attempting a revision confirmation without a source ancestor.",
        )


def test_gate_rechecks_project_resolution_reference_and_revision_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupted immutable records must fail closed at FactStore/Gate read time."""

    def prepared(name: str):
        workflow, _fetcher = _workflow(tmp_path / name, monkeypatch)
        preview = workflow.preview(
            "安装技能 https://github.com/example/extensions", "project-001",
        )
        facts = workflow.facts
        source_reference = facts.confirm_source_proposal(
            preview.preview_id,
            confirmation_id=f"source-confirmation-{name}",
            confirmation_ids=("approve_initial_network_source",),
            actor="local-operator",
            reason="Approved exact fixture source.",
        )
        resolution_reference = _record_resolution(
            workflow,
            proposal_reference=preview.preview_id,
            operation_id=f"resolution-{name}-1001",
        )
        revision_reference = facts.confirm_resolved_revision(
            preview.preview_id,
            resolution_reference,
            confirmation_id=f"revision-confirmation-{name}",
            confirmation_ids=("approve_resolved_revision_download",),
            actor="local-operator",
            reason="Approved resolved fixture revision.",
        )
        return workflow, preview, source_reference, resolution_reference, revision_reference

    # Project drift in the initial source ancestor is rejected before a Gate fact.
    workflow, preview, source_reference, _resolution, _revision = prepared("project")
    source_id = source_reference.rsplit("/", 1)[-1]
    source_record = workflow.facts._records.read(
        "external_extension_source_confirmations", source_id,
    )
    assert source_record is not None
    project_payload = dict(source_record.payload)
    project_payload["project_id"] = "project-foreign"
    _tamper_record(
        workflow, "external_extension_source_confirmations", source_id, project_payload,
    )
    proposal = workflow.facts.load_proposal(preview.preview_id)
    with pytest.raises(ExternalExtensionFactConflict, match="proposal drifted"):
        workflow.gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_resolve",
            project_id="project-001",
            subject_ref=str(proposal["intent_ref"]),
            authorization_ref=source_reference,
            policy_revision="external-extension-natural-language-policy-v1",
        ))

    # A revision confirmation cannot be retargeted to another resolution reference.
    workflow, _preview, _source, _resolution, revision_reference = prepared("resolution")
    revision_id = revision_reference.rsplit("/", 1)[-1]
    revision_record = workflow.facts._records.read(
        "external_extension_revision_confirmations", revision_id,
    )
    assert revision_record is not None
    resolution_payload = dict(revision_record.payload)
    resolution_payload["resolution_ref"] = "crp://external-extension-source-resolutions/missing-resolution-1001"
    _tamper_record(
        workflow, "external_extension_revision_confirmations", revision_id, resolution_payload,
    )
    with pytest.raises(ExternalExtensionFactError, match="source resolution is missing"):
        workflow.facts.load_gate_confirmation(revision_reference)

    # The durable source-confirmation reference is itself part of the revision fact.
    workflow, _preview, _source, resolution_reference, revision_reference = prepared("reference")
    revision_id = revision_reference.rsplit("/", 1)[-1]
    revision_record = workflow.facts._records.read(
        "external_extension_revision_confirmations", revision_id,
    )
    assert revision_record is not None
    reference_payload = dict(revision_record.payload)
    reference_payload["source_confirmation_ref"] = "crp://external-extension-source-confirmations/missing-source-1001"
    _tamper_record(
        workflow, "external_extension_revision_confirmations", revision_id, reference_payload,
    )
    with pytest.raises(ExternalExtensionFactError, match="source confirmation is missing"):
        workflow.gate_authority.authorize(ExternalExtensionGateRequest(
            phase="source_acquire",
            project_id="project-001",
            subject_ref=resolution_reference,
            authorization_ref=revision_reference,
            policy_revision="external-extension-natural-language-policy-v1",
        ))

    # A changed resolved revision invalidates the signed revision confirmation.
    workflow, _preview, _source, resolution_reference, revision_reference = prepared("revision")
    resolution_id = resolution_reference.rsplit("/", 1)[-1]
    resolved_record = workflow.facts._records.read(
        "external_extension_source_resolutions", resolution_id,
    )
    assert resolved_record is not None
    drifted_resolution = dict(resolved_record.payload)
    drifted_source = dict(drifted_resolution["source"])
    drifted_source["immutable_revision"] = "b" * 40
    drifted_resolution["source"] = drifted_source
    _tamper_record(
        workflow, "external_extension_source_resolutions", resolution_id, drifted_resolution,
    )
    with pytest.raises(ExternalExtensionFactConflict, match="revision confirmation resolution drifted"):
        workflow.facts.load_gate_confirmation(revision_reference)


def test_fact_store_rejects_fixed_requested_revision_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
        requested_ref=COMMIT,
    )

    with pytest.raises(ExternalExtensionFactConflict, match="fixed source revision"):
        _record_resolution(
            workflow,
            proposal_reference=preview.preview_id,
            operation_id="fixed-mismatch-resolution-1001",
            revision="b" * 40,
        )


def test_confirm_rejects_wrong_confirmation_or_project_without_client_gate_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions", "project-001", requested_ref=COMMIT,
    )

    with pytest.raises(ExternalExtensionInstallWorkflowError, match="confirmation"):
        workflow.confirm(preview.preview_id, project_id="project-001", confirmations=("wrong-confirmation",))
    with pytest.raises(ExternalExtensionInstallWorkflowError, match="project"):
        workflow.confirm(preview.preview_id, project_id="project-002", confirmations=preview.confirmation_ids)

    assert "gate_fact" not in workflow.preview.__code__.co_varnames
    assert "gate_fact" not in workflow.confirm.__code__.co_varnames


def test_confirm_cas_is_part_of_replay_identity_and_incomplete_effects_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, _fetcher = _workflow(tmp_path, monkeypatch)
    preview = workflow.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
        requested_ref=COMMIT,
    )

    acquired = workflow.confirm(
        preview.preview_id,
        project_id="project-001",
        confirmations=preview.confirmation_ids,
    )
    assert acquired.intake_ref is not None
    review = workflow._preview_from_intake(acquired.intake_ref, ())
    with pytest.raises(ValueError, match="revision is stale"):
        workflow.confirm(
            acquired.intake_ref,
            project_id="project-001",
            confirmations=review.confirmation_ids,
            expected_state_revision=1,
        )
    active = workflow.confirm(
        acquired.intake_ref,
        project_id="project-001",
        confirmations=review.confirmation_ids,
        expected_state_revision=0,
    )
    assert active.status == "active"

    raw = workflow.runtime.log.get(acquired.effects[0].operation_id)
    failed = workflow._incomplete_preview(
        acquired.preview_id,
        replace(raw, state=EffectState.SETTLED_ERR, result_ref=None),
    )
    unknown = workflow._incomplete_preview(
        acquired.preview_id,
        replace(raw, state=EffectState.UNKNOWN, result_ref=None),
    )
    inflight = workflow._incomplete_preview(
        acquired.preview_id,
        replace(raw, state=EffectState.INFLIGHT, result_ref=None),
    )
    assert failed.status == "failed"
    assert unknown.status == "needs_attention"
    assert inflight.status == "pending"
    assert all(item.extension_id is None for item in (failed, unknown, inflight))


def test_mcp_import_preview_binds_immutable_intake_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_files = {
        ".mcp.json": json.dumps({
            "mcpServers": {
                "fixture": {"url": "https://mcp.example.test/rpc"},
            },
        }).encode("utf-8"),
    }
    services, _fetcher = _workflow(
        tmp_path, monkeypatch, archive_files=archive_files,
    )
    source = services.preview(
        "安装技能 https://github.com/example/extensions",
        "project-001",
        requested_ref=COMMIT,
    )
    acquired = services.confirm(
        source.preview_id,
        project_id="project-001",
        confirmations=source.confirmation_ids,
    )
    assert acquired.intake_ref is not None
    review = services._preview_from_intake(acquired.intake_ref, ())

    result = services.workflow.preview_mcp_import(
        intake_ref=acquired.intake_ref,
        project_id="project-001",
        reviewed_candidate=_mcp_candidate(),
        confirmations=review.confirmation_ids,
        actor="local-user",
        reason="Reviewed the exact disabled MCP candidate.",
    )

    authority = MCPApprovedServerMigrationAuthority(tmp_path)
    stored = authority.status(result.migration_id)
    assert stored is not None
    assert stored.state == "previewed"
    assert stored.provenance_ref == result.review_receipt_ref
    assert authority.active_snapshot().servers == ()
    with pytest.raises(
        MCPApprovedServerMigrationConflict,
        match="provenance confirmation is required",
    ):
        authority.confirm(
            migration_id=result.migration_id,
            expected_revision=result.migration_revision,
            command_id="generic-confirm-is-forbidden",
            confirmed=True,
        )

    restarted_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="workflow-mcp-restart",
    )
    restarted = register_external_extension_runtime(tmp_path, restarted_runtime)
    replay = restarted.install_workflow.preview_mcp_import(
        intake_ref=acquired.intake_ref,
        project_id="project-001",
        reviewed_candidate=_mcp_candidate(),
        confirmations=review.confirmation_ids,
        actor="local-user",
        reason="Reviewed the exact disabled MCP candidate.",
    )
    assert replay == result
