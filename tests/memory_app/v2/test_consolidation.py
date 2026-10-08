import json
from datetime import datetime, timezone, timedelta

import pytest
from tests.memory_app.v2.kernel_receipts import wire_receipts, requests

from backend.memory_app.v2.consolidation import Consolidation
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, add_document, publish

env = _env_fixture


class PatternModel:
    def __init__(self, *, allowed=True):
        self.allowed, self.calls = allowed, 0
        self.before = self.after = lambda: None

    def public(self):
        return {
            "generation": {
                "configured": True,
                "enabled": True,
                "allow_remote": self.allowed,
                "base_url": "https://example.invalid/v1",
                "revision": 1,
                "model": "fake",
            },
            "generation_mode": {"revision": 1},
        }

    def complete(self, messages, *, max_tokens, validate_current, wire_attempt_sink=None):
        self.before()
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt() if wire_attempt_sink else None

        def wire():
            self.calls += 1
            payload = {"text": "缓存按需加载减少重复请求", "conditions": ["访问频繁时"]}
            if any("最多300字" in message["content"] for message in messages):
                payload = {"text": "缓存按需加载减少重复请求"}
            text = json.dumps(payload)

            if attempt:
                attempt.succeeded(usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}, cache_observation=None)
            return text

        text = attempt.invoke_wire(wire) if attempt else wire()
        self.after()
        validate_current()
        return text, {"model": "fake", "configuration_revision": 1}


def next_day_job(env, models):
    return Consolidation(
        env.records, env.service, env.documents, models, now=lambda: datetime.now(timezone.utc) + timedelta(days=1)
    )


def documents(env):
    return [
        add_document(
            env,
            summary="缓存按需加载减少重复请求",
            body="缓存按需加载减少重复请求",
            original=f"材料{n} 缓存按需加载减少重复请求",
        )[0]
        for n in range(2)
    ]


def test_pattern_retains_two_sources_pending_first_and_daily_idempotence(env):
    ids = documents(env)
    model = PatternModel()
    job = next_day_job(env, model)
    result = job.run()
    assert result["new_suggestions"] == 1
    patterns = env.records.list("v2_insight_patterns")
    assert len(patterns) == 1
    row = env.records.read("recognition_candidates", patterns[0].object_id)
    assert row.payload["state"] == "pending"
    assert len(row.payload["source_experience_ids"]) == 2
    assert set(patterns[0].payload["document_ids"]) == set(ids)
    assert row.payload["conditions"] == ["访问频繁时"]
    assert env.records.list("recognitions") == ()
    job.run()
    assert model.calls == 2
    assert len(env.records.list("v2_scope_overviews")) == 1
    assert len(env.records.list("v2_insight_patterns")) == 1
    receipt = wire_receipts(env.records)[0]
    assert env.records.list("v2_egress_receipts") == ()
    assert receipt["status"] == "succeeded" and len(requests(env.records)[0]["input"]["refs"]) == 2
    assert "缓存" not in json.dumps(receipt, ensure_ascii=False)
    view = env.http.get("/api/v2/library/insights?project_id=alpha").json()["items"][0]
    assert view["pattern"] is True and view["kind"] == "candidate"


@pytest.mark.parametrize("mode", ["pending", "mixed", "active"])
def test_merge_review_creates_pending_without_rewriting_inputs(env, mode):
    scope = WorkScope("local-user", "alpha")
    eid = env.service.stage_experience(scope=scope, content="Synthetic source")
    rows = [env.service.propose(scope=scope, content="重复认识", source_experience_ids=[eid]) for _ in range(2)]
    if mode != "pending":
        rows[0] = env.service.publish(scope=scope, candidate_id=rows[0].id, expected_revision=1, reviewer="local-user")
    if mode == "active":
        rows[1] = env.service.publish(scope=scope, candidate_id=rows[1].id, expected_revision=1, reviewer="local-user")
    old = [("recognitions" if r.state == "active" else "recognition_candidates", r.id) for r in rows]
    originals = [env.records.read(c, i) for c, i in old]
    service = RestructureProposalService(env.service)
    snapshot = service.capture(
        scope=scope,
        recognition_ids=[r.id for r in rows],
        expected_revisions={r.id: r.revision for r in rows},
        pending_output=True,
    )
    proposal = service.save(
        scope=scope,
        proposal_id="merge-test",
        snapshot=snapshot,
        operation="merge",
        outputs=[
            {"content": "重复认识", "conditions": [], "source_experience_ids": [eid], "source_recognition_ids": []}
        ],
        reason="重复",
    )
    assert proposal["snapshot"]["pending_output"] is True
    reviewed = service.review(
        scope=scope,
        proposal_id=proposal["id"],
        expected_revision=proposal["revision"],
        decision="approve",
        reviewer="local-user",
    )
    result = env.records.read("recognition_candidates", reviewed["result_candidate_ids"][0])
    assert result.payload["state"] == "pending"
    assert [env.records.read(c, i) for c, i in old] == originals
    again = service.review(
        scope=scope,
        proposal_id=proposal["id"],
        expected_revision=proposal["revision"],
        decision="approve",
        reviewer="local-user",
    )
    assert again["result_candidate_ids"] == reviewed["result_candidate_ids"]


def test_existing_insight_gets_reviewable_document_support_not_duplicate(env):
    ids = documents(env)
    old, _ = publish(env, "缓存按需加载减少重复请求")
    before = env.records.read("recognitions", old.id)
    count = len(env.records.list("recognition_candidates"))
    next_day_job(env, PatternModel()).run()
    proposals = env.records.list("v2_insight_evidence_support")
    assert len(proposals) == 1
    row = proposals[0]
    assert row.payload["target_id"] == old.id and row.payload["state"] == "pending"
    assert {d["id"] for d in row.payload["documents"]} == set(ids)
    assert len(env.records.list("recognition_candidates")) == count
    response = env.http.post(
        f"/api/v2/library/evidence-support/{row.object_id}/accept",
        json={"project_id": "alpha", "expected_revision": row.revision},
    )
    assert response.status_code == 200
    assert env.records.read("recognitions", old.id) == before
    assert env.records.read("v2_insight_evidence_support", row.object_id).payload["state"] == "approved"


def test_no_model_keeps_related_and_merge_suggestions_but_no_pattern(env):
    documents(env)
    a, _ = publish(env, "缓存按需加载减少重复请求")
    b, _ = publish(env, "缓存按需加载减少重复请求")
    next_day_job(env, None).run()
    assert len(env.records.list("recognition_restructure_proposals")) == 1
    assert env.records.list("v2_insight_links")
    assert not env.records.list("v2_insight_patterns")
    assert env.records.read("recognitions", a.id).payload["state"] == "active"
    assert env.records.read("recognitions", b.id).payload["state"] == "active"


def test_disabled_remote_and_changed_sources_never_generate_pattern(env):
    ids = documents(env)
    model = PatternModel(allowed=False)
    next_day_job(env, model).run()
    assert model.calls == 0 and not env.records.list("v2_insight_patterns")
    model.allowed = True

    def changed():
        doc = env.documents.read(ids[0])
        env.documents.save_user_edit(ids[0], expected_revision=doc["revision"], markdown="# changed")

    model.after = changed
    # A fresh date permits retrying processing, while input changes stay guarded.
    job = Consolidation(
        env.records,
        env.service,
        env.documents,
        model,
        now=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc),
        recent_days=40000,
    )
    job.run()
    assert not env.records.list("v2_insight_patterns")


def make_merge(env):
    scope = WorkScope("local-user", "alpha")
    eid = env.service.stage_experience(scope=scope, content="source")
    rows = [env.service.propose(scope=scope, content="duplicate", source_experience_ids=[eid]) for _ in range(2)]
    authority = RestructureProposalService(env.service)
    snapshot = authority.capture(
        scope=scope,
        recognition_ids=[r.id for r in rows],
        expected_revisions={r.id: r.revision for r in rows},
        pending_output=True,
    )

    def save(identity):
        return authority.save(
            scope=scope,
            proposal_id=identity,
            snapshot=snapshot,
            operation="merge",
            outputs=[
                {"content": "duplicate", "conditions": [], "source_experience_ids": [eid], "source_recognition_ids": []}
            ],
            reason="duplicate",
        )

    return scope, rows, authority, snapshot, save


def test_foreign_candidate_cannot_be_smuggled_into_snapshot(env):
    from copy import deepcopy
    from backend.recognition import RecognitionError

    scope, rows, authority, snapshot, _ = make_merge(env)
    rogue = env.service.propose(scope=scope, content="other", source_experience_ids=rows[0].source_experience_ids)
    altered = deepcopy(snapshot)
    stored = env.records.read("recognition_candidates", rogue.id)
    altered["candidates"].append({"id": rogue.id, "revision": stored.revision, "payload": stored.payload})
    with pytest.raises(RecognitionError):
        authority.save(
            scope=scope,
            proposal_id="forged",
            snapshot=altered,
            operation="merge",
            outputs=[
                {
                    "content": "duplicate",
                    "conditions": [],
                    "source_experience_ids": list(rows[0].source_experience_ids),
                    "source_recognition_ids": [],
                }
            ],
            reason="x",
        )


def test_two_merge_proposals_cannot_consume_same_candidate_and_old_inputs_are_traceable(env):
    scope, rows, authority, _, save = make_merge(env)
    first, second = save("first"), save("second")
    result = authority.review(
        scope=scope,
        proposal_id=first["id"],
        expected_revision=first["revision"],
        decision="approve",
        reviewer="local-user",
    )
    with pytest.raises(RecognitionConflict):
        authority.review(
            scope=scope,
            proposal_id=second["id"],
            expected_revision=second["revision"],
            decision="approve",
            reviewer="local-user",
        )
    with pytest.raises(RecognitionConflict):
        env.service.publish(scope=scope, candidate_id=rows[0].id, expected_revision=1, reviewer="local-user")
    items = env.http.get("/api/v2/library/insights?project_id=alpha").json()["items"]
    merged = next(r for r in items if r["id"] == result["result_candidate_ids"][0])
    assert {r["id"] for r in merged["merged_from"]} == {r.id for r in rows}
    assert not {r.id for r in rows} & {r["id"] for r in items}
    assert not {r.id for r in rows} & {r.id for r in env.service.list_candidates(scope=scope)}


def test_merge_candidate_and_terminal_state_roll_back_together(env, monkeypatch):
    from core.storage_provider import SQLiteStructuredRecordUnitOfWork

    scope, _, authority, _, save = make_merge(env)
    proposal = save("atomic")
    original = SQLiteStructuredRecordUnitOfWork.put

    def failed(self, collection, identity, payload, **kwargs):
        if collection == "recognition_restructure_proposals" and payload.get("state") == "approved":
            raise RuntimeError("synthetic transaction failure")
        return original(self, collection, identity, payload, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", failed)
    before = env.records.list("recognition_candidates")
    with pytest.raises(RuntimeError):
        authority.review(
            scope=scope, proposal_id=proposal["id"], expected_revision=1, decision="approve", reviewer="local-user"
        )
    assert env.records.list("recognition_candidates") == before
    assert env.records.read("recognition_restructure_proposals", proposal["id"]).payload["state"] == "pending"
    assert not env.records.list("v2_candidate_merges")


def test_private_change_invalidates_pending_merge_approval(env):
    from backend.memory_app.v2.privacy import set_private_project

    scope, _, authority, _, save = make_merge(env)
    proposal = save("private-change")
    set_private_project(env.records, "alpha", True, 0)
    with pytest.raises(RecognitionConflict):
        authority.review(
            scope=scope, proposal_id=proposal["id"], expected_revision=1, decision="approve", reviewer="local-user"
        )


def test_manual_document_forget_cannot_reenter_through_candidate_or_support(env):
    from backend.memory_app.document_recognition import ensure_document_experience

    ids = documents(env)
    scope = WorkScope("local-user", "alpha")
    experience = ensure_document_experience(env.documents, env.service, "alpha", ids[0])[0]
    env.service.propose(scope=scope, content="缓存按需加载减少重复请求", source_experience_ids=[experience])
    with env.records.begin() as tx:
        tx.put("v2_document_recall", ids[0], {"state": "forgotten", "by": "user"}, expected_revision=0)
        tx.commit()
    model = PatternModel()
    next_day_job(env, model).run()
    assert model.calls == 1 and not env.records.list("v2_insight_patterns")
    assert env.records.list("v2_scope_overviews")[0].payload["source_document_ids"] == [ids[1]]


def test_recognition_chain_retains_document_sources_in_merge(env):
    ids = documents(env)
    parents = [publish(env, "缓存按需加载减少重复请求", doc=identity)[0] for identity in ids]
    scope = WorkScope("local-user", "alpha")
    rows = [
        env.service.propose(
            scope=scope, content="缓存按需加载减少重复请求", source_experience_ids=[], source_recognition_ids=[r.id]
        )
        for r in parents
    ]
    next_day_job(env, None).run()
    # Consolidation pairs every duplicate input at most once; validate source retention
    # on the explicit candidate pair as well as all automatically generated previews.
    authority = RestructureProposalService(env.service)
    snapshot = authority.capture(
        scope=scope,
        recognition_ids=[r.id for r in rows],
        expected_revisions={r.id: 1 for r in rows},
        pending_output=True,
    )
    assert len(snapshot["experiences"]) == 2 and len(snapshot["recognitions"]) == 2
    saved = authority.save(
        scope=scope,
        proposal_id="chain",
        snapshot=snapshot,
        operation="merge",
        outputs=[
            {
                "content": "缓存按需加载减少重复请求",
                "conditions": [],
                "source_experience_ids": [r["id"] for r in snapshot["experiences"]],
                "source_recognition_ids": [r.id for r in parents],
            }
        ],
        reason="same",
    )
    reviewed = authority.review(
        scope=scope, proposal_id=saved["id"], expected_revision=1, decision="approve", reviewer="local-user"
    )
    candidate = env.records.read("recognition_candidates", reviewed["result_candidate_ids"][0])
    assert set(candidate.payload["source_recognition_ids"]) == {r.id for r in parents}
    assert len(candidate.payload["source_experience_ids"]) == 2


def test_concurrent_daily_runs_have_only_one_semantic_effect(env):
    from concurrent.futures import ThreadPoolExecutor

    documents(env)
    model = PatternModel()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: next_day_job(env, model).run(), range(2)))
    assert sum(r["new_suggestions"] for r in results) == 1
    assert model.calls == 2 and len(env.records.list("v2_insight_patterns")) == 1
    assert len(env.records.list("v2_scope_overviews")) == 1


def test_expired_run_can_resume_and_taken_over_owner_cannot_write(env):
    documents(env)
    job = next_day_job(env, PatternModel())
    day = job.now().date().isoformat()
    with env.records.begin() as tx:
        tx.put(
            "v2_consolidation_runs",
            day,
            {"status": "running", "started_at": (job.now() - timedelta(minutes=11)).isoformat()},
            expected_revision=0,
        )
        tx.commit()
    assert job.run()["new_suggestions"] == 1
    assert env.records.read("v2_consolidation_runs", day).payload["status"] == "completed"
    # A different day's old worker must stop after another owner takes its lease.
    model = PatternModel()
    job = Consolidation(
        env.records, env.service, env.documents, model, now=lambda: datetime.now(timezone.utc) + timedelta(days=2)
    )
    documents(env)

    def takeover():
        day, old = job._reservation
        with env.records.begin() as tx:
            tx.put(
                "v2_consolidation_runs",
                day,
                {"status": "running", "started_at": job.now().isoformat(), "new_owner": True},
                expected_revision=old.revision,
            )
            tx.commit()

    model.before = takeover
    before = env.records.list("v2_insight_patterns")
    with pytest.raises(RecognitionConflict):
        job.run()
    assert model.calls == 0 and env.records.list("v2_insight_patterns") == before


def test_persona_support_can_be_reviewed_in_its_real_source_project(env):
    ids = documents(env)
    old, _ = publish(env, "缓存按需加载减少重复请求", project="me")
    next_day_job(env, PatternModel()).run()
    response = env.http.get(f"/api/v2/library/insights/{old.id}/evidence-support?project_id=me")
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["project_id"] == "alpha" and row["target_project_id"] == "me"
    assert {d["id"] for d in row["documents"]} == set(ids)
    accepted = env.http.post(
        f"/api/v2/library/evidence-support/{row['id']}/accept",
        json={"project_id": row["project_id"], "expected_revision": row["revision"]},
    )
    assert accepted.status_code == 200
    document = env.http.get(
        "/api/v2/library/drill", params={"project_id": row["project_id"], "from": "note", "id": ids[0]}
    )
    assert document.status_code == 200 and document.json()["note"]["document_id"] == ids[0]
