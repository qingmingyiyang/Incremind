from __future__ import annotations

import pytest

from core.long_horizon_runtime.provenance import (
    ProvenanceContractError,
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
)


def test_provenance_contract_round_trips_project_scoped_versioned_validation() -> None:
    task = TraceSubject(project_id="project-alpha", kind="task", subject_id="task-alpha")
    artifact = TraceSubject(project_id="project-alpha", kind="artifact", subject_id="artifact-alpha")
    binding = VersionBinding(
        authority_ref="crp://artifacts/project-alpha/artifact-alpha",
        revision="revision-7",
        content_fingerprint="fingerprint-7",
    )
    link = TraceLink(
        project_id="project-alpha",
        source=task,
        source_version=VersionBinding(
            authority_ref="crp://tasks/project-alpha/task-alpha", revision="revision-1", content_fingerprint=None,
        ),
        target=artifact,
        target_version=binding,
        relation="produced_by",
    )
    validation = ValidationFact(
        project_id="project-alpha",
        validation_id="validation-alpha",
        subject=artifact,
        subject_version=binding,
        verdict="verified",
        validator_kind="receipt",
        validator_revision="receipt-schema-1",
        evidence_refs=("crp://receipts/project-alpha/terminal",),
    )

    assert TraceSubject.from_payload(task.to_payload()) == task
    assert VersionBinding.from_payload(binding.to_payload()) == binding
    assert TraceLink.from_payload(link.to_payload()) == link
    assert ValidationFact.from_payload(validation.to_payload()) == validation


def test_provenance_contract_rejects_scope_cycles_unbound_validation_and_sensitive_shapes() -> None:
    task = TraceSubject(project_id="project-alpha", kind="task", subject_id="task-alpha")
    other = TraceSubject(project_id="project-beta", kind="artifact", subject_id="artifact-beta")
    with pytest.raises(ProvenanceContractError, match="cross-project"):
        TraceLink(
            project_id="project-alpha", source=task,
            source_version=VersionBinding("crp://tasks/project-alpha/task-alpha", "revision-1", None),
            target=other,
            target_version=VersionBinding("crp://artifacts/project-beta/artifact-beta", "revision-1", None),
            relation="uses",
        )
    with pytest.raises(ProvenanceContractError, match="self"):
        TraceLink(
            project_id="project-alpha", source=task,
            source_version=VersionBinding("crp://tasks/project-alpha/task-alpha", "revision-1", None),
            target=task,
            target_version=VersionBinding("crp://tasks/project-alpha/task-alpha", "revision-1", None),
            relation="uses",
        )
    with pytest.raises(ProvenanceContractError, match="precise"):
        TraceLink(
            project_id="project-alpha", source=task,
            source_version=VersionBinding("crp://tasks/project-alpha/task-alpha", None, None),
            target=TraceSubject(project_id="project-alpha", kind="data", subject_id="data-alpha"),
            target_version=VersionBinding("crp://data/project-alpha/data-alpha", "revision-1", None),
            relation="uses",
        )
    with pytest.raises(ProvenanceContractError, match="revision"):
        ValidationFact(
            project_id="project-alpha",
            validation_id="validation-unbound",
            subject=task,
            subject_version=VersionBinding(
                authority_ref="crp://tasks/project-alpha/task-alpha",
                revision=None,
                content_fingerprint=None,
            ),
            verdict="inconclusive",
            validator_kind="receipt",
            validator_revision="receipt-schema-1",
            evidence_refs=("crp://receipts/project-alpha/terminal",),
        )
    with pytest.raises(ProvenanceContractError, match="shape"):
        TraceSubject.from_payload({
            "schema_version": "1.0.0", "project_id": "project-alpha", "kind": "task",
            "subject_id": "task-alpha", "prompt": "leak",
        })
    with pytest.raises(ProvenanceContractError, match="reference"):
        VersionBinding(
            authority_ref="https://example.test/artifact",
            revision="revision-1",
            content_fingerprint=None,
        )
    with pytest.raises(ProvenanceContractError, match="evidence"):
        ValidationFact(
            project_id="project-alpha",
            validation_id="validation-evidence",
            subject=task,
            subject_version=VersionBinding(
                authority_ref="crp://tasks/project-alpha/task-alpha",
                revision="revision-1",
                content_fingerprint=None,
            ),
            verdict="rejected",
            validator_kind="receipt",
            validator_revision="receipt-schema-1",
            evidence_refs=(),
        )
    with pytest.raises(ProvenanceContractError, match="crossed project scope"):
        TraceLink(
            project_id="project-alpha",
            source=task,
            source_version=VersionBinding(
                "crp://tasks/project-beta/task-alpha", "revision-1", None,
            ),
            target=TraceSubject(
                project_id="project-alpha", kind="data", subject_id="data-alpha",
            ),
            target_version=VersionBinding(
                "crp://data/project-alpha/data-alpha", "revision-1", None,
            ),
            relation="uses",
        )
    with pytest.raises(ProvenanceContractError, match="crossed project scope"):
        ValidationFact(
            project_id="project-alpha",
            validation_id="validation-cross-project-evidence",
            subject=task,
            subject_version=VersionBinding(
                "crp://tasks/project-alpha/task-alpha", "revision-1", None,
            ),
            verdict="verified",
            validator_kind="receipt",
            validator_revision="receipt-schema-1",
            evidence_refs=("crp://receipts/project-beta/terminal",),
        )
    with pytest.raises(ProvenanceContractError, match="crossed project scope"):
        TraceLink(
            project_id="project-alpha",
            source=task,
            source_version=VersionBinding(
                "crp://tasks/project-beta/project-alpha/task-alpha", "revision-1", None,
            ),
            target=TraceSubject(
                project_id="project-alpha", kind="data", subject_id="data-alpha",
            ),
            target_version=VersionBinding(
                "crp://data/project-alpha/data-alpha", "revision-1", None,
            ),
            relation="uses",
        )
    with pytest.raises(ProvenanceContractError, match="crossed project scope"):
        ValidationFact(
            project_id="project-alpha",
            validation_id="validation-smuggled-project-evidence",
            subject=task,
            subject_version=VersionBinding(
                "crp://tasks/project-alpha/task-alpha", "revision-1", None,
            ),
            verdict="verified",
            validator_kind="receipt",
            validator_revision="receipt-schema-1",
            evidence_refs=(
                "crp://receipts/project-beta/project-alpha/terminal",
            ),
        )
