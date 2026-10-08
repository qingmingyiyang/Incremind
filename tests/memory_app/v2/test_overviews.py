import json
from datetime import datetime, timezone

import pytest
from backend.memory_app.v2.overviews import ScopeOverviews
from backend.memory_app.v2.projects import assign_scene
from tests.memory_app.v2.test_workbench_ask import env as env, add_document
from tests.memory_app.v2.test_consolidation import PatternModel
from tests.memory_app.v2.kernel_receipts import requests


class OverviewModel(PatternModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.inputs = []

    def complete(self, messages, *, max_tokens, validate_current, wire_attempt_sink=None):
        self.before()
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            self.calls += 1
            self.inputs.append(messages)
            attempt.succeeded(usage={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}, cache_observation=None)
            return json.dumps({"text": "桥梁预算已确认，设备验收已完成。"}, ensure_ascii=False)
        result = attempt.invoke_wire(wire)
        self.after()
        validate_current()
        return result, {"model": "fake", "configuration_revision": 1}


def test_scope_overview_is_summary_only_aux_and_revision_idempotent(env):
    doc, _ = add_document(env, summary="桥梁预算已确认", body="正文机密标记", original="真实原件标记")
    model = OverviewModel()
    service = ScopeOverviews(env.records, env.documents, model)
    result = service.update("alpha")
    assert result["source_document_ids"] == [doc]
    assert len(result["text"]) <= 300
    payload = json.dumps(model.inputs, ensure_ascii=False)
    assert "桥梁预算已确认" in payload
    assert "正文机密标记" not in payload and "真实原件标记" not in payload
    assert model.calls == 1
    assert service.update("alpha") == result and model.calls == 1
    turn = requests(env.records)[0]
    assert turn["desired_outcome"] == "memory.overview"
    assert turn["execution_policy"]["purpose"] == "aux"
    assert turn["capability_policy"]["allowed"] == []


def test_closed_egress_is_silent_and_does_not_create_turn(env):
    add_document(env, summary="桥梁预算")
    model = OverviewModel(allowed=False)
    assert ScopeOverviews(env.records, env.documents, model).update("alpha") is None
    assert model.calls == 0 and requests(env.records) == []


def test_scene_overview_uses_only_current_scene_and_invalidates_on_move(env):
    first, _ = add_document(env, summary="桥梁预算")
    second, _ = add_document(env, summary="设备验收")
    assign_scene(env.records, "document", first, "alpha", "工程")
    assign_scene(env.records, "document", second, "alpha", "其他")
    service = ScopeOverviews(env.records, env.documents, OverviewModel())
    assert service.update("alpha", "工程")["source_document_ids"] == [first]
    assert service.current("alpha", "工程")
    assign_scene(env.records, "document", second, "alpha", "工程")
    assert service.current("alpha", "工程") is None


def test_overview_navigation_adds_real_zero_keyword_summaries_without_changing_coverage(env):
    first, _ = add_document(env, summary="桥梁预算已确认", body="合成正文甲")
    second, _ = add_document(env, summary="设备验收已完成", body="合成正文乙")
    model = OverviewModel()
    service = ScopeOverviews(env.records, env.documents, model)
    service.update("alpha")
    query = env.domains.query
    question = "最近在忙什么？"
    assert query.collect_candidates("alpha", question)["candidates"] == []
    plan = query.prepare_ask("alpha", question)
    assert {c["entry"]["id"] for c in plan["chosen"]} == {first, second}
    assert all(c["kind"] == "document" and c["layer"] == "L2" for c in plan["chosen"])
    assert all(query.documents.markdown(c["entry"]["id"])[w.start:w.end] == w.text
               for c in plan["chosen"] for w in c["windows"])
    assert all(row["coverage"] == 0 and row["stopped"] is False for row in plan["trace"])
    assert model.calls == 1
    query.validate_ask_plan(plan)


def test_navigation_stale_new_document_returns_original_path(env):
    add_document(env, summary="桥梁预算已确认")
    service = ScopeOverviews(env.records, env.documents, OverviewModel())
    service.update("alpha")
    add_document(env, summary="新设备预算")
    query = env.domains.query
    assert query.prepare_ask("alpha", "最近在忙什么？")["chosen"] == []


def test_scope_change_after_planning_rejects_old_navigation(env):
    from backend.recognition import RecognitionConflict
    add_document(env, summary="桥梁预算已确认")
    ScopeOverviews(env.records, env.documents, OverviewModel()).update("alpha")
    query = env.domains.query
    plan = query.prepare_ask("alpha", "最近在忙什么？")
    assert plan["chosen"]
    add_document(env, summary="另一份新资料")
    with pytest.raises(RecognitionConflict, match="scope_overview"):
        query.validate_ask_plan(plan)


def test_closed_egress_keeps_existing_lexical_path_without_cached_navigation(env):
    add_document(env, summary="桥梁预算已确认")
    ScopeOverviews(env.records, env.documents, OverviewModel()).update("alpha")
    query = env.domains.query
    question = "最近在忙什么？"
    assert query.prepare_ask("alpha", question)["chosen"]
    env.model.allowed = False
    assert query.prepare_ask("alpha", question)["chosen"] == []
    assert query.prepare_ask("alpha", "桥梁预算")["chosen"]


def test_private_original_and_empty_summary_never_enter_inputs(env):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    public, _ = add_document(env, summary="桥梁预算已确认")
    private, item = add_document(env, summary="私密摘要禁止发送")
    add_document(env, summary="", body="无摘要正文禁止回退")
    row = env.records.read("workspace_items", item)
    SourceEgressService(env.records).set_policy(WorkScope("local-user", "alpha"), "original_item", item, row.revision, 0, [])
    model = OverviewModel()
    result = ScopeOverviews(env.records, env.documents, model).update("alpha")
    assert result["source_document_ids"] == [public]
    assert private not in result["source_document_ids"]
    assert "私密摘要禁止发送" not in json.dumps(model.inputs, ensure_ascii=False)
    assert "无摘要正文禁止回退" not in json.dumps(model.inputs, ensure_ascii=False)


@pytest.mark.parametrize("point", ["before", "after"])
def test_permission_revocation_prevents_dispatch_or_commit(env, point):
    from backend.memory_app.v2.privacy import set_private_project
    from backend.recognition import RecognitionConflict
    add_document(env, summary="桥梁预算已确认")
    model = OverviewModel()
    setattr(model, point, lambda: set_private_project(env.records, "alpha", True, 0))
    with pytest.raises(RecognitionConflict):
        ScopeOverviews(env.records, env.documents, model).update("alpha")
    assert model.calls == (0 if point == "before" else 1)
    assert env.records.list("v2_scope_overviews") == ()


def test_document_change_during_generation_cannot_commit_old_overview(env):
    from backend.recognition import RecognitionConflict
    doc, _ = add_document(env, summary="桥梁预算已确认")
    model = OverviewModel()
    model.after = lambda: env.documents.save_user_edit(doc, expected_revision=2,
        markdown="# Synthetic\n\n## 摘要\n新的预算\n\n## 正文\n正文")
    with pytest.raises(RecognitionConflict):
        ScopeOverviews(env.records, env.documents, model).update("alpha")
    assert env.records.list("v2_scope_overviews") == ()


def test_daily_job_updates_project_and_scene_once_then_reuses_next_day(env):
    from datetime import timedelta
    from backend.memory_app.v2.consolidation import Consolidation
    doc, _ = add_document(env, summary="桥梁预算已确认")
    assign_scene(env.records, "document", doc, "alpha", "工程")
    model = OverviewModel()
    now = datetime.now(timezone.utc)
    for days in (1, 2):
        result = Consolidation(env.records, env.service, env.documents, model,
                               now=lambda: now + timedelta(days=days)).run()
        assert result["failed_groups"] == 0
    assert model.calls == 2
    assert len(env.records.list("v2_scope_overviews")) == 2


def test_fixed_overview_fixture_uses_real_turns_and_real_l2(tmp_path):
    import sqlite3
    from pathlib import Path
    from tools.memory_eval import seed
    from backend.memory_app.model_config import ModelConfiguration
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.llm.litellm_gateway import _estimate_input_tokens
    from tests.memory_app.v2.kernel_receipts import wire_receipts
    fixture = json.loads((Path(__file__).parents[2] / "fixtures/memory_eval/overview_navigation.json").read_text(encoding="utf-8"))
    keeper = sqlite3.connect(tmp_path / "records.sqlite3")
    calls = []
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        query, identities = seed(tmp_path, fixture)
        names = {actual: logical for logical, actual in identities.items()}
        for document in fixture["documents"]:
            if document.get("scene"):
                assign_scene(query.records, "document", identities[document["id"]], document["project_id"], document["scene"])
        def wire(**kwargs):
            calls.append(kwargs["messages"])
            materials = json.loads(next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "user"))
            text = "".join(m["summary"] for m in materials if not any(mark in m["summary"] for mark in ("旧", "已归档")))[:300]
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"text": text}, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}}
        models = ModelConfiguration(query.records, tmp_path, InMemorySecretStore(), completion_fn=wire)
        models.update("generation", {"base_url": "https://example.invalid/v1", "model": "synthetic-overview",
            "api_key": "synthetic-only", "allow_remote": True, "expected_revision": 0})
        query.models = models
        generator = ScopeOverviews(query.records, query.documents, models)
        for question in fixture["questions"]:
            assert generator.update(question["project_id"], question.get("scene"))
        tokens = 0
        for question in fixture["questions"]:
            plan = query.prepare_ask(question["project_id"], question["question"], scene=question.get("scene"))
            for identity, answer in question["expected_evidence"].items():
                assert any(c["layer"] == "L2" and names.get(c["entry"]["id"]) == identity
                           and answer in c["excerpt"] for c in plan["chosen"])
            for candidate in plan["chosen"]:
                content = (query.documents.markdown(candidate["entry"]["id"]) if candidate["kind"] == "document" else candidate["entry"]["content"])
                assert all(content[w.start:w.end] == w.text for w in candidate["windows"])
            query.validate_ask_plan(plan)
            tokens += _estimate_input_tokens([{"role": "user", "content": "\n\n".join(c["excerpt"] for c in plan["chosen"])}])
        # Frozen before has no gold summaries and 3791 total evidence tokens.
        assert tokens < 3791
        assert len(calls) == len(requests(query.records)) == 3
        assert all(r["desired_outcome"] == "memory.overview" and r["execution_policy"]["purpose"] == "aux" for r in requests(query.records))
        assert len(wire_receipts(query.records)) == 3
        assert all(receipt["status"] == "succeeded" for receipt in wire_receipts(query.records))
    finally:
        keeper.close()


def test_actual_ask_uses_navigated_summary_citations(env):
    from tests.memory_app.v2.test_workbench_ask import ask
    add_document(env, summary="桥梁预算已确认", body="合成正文甲")
    add_document(env, summary="设备验收已完成", body="合成正文乙")
    ScopeOverviews(env.records, env.documents, OverviewModel()).update("alpha")
    response = ask(env, text="最近在忙什么？")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is False
    assert receipt["layers"]["summary"] == 2
    assert receipt["citations"]
    assert "v2_scope_overviews" not in json.dumps(receipt)
