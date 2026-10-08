"""Current read eligibility must not rewrite recorded history."""
from dataclasses import replace

import pytest

from backend.recognition import RecognitionConflict, RecognitionService
from backend.recognition.restructuring import RestructureProposalService
from backend.memory_app.source_egress import SourceEgressService
from core.storage_provider import SQLiteStructuredRecordStore
from tests.recognition.test_artifact_dependencies import env, _chain, _root


def _legacy_missed_cascade(env, chain):
    with env.records.begin() as tx:
        for collection, item_id, state in (
            ("recognition_experiences", chain.original, "revoked"),
            ("recognitions", chain.root.id, "stale"),
        ):
            row = tx.read(collection, item_id)
            tx.put(collection, item_id, {**row.payload, "state": state}, expected_revision=row.revision)
        tx.commit()


def test_legacy_missed_cascade_has_read_only_current_qualification(env):
    chain = _chain(env)
    _, unrelated = _root(env, "unrelated")
    markdown = env.service.export_markdown(scope=env.scope, recognition_id=chain.recognitions[-1].id)
    _legacy_missed_cascade(env, chain)
    before = env.records.list_all()
    for service in (env.service, RecognitionService(SQLiteStructuredRecordStore(env.records.database_path))):
        for original in chain.recognitions:
            current = service.get_recognition(scope=env.scope, recognition_id=original.id)
            assert current.state == "active" and current.revision == original.revision
            assert current.effective_state == "stale" and not current.authorized
            assert current.evidence_eligible is False and 0 < len(current.evidence_reason) <= 240
            projection = current.retrieval_projection()
            assert projection["recorded_state"] == "active" and projection["status"] == "stale"
        assert [item["id"] for item in service.retrieval_entries(scope=env.scope)] == [unrelated.id]
        questions = service.list_questions(scope=env.scope)
        assert all(q.state == "current" and q.effective_state == "stale" for q in questions)
        assert all(not q.evidence_eligible and q.evidence_reason for q in questions)
        assert service.list_questions(scope=env.scope, include_stale=False) == ()
        assert service.export_markdown(scope=env.scope, recognition_id=chain.recognitions[-1].id) == markdown
        unchanged_import = service.markdown_commit(scope=env.scope, markdown=markdown)
        assert not unchanged_import.authorized and unchanged_import.state == "active"
    assert env.records.list_all() == before


def test_reads_use_one_read_snapshot_and_do_not_wait_for_a_writer(env, monkeypatch):
    chain = _chain(env)
    original_order = [row.object_id for row in env.records.list("recognitions")]
    connections, statements = [], []
    connect = env.records._connect
    def tracked_connect():
        connection = connect()
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection
    # Existing writer is intentionally left open. WAL readers must still work.
    with env.records.begin():
        monkeypatch.setattr(env.records, "_connect", tracked_connect)
        monkeypatch.setattr(env.records, "begin", lambda: pytest.fail("read opened a write transaction"))
        rows = env.service.list_recognitions(scope=env.scope)
        assert [row.id for row in rows] == original_order
        assert all(row.authorized for row in rows)
        assert len(connections) == 1
    assert not any("BEGIN IMMEDIATE" in query.upper() for query in statements)
    for artifact in chain.artifacts:
        matching = [query for query in statements if "SELECT collection, object_id" in query
                    and "WHERE collection = 'recognition_experiences'" in query
                    and "object_id = '" + artifact.id + "'" in query]
        assert len(matching) <= 1


def test_question_checks_frozen_revision_even_if_recognition_remains_eligible(env):
    _, recognition = _root(env)
    env.service.upsert_question(scope=env.scope, question_id="q", question="Q", content="A",
        recognition_ids=[recognition.id], source_revisions={recognition.id: 1}, expected_revision=0)
    with env.records.begin() as tx:
        row = tx.read("recognitions", recognition.id)
        tx.put("recognitions", row.object_id, {**row.payload, "content": "corrected"}, expected_revision=row.revision)
        tx.commit()
    assert env.service.get_recognition(scope=env.scope, recognition_id=recognition.id).authorized
    question = env.service.list_questions(scope=env.scope)[0]
    assert question.state == "current" and question.effective_state == "stale"


def test_external_permission_change_does_not_change_fact_eligibility(env):
    chain = _chain(env)
    SourceEgressService(env.records).set_policy(env.scope, "experience", chain.original, 1, 1, [])
    assert all(item.authorized for item in env.service.list_recognitions(scope=env.scope))
    assert all(q.effective_state == "current" for q in env.service.list_questions(scope=env.scope))
    for artifact in chain.artifacts:
        edited = env.documents.save_user_edit(artifact.document["id"], markdown="edited", expected_revision=1)
        env.documents.archive(artifact.document["id"], expected_revision=edited["revision"])
    assert all(item.authorized for item in env.service.list_recognitions(scope=env.scope))


def test_unassessed_dto_does_not_authorize_current_use(env):
    _, recognition = _root(env)
    assert not replace(recognition, evidence_eligible=False).authorized


def test_write_results_share_current_qualification(env):
    _, recognition = _root(env)
    assert recognition.authorized and recognition.evidence_reason is None
    revised = env.service.revise(scope=env.scope, recognition_id=recognition.id,
        expected_revision=recognition.revision, content="revised")
    assert revised.authorized
    children = env.service.split(scope=env.scope, recognition_id=revised.id,
        expected_revision=revised.revision, parts=["first part", "second part"])
    assert all(child.authorized for child in children)
    merged = env.service.merge(scope=env.scope, recognition_ids=[child.id for child in children],
        expected_revisions={child.id: child.revision for child in children}, content="merged")
    assert merged.authorized and merged.status == "active"
    markdown = env.service.export_markdown(scope=env.scope, recognition_id=merged.id)
    assert env.service.markdown_commit(scope=env.scope, markdown=markdown).authorized
    question = env.service.upsert_question(scope=env.scope, question_id="valid-q", question="Q", content="A",
        recognition_ids=[merged.id], source_revisions={merged.id: merged.revision}, expected_revision=0)
    assert question.evidence_eligible and question.effective_state == "current"
    revoked = env.service.revoke(scope=env.scope, recognition_id=merged.id,
        expected_revision=merged.revision, reason="retired")
    assert revoked.state == "revoked" and not revoked.authorized


def test_descriptive_live_user_statement_refs_do_not_gain_new_evidence_semantics(env):
    experience = env.service.stage_experience(scope=env.scope, content="user assertion",
        provenance={"kind": "user_statement", "source_refs": [{"type": "turn", "id": "separate-turn-store"}]})
    candidate = env.service.propose(scope=env.scope, content="adopted", source_experience_ids=[experience])
    recognition = env.service.publish(scope=env.scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer="user")
    assert recognition.authorized


@pytest.mark.parametrize("operation", ["revise", "revoke", "noop"])
def test_frozen_restructure_save_rechecks_hidden_artifact_ancestors(env, operation):
    chain = _chain(env)
    target = chain.recognitions[-1]
    proposals = RestructureProposalService(env.service)
    snapshot = proposals.capture(scope=env.scope, recognition_ids=[target.id], expected_revisions={target.id: 1})
    _legacy_missed_cascade(env, chain)
    before = env.records.list_all()
    outputs = [] if operation != "revise" else [{"content": "new", "conditions": [],
        "source_experience_ids": [chain.artifacts[-1].id], "source_recognition_ids": []}]
    with pytest.raises(RecognitionConflict):
        proposals.save(scope=env.scope, proposal_id="stale-proposal", snapshot=snapshot,
                       operation=operation, outputs=outputs, reason="old frozen input")
    assert env.records.list_all() == before
