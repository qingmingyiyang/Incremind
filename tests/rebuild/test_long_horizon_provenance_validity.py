from __future__ import annotations

from core.long_horizon_runtime.provenance import (
    ProjectProvenanceTrace,
    RecordedTraceSubject,
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
)


PROJECT = "project-alpha"


def _subject(kind: str, subject_id: str, revision: str) -> tuple[TraceSubject, VersionBinding]:
    subject = TraceSubject(PROJECT, kind, subject_id)
    return subject, VersionBinding(
        f"crp://provenance/{PROJECT}/{kind}/{subject_id}", revision, None,
    )


def _validation(subject: TraceSubject, version: VersionBinding, verdict: str) -> ValidationFact:
    return ValidationFact(
        PROJECT, f"validation-{subject.kind}-{subject.subject_id}-{verdict}",
        subject, version, verdict, "receipt", "r1",
        (f"crp://receipts/{PROJECT}/{subject.subject_id}-{verdict}",),
    )


def test_new_data_revision_stales_a_multihop_dependency_chain_without_rewriting_history() -> None:
    data, data_r1 = _subject("data", "dataset", "r1")
    experiment, experiment_r1 = _subject("experiment", "experiment", "r1")
    artifact, artifact_r1 = _subject("artifact", "artifact", "r1")
    action, action_r1 = _subject("world_action", "action", "r1")
    agent, agent_r1 = _subject("agent_run", "agent", "r1")
    _, data_r2 = _subject("data", "dataset", "r2")
    records = tuple(
        RecordedTraceSubject(subject, version, index)
        for index, (subject, version) in enumerate((
            (data, data_r1), (experiment, experiment_r1), (artifact, artifact_r1),
            (action, action_r1), (agent, agent_r1), (data, data_r2),
        ), start=1)
    )
    links = (
        TraceLink(PROJECT, experiment, experiment_r1, data, data_r1, "depends_on"),
        TraceLink(PROJECT, artifact, artifact_r1, experiment, experiment_r1, "produced_by"),
        TraceLink(PROJECT, action, action_r1, artifact, artifact_r1, "uses"),
        TraceLink(PROJECT, agent, agent_r1, action, action_r1, "executes"),
    )
    validations = tuple(_validation(subject, version, "verified") for subject, version in (
        (data, data_r1), (experiment, experiment_r1), (artifact, artifact_r1),
        (action, action_r1), (agent, agent_r1),
    ))
    trace = ProjectProvenanceTrace(PROJECT, 6, records, links, validations)

    validity = trace.current_validity
    assert validity.for_subject(data, data_r1).status == "stale"
    assert all(
        validity.for_subject(subject, version).status == "stale"
        for subject, version in ((experiment, experiment_r1), (artifact, artifact_r1), (action, action_r1), (agent, agent_r1))
    )
    assert validity.for_subject(data, data_r2).status == "pending"
    assert validity.stable_subjects == ()
    assert trace.validations == validations


def test_rejected_target_invalidates_dependants_and_inconclusive_blocks_stability() -> None:
    data, data_v = _subject("data", "dataset", "r1")
    artifact, artifact_v = _subject("artifact", "artifact", "r1")
    action, action_v = _subject("world_action", "action", "r1")
    agent, agent_v = _subject("agent_run", "agent", "r1")
    hypothesis, hypothesis_v = _subject("hypothesis", "hypothesis", "r1")
    records = tuple(RecordedTraceSubject(subject, version, index) for index, (subject, version) in enumerate(
        ((data, data_v), (artifact, artifact_v), (action, action_v), (agent, agent_v), (hypothesis, hypothesis_v)), start=1,
    ))
    links = (
        TraceLink(PROJECT, artifact, artifact_v, data, data_v, "uses"),
        TraceLink(PROJECT, action, action_v, artifact, artifact_v, "uses"),
        TraceLink(PROJECT, agent, agent_v, action, action_v, "executes"),
    )
    trace = ProjectProvenanceTrace(
        PROJECT, 5, records, links,
        (_validation(data, data_v, "verified"), _validation(artifact, artifact_v, "rejected"),
         _validation(action, action_v, "verified"), _validation(action, action_v, "inconclusive"),
         _validation(agent, agent_v, "verified"), _validation(hypothesis, hypothesis_v, "verified"),
         _validation(hypothesis, hypothesis_v, "inconclusive")),
    )

    validity = trace.current_validity
    assert validity.for_subject(data, data_v).status == "verified"
    assert validity.for_subject(artifact, artifact_v).status == "invalidated"
    assert validity.for_subject(action, action_v).status == "invalidated"
    assert validity.for_subject(agent, agent_v).status == "invalidated"
    assert validity.for_subject(action, action_v).stable is False
    assert validity.for_subject(hypothesis, hypothesis_v).status == "pending"
    assert validity.for_subject(hypothesis, hypothesis_v).stable is False


def test_shared_verified_ancestor_does_not_look_like_a_dependency_cycle() -> None:
    source, source_v = _subject("data", "source", "r1")
    left, left_v = _subject("experiment", "left", "r1")
    right, right_v = _subject("experiment", "right", "r1")
    joined, joined_v = _subject("artifact", "joined", "r1")
    values = ((source, source_v), (left, left_v), (right, right_v), (joined, joined_v))
    trace = ProjectProvenanceTrace(
        PROJECT,
        4,
        tuple(
            RecordedTraceSubject(subject, version, index)
            for index, (subject, version) in enumerate(values, start=1)
        ),
        (
            TraceLink(PROJECT, left, left_v, source, source_v, "depends_on"),
            TraceLink(PROJECT, right, right_v, source, source_v, "depends_on"),
            TraceLink(PROJECT, joined, joined_v, left, left_v, "depends_on"),
            TraceLink(PROJECT, joined, joined_v, right, right_v, "depends_on"),
        ),
        tuple(_validation(subject, version, "verified") for subject, version in values),
    )

    assert trace.current_validity.for_subject(joined, joined_v).stable is True
