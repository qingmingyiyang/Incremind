"""Artifact evidence eligibility is separate from completion and egress grants."""
from types import SimpleNamespace

import pytest

from backend.memory_app.packet_egress import capture_packet_egress
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "artifacts.sqlite3")
    return SimpleNamespace(records=records, service=RecognitionService(records),
                           scope=WorkScope("user", "project"),
                           documents=SQLiteDocumentRepository(records, namespace_id="recognition"))


def _publish(env, name, *, experiences=(), recognitions=(), scope=None):
    scope = scope or env.scope
    candidate = env.service.propose(scope=scope, content=name,
        source_experience_ids=experiences, source_recognition_ids=recognitions)
    return env.service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer="user", recognition_id=name)


def _root(env, name="root", *, scope=None):
    scope = scope or env.scope
    experience = env.service.stage_experience(scope=scope, content="original evidence",
        experience_id="evidence-" + name, provenance={"kind": "user_statement"})
    SourceEgressService(env.records).set_policy(scope, "experience", experience, 1, 0, ["generation", "embedding", "rerank"])
    return experience, _publish(env, name, experiences=[experience], scope=scope)


def _retain(env, name, roots, *, scope=None):
    scope = scope or env.scope
    task_id, packet_id = "task-" + name, "packet-" + name
    packet = {"id": packet_id, "project_id": scope.project_id, "kind": "context",
              "query": name, "items": [{"id": row.id, "revision": row.revision} for row in roots]}
    packet["source_egress"] = capture_packet_egress(env.service, scope, packet)
    packet.update(state="consumed", task_id=task_id)
    document = env.documents.create(DocumentDraft(title=name, document_type="agent-result",
        markdown="historical model output " + name, project_id=scope.project_id,
        source_refs=({"source_id": task_id, "locator": "task://" + task_id},)))
    with env.records.begin() as tx:
        packet_record = tx.put("recognition_context_packets", packet_id, packet, expected_revision=0)
        task = tx.put("recognition_tasks", task_id, {"id": task_id, "project_id": scope.project_id,
            "state": "completed", "document_id": document["id"], "context_packet_id": packet_id},
            expected_revision=0)
        tx.commit()
    experience_id = "experience-" + task_id + "-r1"
    env.service.stage_experience(scope=scope, experience_id=experience_id,
        content="用户选择保留的模型生成成果，内容尚需人工事实核验。\n来源：task://" + task_id,
        provenance={"kind": "model_generated_artifact", "actor": "agent", "source_refs": [
            {"type": "task", "id": task_id, "revision": task.revision},
            {"type": "document", "id": document["id"], "revision": document["revision"]},
            {"type": "context_packet", "id": packet_id, "revision": packet_record.revision}]})
    return SimpleNamespace(id=experience_id, document=document, task_id=task_id, packet_id=packet_id)


def _chain(env):
    original, root = _root(env)
    first = _retain(env, "first", [root])
    child = _publish(env, "child", experiences=[first.id])
    second = _retain(env, "second", [child])
    grandchild = _publish(env, "grandchild", experiences=[second.id])
    candidates = [env.service.propose(scope=env.scope, content="pending " + item.id,
        source_experience_ids=[item.id]) for item in (first, second)]
    for recognition in (child, grandchild):
        env.service.upsert_question(scope=env.scope, question_id="question-" + recognition.id,
            question="what next", content=recognition.content, recognition_ids=[recognition.id],
            source_revisions={recognition.id: recognition.revision}, expected_revision=0)
    return SimpleNamespace(original=original, root=root, artifacts=(first, second),
                           recognitions=(child, grandchild), candidates=candidates)


def _change_root(env, chain, operation):
    if operation == "experience_revoke":
        env.service.revoke_experience(scope=env.scope, experience_id=chain.original, expected_revision=1)
    elif operation == "recognition_revoke":
        env.service.revoke(scope=env.scope, recognition_id=chain.root.id, expected_revision=1, reason="corrected")
    else:
        env.service.revise(scope=env.scope, recognition_id=chain.root.id, expected_revision=1,
                           content="revised evidence conclusion")


def _assert_ineligible(env, chain):
    for recognition in chain.recognitions:
        assert env.service.get_recognition(scope=env.scope, recognition_id=recognition.id).state == "stale"
    assert all(question.state == "stale" for question in env.service.list_questions(scope=env.scope))
    assert not {r.id for r in chain.recognitions}.intersection(
        row["id"] for row in env.service.retrieval_entries(scope=env.scope))
    for candidate in chain.candidates:
        current = env.records.read("recognition_candidates", candidate.id)
        assert current.payload["state"] == "invalidated"
        with pytest.raises(RecognitionConflict):
            env.service.publish(scope=env.scope, candidate_id=candidate.id,
                expected_revision=current.revision, reviewer="user")
    for artifact in chain.artifacts:
        with pytest.raises(RecognitionConflict):
            env.service.propose(scope=env.scope, content="new proposal", source_experience_ids=[artifact.id])


@pytest.mark.parametrize("operation", ["experience_revoke", "recognition_revise", "recognition_revoke"])
def test_two_artifact_generations_invalidate_evidence_without_rewriting_history(env, operation):
    chain = _chain(env)
    preserved_collections = ("recognition_experiences", "recognition_tasks", "recognition_context_packets",
                             "documents", "document_revisions", "document_markdown")
    history = {collection: tuple(row for row in env.records.list(collection)
        if row.object_id != chain.original) for collection in preserved_collections}
    _change_root(env, chain, operation)
    _assert_ineligible(env, chain)
    assert history == {collection: tuple(row for row in env.records.list(collection)
        if row.object_id != chain.original) for collection in preserved_collections}
    for artifact in chain.artifacts:
        payload = env.records.read("recognition_experiences", artifact.id).payload
        assert payload["state"] == "active"  # Retention/completion is still a historical fact.
        assert payload["provenance"]["artifact_status"] == "committed"
        assert env.records.read("recognition_tasks", artifact.task_id).payload["state"] == "completed"
    reopened = SQLiteStructuredRecordStore(env.records.database_path)
    env.records, env.service = reopened, RecognitionService(reopened)
    _assert_ineligible(env, chain)


def test_egress_policy_changes_and_document_edits_do_not_invalidate_evidence(env):
    chain = _chain(env)
    before = {name: env.records.list(name) for name in (
        "recognition_experiences", "recognitions", "recognition_candidates", "recognition_questions")}
    for artifact in chain.artifacts:
        changed = env.documents.save_user_edit(artifact.document["id"], markdown="later edit", expected_revision=1)
        env.documents.archive(artifact.document["id"], expected_revision=changed["revision"])
    egress = SourceEgressService(env.records)
    egress.set_policy(env.scope, "experience", chain.original, 1, 1, [])
    assert before == {name: env.records.list(name) for name in before}
    for candidate in chain.candidates:
        env.service.publish(scope=env.scope, candidate_id=candidate.id,
                            expected_revision=candidate.revision, reviewer="user")
    snapshot = egress.snapshot(env.scope, [{"type": "experience", "id": chain.artifacts[1].id, "revision": 1}])
    with pytest.raises(RecognitionConflict):
        egress.require(snapshot, "generation")
    egress.set_policy(env.scope, "experience", chain.original, 1, 2, ["generation", "embedding", "rerank"])
    egress.require(egress.snapshot(env.scope,
        [{"type": "experience", "id": chain.artifacts[1].id, "revision": 1}]), "generation")


def test_unrelated_other_project_and_history_only_parent_are_not_evidence_edges(env):
    chain = _chain(env)
    _, unrelated = _root(env, "unrelated")
    foreign = WorkScope("user", "other-project")
    _, foreign_root = _root(env, "foreign-root", scope=foreign)
    other_artifact = _retain(env, "foreign", [foreign_root], scope=foreign)
    _publish(env, "foreign-child", experiences=[other_artifact.id], scope=foreign)
    # Historical restructuring lineage is deliberately not a supporting source.
    with env.records.begin() as tx:
        row = tx.read("recognitions", unrelated.id)
        tx.put("recognitions", unrelated.id, {**row.payload, "parent_ids": [chain.root.id]}, expected_revision=row.revision)
        tx.commit()
    before = env.records.list_all()
    _change_root(env, chain, "recognition_revise")
    _assert_ineligible(env, chain)
    after = env.records.list_all()
    foreign_before = tuple(row for row in before if row.payload.get("project_id") == foreign.project_id)
    assert foreign_before == tuple(row for row in after if row.payload.get("project_id") == foreign.project_id)
    assert env.service.get_recognition(scope=env.scope, recognition_id=unrelated.id).state == "active"


def test_cascade_and_candidate_changes_roll_back_in_the_original_transaction(env, monkeypatch):
    chain = _chain(env)
    before = env.records.list_all()
    invalidate = env.service._invalidate_questions
    def fail_after_writes(*args, **kwargs):
        invalidate(*args, **kwargs)
        raise OSError("injected after all dependent writes")
    monkeypatch.setattr(env.service, "_invalidate_questions", fail_after_writes)
    with pytest.raises(OSError, match="injected"):
        _change_root(env, chain, "recognition_revoke")
    reopened = SQLiteStructuredRecordStore(env.records.database_path)
    assert reopened.list_all() == before
    env.records, env.service = reopened, RecognitionService(reopened)
    _change_root(env, chain, "recognition_revoke")
    _assert_ineligible(env, chain)


def test_pending_only_artifact_dependency_is_invalidated(env):
    _, root = _root(env)
    artifact = _retain(env, "pending-only", [root])
    candidate = env.service.propose(scope=env.scope, content="pending", source_experience_ids=[artifact.id])
    env.service.revoke(scope=env.scope, recognition_id=root.id, expected_revision=1, reason="corrected")
    assert env.records.read("recognition_candidates", candidate.id).payload["state"] == "invalidated"
    assert env.records.read("recognition_experiences", artifact.id).revision == 1


def test_publication_rechecks_nested_evidence_when_old_state_missed_the_cascade(env):
    chain = _chain(env)
    # Reproduce a pre-fix database: the root changed, but its artifact's
    # descendants and candidate rows were left active/pending.
    with env.records.begin() as tx:
        root = tx.read("recognitions", chain.root.id)
        tx.put("recognitions", root.object_id, {**root.payload, "state": "revoked"}, expected_revision=root.revision)
        tx.commit()
    before = env.records.list_all()
    for candidate in chain.candidates:
        with pytest.raises(RecognitionConflict):
            env.service.publish(scope=env.scope, candidate_id=candidate.id,
                                expected_revision=candidate.revision, reviewer="user")
        with pytest.raises(RecognitionConflict):
            env.service.edit_candidate(scope=env.scope, candidate_id=candidate.id,
                expected_revision=candidate.revision, content="edited", editor="user")
    assert env.records.list_all() == before


@pytest.mark.parametrize("operation", ["propose", "publish", "edit", "question"])
def test_direct_recognition_reference_rechecks_legacy_missed_artifact_cascade(env, operation):
    chain = _chain(env)
    candidate = env.service.propose(scope=env.scope, content="derived through recognition",
        source_experience_ids=[], source_recognition_ids=[chain.recognitions[-1].id])
    # State produced by the old revoke: E0 revoked, R0 stale; artifact-derived
    # R1/R2 and a candidate citing R2 remained active/pending.
    with env.records.begin() as tx:
        experience = tx.read("recognition_experiences", chain.original)
        root = tx.read("recognitions", chain.root.id)
        tx.put("recognition_experiences", experience.object_id, {**experience.payload, "state": "revoked"},
               expected_revision=experience.revision)
        tx.put("recognitions", root.object_id, {**root.payload, "state": "stale"}, expected_revision=root.revision)
        tx.commit()
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict):
        if operation == "propose":
            env.service.propose(scope=env.scope, content="newly derived", source_experience_ids=[],
                                source_recognition_ids=[chain.recognitions[-1].id])
        elif operation == "publish":
            env.service.publish(scope=env.scope, candidate_id=candidate.id,
                                expected_revision=candidate.revision, reviewer="user")
        elif operation == "edit":
            env.service.edit_candidate(scope=env.scope, candidate_id=candidate.id,
                expected_revision=candidate.revision, content="edited", editor="user")
        else:
            recognition = chain.recognitions[-1]
            env.service.upsert_question(scope=env.scope, question_id="fresh-question",
                question="what next", content="new synthesis", recognition_ids=[recognition.id],
                source_revisions={recognition.id: recognition.revision}, expected_revision=0)
    assert env.records.list_all() == before


def test_empty_context_model_artifact_can_ground_a_candidate_without_an_egress_grant(env):
    artifact = _retain(env, "empty", [])
    recognition = _publish(env, "independent-result", experiences=[artifact.id])
    assert recognition.state == "active"
    authority = SourceEgressService(env.records)
    refs = [{"type": "experience", "id": artifact.id, "revision": 1}]
    snapshot = authority.snapshot(env.scope, refs)
    for purpose in ("generation", "embedding", "rerank"):
        authority.require(snapshot, purpose)
    authority.set_policy(env.scope, "experience", artifact.id, 1, 0, [])
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(env.scope, snapshot)
    private = authority.snapshot(env.scope, refs)
    for purpose in ("generation", "embedding", "rerank"):
        with pytest.raises(RecognitionConflict):
            authority.require(private, purpose)


def test_verified_references_define_edges_not_experience_names_or_body_mentions(env):
    _, root = _root(env)
    artifact = _retain(env, "original", [root])
    provenance = env.records.read("recognition_experiences", artifact.id).payload["provenance"]
    actual = env.service.stage_experience(scope=env.scope, experience_id="arbitrary-identity",
        content="output without textual pointers", provenance=provenance)
    mention = env.service.stage_experience(scope=env.scope, experience_id="experience-" + artifact.task_id + "-r2",
        content="A user mentioned task://" + artifact.task_id, provenance={"kind": "user_statement"})
    dependent = _publish(env, "actual-dependency", experiences=[actual])
    independent = _publish(env, "only-a-mention", experiences=[mention])
    env.service.revise(scope=env.scope, recognition_id=root.id, expected_revision=1, content="corrected")
    assert env.service.get_recognition(scope=env.scope, recognition_id=dependent.id).state == "stale"
    assert env.service.get_recognition(scope=env.scope, recognition_id=independent.id).state == "active"


def test_cyclic_stored_artifact_evidence_is_rejected_without_writes(env):
    _, root = _root(env)
    artifact = _retain(env, "cycle", [root])
    with env.records.begin() as tx:
        parent = tx.read("recognitions", root.id)
        packet = tx.read("recognition_context_packets", artifact.packet_id)
        experience = tx.read("recognition_experiences", artifact.id)
        provenance = dict(experience.payload["provenance"])
        refs = [{**ref, **({"revision": packet.revision + 1} if ref["type"] == "context_packet" else {})}
                for ref in provenance["source_refs"]]
        tx.put("recognitions", root.id, {**parent.payload, "source_experience_ids": [artifact.id],
            "source_experience_revisions": {artifact.id: experience.revision + 1}}, expected_revision=parent.revision)
        tx.put("recognition_context_packets", artifact.packet_id, {**packet.payload,
            "items": [{"id": root.id, "revision": parent.revision + 1}]}, expected_revision=packet.revision)
        tx.put("recognition_experiences", artifact.id, {**experience.payload,
            "provenance": {**provenance, "source_refs": refs}}, expected_revision=experience.revision)
        tx.commit()
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict, match="cycle"):
        env.service.propose(scope=env.scope, content="cyclic claim", source_experience_ids=[artifact.id])
    assert env.records.list_all() == before


@pytest.mark.parametrize("damage", ["task_binding", "document_binding", "packet_binding", "cross_project"])
def test_unverified_artifact_links_cannot_ground_new_candidates(env, damage):
    _, root = _root(env)
    artifact = _retain(env, "damaged", [root])
    with env.records.begin() as tx:
        row = tx.read("recognition_experiences", artifact.id)
        provenance = dict(row.payload["provenance"])
        refs = [dict(ref) for ref in provenance["source_refs"]]
        if damage == "cross_project":
            payload = {**row.payload, "scope": {"user_id": "user", "project_id": "other-project"},
                       "project_id": "other-project"}
        else:
            kind = {"task_binding": "task", "document_binding": "document", "packet_binding": "context_packet"}[damage]
            next(ref for ref in refs if ref["type"] == kind)["id"] = "missing"
            payload = {**row.payload, "provenance": {**provenance, "source_refs": refs}}
        tx.put("recognition_experiences", artifact.id, payload, expected_revision=row.revision)
        tx.commit()
    scope = WorkScope("user", "other-project") if damage == "cross_project" else env.scope
    with pytest.raises(RecognitionConflict):
        env.service.propose(scope=scope, content="unsupported", source_experience_ids=[artifact.id])
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(scope, [{"type": "experience", "id": artifact.id, "revision": 2}])
    # A damaged unrelated record must not prevent revocation of valid evidence.
    env.service.revoke(scope=env.scope, recognition_id=root.id, expected_revision=1, reason="corrected")
