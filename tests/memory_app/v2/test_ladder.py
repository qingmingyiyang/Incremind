from types import SimpleNamespace
import asyncio
import json

import pytest

from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.policies import override
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.document_recognition import ensure_document_experience
from fastapi import FastAPI
from backend.recognition import RecognitionError, RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    service = RecognitionService(records)
    query = WorkspaceQuery(records, documents, JsonObjectStore(tmp_path / ".rebuild-data"),
                           SimpleNamespace(public=lambda: {}), service)
    return SimpleNamespace(records=records, documents=documents, service=service, query=query, root=tmp_path)


def document(env, summary="alpha beta", body="alpha beta gamma", scene=None, original=None):
    class Model:
        def complete(self, messages, **kwargs):
            return json.dumps({"title": "Synthetic " + str(len(env.documents.list())), "summary": summary,
                "facts": [], "topics": [], "todos": [], "uncertainties": [], "people": [],
                "dates": [], "suggestions": []}), {}
    domains = install_workspace_routes(FastAPI(), runtime_root=env.root, records=env.records,
        documents=env.documents, service=env.service, models=Model())
    item = asyncio.run(domains.intake.add_text({"project_id": "alpha", "text": original or body}))
    confirmed = asyncio.run(process_and_confirm(domains, item["id"], "alpha"))
    result = env.documents.save_user_edit(confirmed["document_id"], expected_revision=1,
        markdown="# Synthetic\n\n## 摘要\n" + summary + "\n\n## 正文\n" + body)
    if scene:
        assign_scene(env.records, "document", result["id"], "alpha", scene)
    return result["id"]


def recognition(env, text, *, project="alpha", doc=None):
    scope = WorkScope("local-user", project)
    experience = (ensure_document_experience(env.documents, env.service, project, doc)[0] if doc else
                  env.service.stage_experience(scope=scope, content="Synthetic evidence"))
    proposed = env.service.propose(scope=scope, content=text, source_experience_ids=[experience])
    return env.service.publish(scope=scope, candidate_id=proposed.id, expected_revision=1, reviewer="local-user")


def test_sufficient_recognition_stops_before_summary_and_preserves_trace(env):
    doc = document(env)
    recognition(env, "alpha beta gamma", doc=doc)
    plan = env.query.prepare_ask("alpha", "alpha beta gamma?")
    assert [c["layer"] for c in plan["chosen"]] == ["L3"]
    assert plan["trace"] == [{"layer": "L3", "considered": 1, "selected": 1, "skipped_budget": 0, "coverage": 1.0, "stopped": True}]


def test_insufficient_recognition_prefers_connected_summary_with_exact_coordinates(env):
    document(env, summary="alpha beta gamma unrelated")
    linked = document(env)
    recognition(env, "alpha", doc=linked)
    plan = env.query.prepare_ask("alpha", "alpha beta gamma?")
    assert [c["layer"] for c in plan["chosen"]] == ["L3", "L2", "L2"]
    summaries = [c for c in plan["chosen"] if c["layer"] == "L2"]
    assert summaries[0]["document_id"] == linked
    for candidate in summaries:
        markdown = env.documents.markdown(candidate["document_id"])
        assert all(markdown[w.start:w.end] == w.text for w in candidate["windows"])
        assert "## 正文" not in candidate["excerpt"]
    assert plan["trace"][-1]["stopped"] is True


def test_detail_question_reaches_body_excluding_summary(env):
    doc = document(env, summary="alpha 原文", body="alpha 原文具体数据")
    recognition(env, "alpha 原文具体数据", doc=doc)
    plan = env.query.prepare_ask("alpha", "alpha 原文?")
    assert "L1" in [c["layer"] for c in plan["chosen"]]
    body = next(c for c in plan["chosen"] if c["layer"] == "L1")
    assert "## 摘要" not in body["excerpt"]
    assert plan["trace"][-1]["layer"] == "L1" and plan["trace"][-1]["stopped"] is True


def test_scene_is_inherited_by_recognition_and_document_layers(env):
    allowed = document(env, scene="reading")
    other = document(env, scene="writing")
    first = recognition(env, "alpha", doc=allowed)
    recognition(env, "alpha", doc=other)
    persona = recognition(env, "alpha beta gamma", project="me")
    assign_scene(env.records, "recognition", persona.id, "me", "writing")
    candidates = env.query.collect_candidates("alpha", "alpha beta gamma?", scene="reading")
    assert candidates["candidates"]
    assert all(c["scene"] == "reading" for c in candidates["candidates"])
    plan = env.query.prepare_ask("alpha", "alpha beta gamma?", scene="reading")
    assert first.id in [c["id"] for c in plan["chosen"]]
    assert other not in [c.get("document_id") for c in plan["chosen"]]
    assert persona.id not in [c["id"] for c in plan["chosen"]]


def test_persona_has_own_scope_limit_and_does_not_supply_coverage(env):
    for i in range(3):
        recognition(env, "alpha " + ("oranges orchard", "stargazing telescopes", "swimming oceans")[i], project="me")
    document(env, summary="alpha beta gamma")
    plan = env.query.prepare_ask("alpha", "alpha beta gamma?")
    personas = plan["profile"]["items"]
    assert len(personas) == 3 and plan['profile']['tokens'] <= 600
    assert not any(c.get("persona") for c in plan["chosen"])
    assert plan["trace"][0]["coverage"] == 0 and not plan["trace"][0]["stopped"]
    assert any(c["layer"] == "L2" for c in plan["chosen"])
    env.query.validate_ask_plan(plan)
    set_preference(env.records, WorkScope("local-user", "me"), personas[0]["id"],
        recognition_revision=1, preference_revision=0, state="forgotten")
    with pytest.raises(RecognitionError):
        env.query.validate_ask_plan(plan)
    set_preference(env.records, WorkScope("local-user", "me"), personas[0]["id"],
        recognition_revision=1, preference_revision=1, state="normal")
    set_private_project(env.records, "me", True, 0)
    with pytest.raises(RecognitionError):
        env.query.validate_ask_plan(plan)
    set_private_project(env.records, "me", False, 1)
    me = env.query.prepare_ask("me", "alpha beta gamma?")
    assert len({c["id"] for c in me["chosen"]}) == len(me["chosen"])
    assert not any(c.get("persona") for c in me["chosen"])


def test_original_windows_use_original_coordinates_after_document_layers(env):
    original = "原件开头\r\nalpha beta gamma 原话unique-marker"
    doc = document(env, summary="alpha", body="alpha", original=original)
    plan = env.query.prepare_ask("alpha", "unique-marker 原话?")
    candidate = next(c for c in plan["chosen"] if c["layer"] == "L0")
    assert [c["layer"] for c in plan["chosen"]] == ["L0"]
    assert candidate["document_id"] == doc
    assert candidate["coordinate_space"] == "workspace_source_text_v1"
    assert all(original[w.start:w.end] == w.text for w in candidate["windows"])
    assert [t["layer"] for t in plan["trace"]] == ["L3", "L2", "L1", "L0"]
    assert plan["trace"][-1]["stopped"] is True
    env.query.validate_ask_plan(plan)


def test_forgotten_and_private_projects_are_excluded(env):
    forgotten = recognition(env, "alpha beta gamma")
    set_preference(env.records, WorkScope("local-user", "alpha"), forgotten.id,
        recognition_revision=1, preference_revision=0, state="forgotten")
    recognition(env, "alpha beta gamma", project="me")
    set_private_project(env.records, "me", True, 0)
    assert env.query.prepare_ask("alpha", "alpha beta gamma?")["chosen"] == []
    document(env)
    set_private_project(env.records, "alpha", True, 0)
    assert env.query.prepare_ask("alpha", "alpha beta gamma?")["chosen"] == []


def test_public_query_terms_keeps_legacy_alias_and_ladder_total_budget():
    from core.search_and_recall.evidence_windows import query_terms, _terms
    from backend.memory_app.v2.ladder import plan_ladder
    assert query_terms is _terms
    candidates = [{"id": str(i), "layer": layer, "score": 10-i, "excerpt": "alpha " + chr(65+i)*2, "document_id": "doc"}
                  for i, layer in enumerate(["L3"]*5 + ["L2"]*4 + ["L1"]*4 + ["L0"]*4)]
    candidates += [{"id": "persona"+str(i), "layer": "L3", "score": 9, "excerpt": "alpha beta gamma " + chr(65+i)*12,
                    "persona": True} for i in range(4)]
    result = plan_ladder(candidates, "alpha beta gamma?")
    assert len(result["chosen"]) == 9
    assert sum(t["selected"] for t in result["trace"]) == len(result["chosen"])
    assert len([c for c in result["chosen"] if c.get("persona")]) == 0
    assert [t["layer"] for t in result["trace"]] == ["L3", "L2", "L1", "L0"]
    assert all(t["coverage"] == pytest.approx(1/3) and not t["stopped"] for t in result["trace"])


@override(scope="@1")
def test_connected_source_precedes_unrelated_source_and_inherits_any_matching_scene(env):
    linked = []
    for scene in ("reading", "writing"):
        doc = env.documents.create(DocumentDraft(title=scene, document_type="legacy-material",
            markdown="# " + scene + "\n\n## 摘要\nalpha\n\n## 正文\nalpha",
            source_refs=({"source_id": "linked-source", "locator": "text:0:5"},), project_id="alpha"))
        linked.append(doc["id"])
        assign_scene(env.records, "document", doc["id"], "alpha", scene)
    for identity, content in (("linked-source", "alpha"), ("unrelated-source", "alpha beta gamma")):
        env.query.source_store.write("sources", identity, {"id": identity, "title": identity,
            "project_id": "alpha", "metadata": {"content": content}}, expected_revision=0)
    plan = env.query.prepare_ask("alpha", "alpha beta gamma?")
    originals = [c for c in plan["chosen"] if c["layer"] == "L0"]
    assert [c["id"] for c in originals] == ["linked-source", "unrelated-source"]
    assert set(originals[0]["document_ids"]) == set(linked)
    scoped = env.query.prepare_ask("alpha", "alpha beta gamma?", scene="reading")
    assert any(c["id"] == "linked-source" and c["scene"] == "reading" for c in scoped["chosen"])
    assert all(c.get("scene") == "reading" for c in scoped["chosen"])
