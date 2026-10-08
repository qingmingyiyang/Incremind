import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.v2.projects import assign_scene, scene_of
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


def response_for(messages, response):
    """Synthetic provider matches the received protocol without repairing errors."""
    try:
        sent, output = json.loads(messages[-1]['content']), json.loads(response)
        if (isinstance(sent, dict) and isinstance(sent.get('neighbors'), list)
                and isinstance(sent.get('projects'), list) and isinstance(output, dict)
                and set(output) == {'insights'} and isinstance(output['insights'], list)
                and all(isinstance(row, dict) and set(row) == {'text', 'conditions'}
                    and isinstance(row['conditions'], list) for row in output['insights'])):
            return json.dumps({'insights': [{**row, 'kind': 'new_method',
                'relation': 'new', 'target_id': None, 'scope_hint': None,
                'conditions': row['conditions'] or ['仅限测试'],
                **({'origin': 'body'} if isinstance(sent.get('comment_sources'), list) else {})}
                for row in output['insights']], 'supports': []})
    except (json.JSONDecodeError, TypeError):
        pass
    return response


class Model:
    def __init__(self):
        self.calls = 0
        self.intake = True
        self.response = json.dumps({"insights": [
            {"text": "短认识一", "conditions": ["仅限测试"]},
            {"text": "短认识二", "conditions": []}]})
        self.after = lambda: None
        self.error = None

    def public(self):
        return {"generation": {"base_url": "https://example.com/v1", "allow_remote": True}}

    def complete(self, messages, *, max_tokens, validate_current=None):
        if self.intake:
            return json.dumps({"title": "整理稿", "summary": "摘要", "topics": [],
                "facts": [], "todos": [], "uncertainties": [], "people": [],
                "dates": [], "suggestions": []}), {}
        validate_current()
        self.calls += 1
        self.messages = messages
        self.after()
        if self.error:
            raise self.error
        return response_for(messages, self.response), {"model": "fake", "configuration_revision": 1}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    service, model = RecognitionService(records), Model()
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path,
        records=records, models=model, documents=documents, service=service)
    item = asyncio.run(domains.intake.add_text({"project_id": "alpha", "text": "原文证据"}))
    result = asyncio.run(process_and_confirm(domains, item["id"], "alpha"))
    doc = result["document_id"]
    assert doc
    model.intake = False
    assign_scene(records, "document", doc, "alpha", "阅读")
    return SimpleNamespace(records=records, documents=documents, service=service,
        model=model, doc=doc, scope=WorkScope("local-user", "alpha"))


def generate(env):
    from backend.memory_app.v2.insight_generation import generate_insights
    return generate_insights(env.model, env.service, env.documents, "alpha", env.doc)


def test_pending_short_candidates_are_idempotent_and_inherit_scene(env):
    from backend.memory_app.document_recognition import CANDIDATE_SOURCE_CONSTRAINTS
    result = generate(env)
    assert len(result) == 2
    assert [row["id"] for row in result] == [f"candidate-v2-{env.doc}-r1-{n}" for n in (1, 2)]
    assert all(row["kind"] == "candidate" and row["state"] == "pending" for row in result)
    assert all(row["scene"] == "阅读" and row["document_ids"] == [env.doc] for row in result)
    assert env.records.list("recognitions") == ()
    assert generate(env) == result and env.model.calls == 1
    assert CANDIDATE_SOURCE_CONSTRAINTS in env.model.messages[0]["content"]
    assert all(row.payload["generation"]["model"] == "fake" for row in env.records.list("recognition_candidates"))


@pytest.mark.parametrize("response", ["not json", '{"insights": "bad"}', '{"insights":[{"text":"短","conditions":[null]}]}'])
def test_invalid_output_returns_empty_without_changing_document(env, caplog, response):
    before = env.documents.read(env.doc)
    env.model.response = response
    assert generate(env) == []
    assert "insight_invalid_output" in caplog.text
    assert env.documents.read(env.doc) == before
    assert env.records.list("recognition_candidates") == ()


def test_private_project_never_calls_model(env, caplog):
    set_private_project(env.records, "alpha", True, 0)
    assert generate(env) == [] and env.model.calls == 0
    assert "insight_private_project" in caplog.text


def test_long_rows_are_dropped_and_only_three_are_kept(env, caplog):
    env.model.response = json.dumps({"insights": [{"text": "长" * 41, "conditions": []}] +
        [{"text": "短" * 40, "conditions": []} for _ in range(4)]})
    result = generate(env)
    assert len(result) == 3 and all(len(row["text"]) == 40 for row in result)
    assert "insight_too_long" in caplog.text
    assert generate(env) == result and env.model.calls == 1


def test_model_error_is_body_free(env, caplog):
    env.model.error = RuntimeError("sensitive synthetic body")
    assert generate(env) == []
    assert "insight_generation_failed" in caplog.text
    assert "sensitive synthetic body" not in caplog.text


@pytest.mark.parametrize("change", ["privacy", "configuration", "revocation"])
def test_post_model_source_guard_discards_stale_response(env, caplog, change):
    def update():
        if change == "privacy":
            set_private_project(env.records, "alpha", True, 0)
        elif change == "configuration":
            env.model.public = lambda: {"generation": {"base_url": "https://changed.example/v1", "allow_remote": True}}
        else:
            experience = env.records.list("recognition_experiences")[0]
            env.service.revoke_experience(scope=env.scope, experience_id=experience.object_id, expected_revision=experience.revision)
    env.model.after = update
    assert generate(env) == []
    assert env.records.list("recognition_candidates") == ()
    assert "insight_generation_failed" in caplog.text


def test_published_alias_keeps_identity_and_scene(env):
    from backend.memory_app.v2.insights import resolve_insight, insight_view
    candidate = generate(env)[0]
    published = env.service.publish(scope=env.scope, candidate_id=candidate["id"],
        expected_revision=1, reviewer="local-user")
    assert resolve_insight(env.records, env.scope, candidate["id"]).object_id == published.id
    assert resolve_insight(env.records, env.scope, published.id).object_id == published.id
    view = insight_view(env.records, env.scope, published.id)
    assert view == insight_view(env.records, env.scope, candidate["id"])
    assert view["aliases"] == [candidate["id"]] and view["kind"] == "recognition"
    assert view["state"] == "active" and view["scene"] == "阅读"
    assert view["source_count"] == 1 and view["document_ids"] == [env.doc]
    assert resolve_insight(env.records, WorkScope("local-user", "other"), published.id) is None
    assert resolve_insight(env.records, WorkScope("other-user", "alpha"), candidate["id"]) is None


def test_new_document_revision_keeps_old_pending_history(env):
    first = generate(env)
    env.documents.save_user_edit(env.doc, markdown="新修订内容", expected_revision=1)
    second = generate(env)
    assert len(second) == 2 and all("-r2-" in row["id"] for row in second)
    assert env.model.calls == 2 and len(env.records.list("recognition_candidates")) == 4
    assert all(env.records.read("recognition_candidates", row["id"]).payload["state"] == "pending" for row in first)


def test_historical_document_revision_remains_valid_during_generation(env):
    env.model.after = lambda: env.documents.save_user_edit(env.doc, markdown="新修订", expected_revision=1)
    result = generate(env)
    assert len(result) == 2 and all("-r1-" in row["id"] for row in result)
    assert env.documents.read(env.doc)["revision"] == 2


def test_view_reuses_qualified_recognition_state_and_hides_rejected(env):
    from backend.memory_app.v2.insights import insight_view
    candidate, rejected = generate(env)
    published = env.service.publish(scope=env.scope, candidate_id=candidate["id"],
        expected_revision=1, reviewer="local-user")
    env.service.reject_candidate(scope=env.scope, candidate_id=rejected["id"],
        expected_revision=1, reviewer="local-user")
    assert insight_view(env.records, env.scope, rejected["id"]) is None
    experience = env.records.list("recognition_experiences")[0]
    with env.records.begin() as tx:
        tx.put("recognition_experiences", experience.object_id,
            {**experience.payload, "content": "changed evidence"}, expected_revision=experience.revision)
        tx.commit()
    assert env.service.get_recognition(scope=env.scope, recognition_id=published.id).effective_state == "stale"
    assert insight_view(env.records, env.scope, candidate["id"])["state"] == "stale"


def test_alias_does_not_cross_scope_or_retain_missing_target(env):
    from backend.memory_app.v2.insights import resolve_insight
    candidate = generate(env)[0]
    published = env.service.publish(scope=env.scope, candidate_id=candidate["id"],
        expected_revision=1, reviewer="local-user")
    row = env.records.read("recognitions", published.id)
    with env.records.begin() as tx:
        tx.put("recognitions", published.id, {**row.payload,
            "scope": {"user_id": "other-user", "project_id": "alpha"}}, expected_revision=row.revision)
        tx.commit()
    assert resolve_insight(env.records, env.scope, candidate["id"]) is None

    with env.records.begin() as tx:
        tx.delete("recognitions", published.id, expected_revision=row.revision + 1)
        tx.commit()
    assert resolve_insight(env.records, env.scope, candidate["id"]) is None


def test_related_view_uses_approved_scoped_relations(env):
    from backend.memory_app.relations import RelationProposalService
    from backend.memory_app.v2.insights import insight_view
    candidates = generate(env)
    published = [env.service.publish(scope=env.scope, candidate_id=row["id"],
        expected_revision=1, reviewer="local-user") for row in candidates]
    relations = RelationProposalService(env.records)
    relation = relations.propose(env.scope, published[0].id, published[1].id, "supports", "测试证据")
    assert insight_view(env.records, env.scope, published[0].id)["related"] == []
    relations.review(env.scope, relation["id"], relation["revision"], "approved")
    assert insight_view(env.records, env.scope, published[0].id)["related"] == [
        {"id": published[1].id, "text": published[1].content}]


def test_actual_comparative_default_freezes_neighbors_and_returns_pending_hints(env):
    experience = env.service.stage_experience(scope=env.scope, content='原文证据 方法')
    candidate = env.service.propose(scope=env.scope, content='原文证据 方法',
        source_experience_ids=[experience])
    old = env.service.publish(scope=env.scope, candidate_id=candidate.id,
        expected_revision=candidate.revision, reviewer='local-user')
    result = generate(env)
    assert len(result) == 2
    assert all(row['state'] == 'pending' and row['hint']['relation'] == 'new' for row in result)
    sent = json.loads(env.model.messages[-1]['content'])
    assert old.id in {row['id'] for row in sent['neighbors']}
    turn = next(row for row in env.records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights')
    assert turn.payload['request']['policy_versions']['extract'] == '@3'
    assert turn.payload['request']['policy_versions']['scope'] == '@2'
    assert old.id in {row['id'] for row in turn.payload['request']['privacy']['material_refs']
        if row['type'] == 'recognition'}
    assert generate(env) == result and env.model.calls == 1
    assert len(env.records.list('recognitions')) == 1


def test_native_comment_recipe_keeps_plain_body_pending_without_comment_proof(env):
    env.model.response = json.dumps({'insights': [{'origin': 'body', 'kind': 'new_method',
        'relation': 'new', 'text': '普通正文方法', 'conditions': ['出行之前'],
        'target_id': None, 'scope_hint': None}], 'supports': []})
    result = generate(env)
    assert len(result) == 1 and result[0]['text'] == '普通正文方法'
    assert result[0]['conditions'] == ['出行之前'] and result[0]['state'] == 'pending'
    assert result[0]['hint'] == {'relation': 'new', 'target_id': None, 'scope_hint': None, 'target': None}
    sent = json.loads(env.model.messages[-1]['content'])
    assert sent['comment_sources'] == []
    turn = next(row for row in env.records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights')
    assert turn.payload['request']['policy_versions']['extract'] == '@3'
    assert env.records.read('v2_comment_extract_inputs', turn.object_id).payload['sources'] == []
    assert env.records.list('v2_comment_candidates') == ()
    assert env.records.list('recognitions') == () and env.records.list('recognition_relations') == ()
    assert generate(env) == result and env.model.calls == 1


def test_native_comment_recipe_rejects_body_without_origin_instead_of_legacy_fallback(env, caplog):
    before = env.documents.read(env.doc)
    env.model.response = json.dumps({'insights': [{'kind': 'new_method', 'relation': 'new',
        'text': '未声明正文来源', 'conditions': ['出行之前'], 'target_id': None,
        'scope_hint': None}], 'supports': []})
    assert generate(env) == [] and env.model.calls == 1
    assert 'insight_invalid_output' in caplog.text
    turn = next(row for row in env.records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights')
    assert turn.payload['request']['policy_versions']['extract'] == '@3'
    assert json.loads(env.model.messages[-1]['content'])['comment_sources'] == []
    assert env.documents.read(env.doc) == before
    assert all(env.records.list(collection) == () for collection in (
        'recognition_candidates', 'v2_candidate_hints', 'v2_comment_candidates',
        'recognitions', 'recognition_relations'))


def test_synthetic_provider_preserves_legacy_bytes_and_adds_body_origin_only_for_received_at3():
    response = json.dumps({'insights': [{'text': '合成方法', 'conditions': []}]})
    messages = lambda sent: [{'role': 'user', 'content': json.dumps(sent)}]
    assert response_for(messages({'experiences': []}), response) == response
    comparative = {'insights': [{'text': '合成方法', 'conditions': ['仅限测试'],
        'kind': 'new_method', 'relation': 'new', 'target_id': None, 'scope_hint': None}],
        'supports': []}
    assert response_for(messages({'neighbors': [], 'projects': []}), response) == json.dumps(comparative)
    comparative['insights'][0]['origin'] = 'body'
    assert response_for(messages({'neighbors': [], 'projects': [], 'comment_sources': []}),
        response) == json.dumps(comparative)


@pytest.mark.parametrize('response', ['not json', '{"insights": "bad"}',
    '{"insights":[{"text":"短","conditions":[null]}]}',
    '{"insights":[{"text":"短","conditions":[],"invented":0}]}',
    '{"insights":[{"kind":"new_method","relation":"new","text":"短",'
    '"conditions":["条件"],"target_id":null,"scope_hint":null}],"supports":[]}'])
def test_synthetic_at3_provider_does_not_repair_malformed_or_missing_origin_responses(response):
    messages = [{'role': 'user', 'content': json.dumps({
        'neighbors': [], 'projects': [], 'comment_sources': []})}]
    if response == '{"insights":[{"text":"短","conditions":[null]}]}':
        # The legacy minimal shape is adapted, but its invalid condition is retained.
        assert json.loads(response_for(messages, response))['insights'][0]['conditions'] == [None]
    else:
        assert response_for(messages, response) == response


def test_historical_extract_one_preserves_empty_conditions_and_original_schema(env):
    from backend.memory_app.v2.policies import override
    with override(extract='@1'):
        result = generate(env)
    assert len(result) == 2
    assert result[1]['conditions'] == []
    assert all('hint' not in row for row in result)
    sent = json.loads(env.model.messages[-1]['content'])
    assert set(sent) == {'experiences'}
    turn = next(row for row in env.records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights')
    assert turn.payload['request']['policy_versions']['extract'] == '@1'
    assert env.records.list('v2_extract_inputs') == ()
