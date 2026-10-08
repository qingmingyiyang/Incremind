"""Scene scope uses real published objects, document layers and bookshelf reads."""
from unittest.mock import patch

import pytest

from backend.memory_app.v2.bookshelf import _spines, consult_bookshelf
from backend.memory_app.v2.policies import get, override
from backend.memory_app.v2.policies.types import ScopeInput
from backend.memory_app.v2.profile import profile_messages
from backend.memory_app.v2.projects import assign_scene
from tests.memory_app.v2.test_ladder import env as _env, document, recognition
from tests.memory_app.v2.test_bookshelf import forgotten

env = _env


@pytest.fixture
def inherited_scope():
    with override(scope="@2"):
        yield


@pytest.mark.parametrize("layer", ["L2", "L1", "L0"])
def test_scene_inherits_project_document_layers_and_excludes_siblings(env, inherited_scope, layer):
    project = document(env, summary="alpha project method", body="alpha project detail",
                       original="alpha project original")
    local = document(env, summary="alpha local fact", body="alpha local detail",
                     original="alpha local original", scene="local")
    sibling = document(env, summary="alpha sibling fact", body="alpha sibling detail",
                       original="alpha sibling original", scene="sibling")
    rows = env.query.collect_candidates("alpha", "alpha", scene="local")["candidates"]
    own = {row.get("document_id") for row in rows if row["layer"] == layer}
    assert project in own and local in own
    assert sibling not in own
    assert all(row["scene"] in {None, "local"} for row in rows)


def test_scene_inherits_project_recognition_and_its_document_scene(env, inherited_scope):
    project = recognition(env, "alpha project methodology")
    local_doc = document(env, scene="local")
    local = recognition(env, "alpha local case", doc=local_doc)
    sibling_doc = document(env, scene="sibling")
    sibling = recognition(env, "alpha sibling case", doc=sibling_doc)
    rows = env.query.collect_candidates("alpha", "alpha", scene="local")["candidates"]
    ids = {row["id"] for row in rows if row["layer"] == "L3"}
    assert {project.id, local.id} <= ids
    assert sibling.id not in ids


def test_final_recognition_selection_prefers_local_on_equal_score(env, inherited_scope):
    with patch("backend.recognition.service._now", return_value="2020-01-01T00:00:00+00:00"):
        local = recognition(env, "alpha cedar local observation")
    assign_scene(env.records, "recognition", local.id, "alpha", "local")
    with patch("backend.recognition.service._now", return_value="2099-01-01T00:00:00+00:00"):
        project = recognition(env, "alpha maple project method")
    rows = env.query.collect_candidates("alpha", "alpha", scene="local")["candidates"]
    pair = [row for row in rows if row["id"] in {local.id, project.id}]
    assert len(pair) == 2 and pair[0]["score"] == pair[1]["score"]
    assert pair[0]["id"] == local.id
    plan = env.query.prepare_ask("alpha", "alpha", scene="local")
    assert plan["policy_versions"]["scope"] == "@2"
    assert plan["chosen"][0]["id"] == local.id


@pytest.mark.parametrize("layer", ["L2", "L1", "L0"])
def test_final_document_selection_prefers_local_even_when_project_document_is_connected(env, inherited_scope, layer):
    local = document(env, summary="alpha cedar", body="alpha cedar detail",
                     original="alpha cedar original", scene="local")
    project = document(env, summary="alpha maple", body="alpha maple detail",
                       original="alpha maple original")
    recognition(env, "alpha project method", doc=project)
    plan = env.query.prepare_ask("alpha", "alpha 原文", scene="local")
    selected = [row for row in plan["chosen"] if row["layer"] == layer]
    assert {row.get("document_id") for row in selected} == {local, project}
    assert selected[0]["document_id"] == local


@pytest.mark.parametrize("kind", ["recognition", "document"])
def test_bookshelf_inherits_project_memory_with_sibling_and_manual_forget_guards(env, inherited_scope, kind):
    identities = []
    for scene in (None, "local", "sibling"):
        if kind == "recognition":
            insight = recognition(env, "alpha beta gamma forgotten " + str(scene))
            forgotten(env, insight)
            identity = insight.id
            if scene:
                assign_scene(env.records, "recognition", identity, "alpha", scene)
        else:
            identity = document(env, summary="alpha beta gamma", scene=scene)
            with env.records.begin() as tx:
                tx.put("v2_document_recall", identity, {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
                tx.commit()
        identities.append(identity)
    spines = _spines(env.query, "alpha", "local")
    ids = {spine["id"] for spine in spines if spine["kind"] == kind}
    assert set(identities[:2]) <= ids and identities[2] not in ids
    plan = env.query.prepare_ask("alpha", "alpha beta gamma", scene="local")
    consult_bookshelf(env.query, "alpha", "alpha beta gamma", plan, scene="local")
    assert identities[2] not in {spine["id"] for spine in plan["bookshelf"]["spines"]}
    if kind == "recognition":
        assert plan["bookshelf"]["used"] > 0
        assert any(row["id"] in identities[:2] for row in plan["chosen"])


def test_unassigned_me_recognition_is_present_once_as_profile_context(env, inherited_scope):
    persona = recognition(env, "alpha persona likes quiet places", project="me")
    local = recognition(env, "alpha local fact")
    assign_scene(env.records, "recognition", local.id, "alpha", "local")
    plan = env.query.prepare_ask("alpha", "alpha", scene="local")
    assert {item["id"] for item in plan["profile"]["items"]} == {persona.id}
    assert persona.id not in {row["id"] for row in plan["chosen"]}
    evidence = {"role": "user", "content": "\n".join(row["excerpt"] for row in plan["chosen"])}
    messages = profile_messages(plan["profile"], [evidence])
    assert len(messages) == 2 and messages[0]["role"] == "system"
    assert messages[0]["content"].count("alpha persona likes quiet places") == 1
    assert "alpha persona likes quiet places" not in messages[1]["content"]


def test_scope_one_exact_scene_contract_remains_available():
    legacy = get("scope", version="@1")
    assert legacy(ScopeInput("local", "local")) is True
    assert legacy(ScopeInput("local", None)) is False
    assert legacy(ScopeInput("local", "sibling")) is False
    assert legacy(ScopeInput(None, "sibling")) is True


@pytest.mark.parametrize("requested,assigned,expected", [
    ("local", "local", True), ("local", None, True),
    ("local", "sibling", False), (None, "sibling", True),
])
def test_scope_two_visibility(requested, assigned, expected):
    assert get("scope", version="@2")(ScopeInput(requested, assigned)) is expected


def test_no_scene_keeps_every_candidate_and_final_evidence_identical(env):
    local_doc = document(env, scene="local")
    document(env, summary="alpha project", body="alpha project original")
    sibling_doc = document(env, scene="sibling")
    recognition(env, "alpha local knowledge", doc=local_doc)
    recognition(env, "alpha sibling knowledge", doc=sibling_doc)
    recognition(env, "alpha project knowledge")
    values = []
    for selected in ("@1", "@2"):
        with override(scope=selected):
            collected = env.query.collect_candidates("alpha", "alpha 原文")
            plan = env.query.prepare_ask("alpha", "alpha 原文", collected=collected)
            values.append((collected["candidates"], collected["excluded_sources"],
                           plan["chosen"], plan["trace"], plan["profile"]))
    assert values[0] == values[1]


def test_original_linked_only_to_sibling_documents_never_becomes_project_memory(env, inherited_scope):
    sibling = document(env, scene="sibling")
    identity = "legacy-sibling-source"
    env.query.source_store.write("sources", identity, {
        "id": identity, "project_id": "alpha", "title": "Sibling alpha",
        "identity_method": "legacy_import", "metadata": {"content_snapshot": "alpha sibling secret original"},
    }, expected_revision=0)
    project_source = "legacy-project-source"
    env.query.source_store.write("sources", project_source, {
        "id": project_source, "project_id": "alpha", "title": "Project alpha",
        "identity_method": "legacy_import", "metadata": {"content_snapshot": "alpha project original"},
    }, expected_revision=0)
    current = env.documents.read(sibling)
    env.documents.save_user_edit(sibling, expected_revision=current["revision"],
        markdown=env.documents.markdown(sibling),
        source_refs=[*current["source_refs"], {"source_id": identity, "locator": "source://" + identity}])
    assert any(row["id"] == identity for row in env.query.query_entries("alpha"))
    rows = env.query.collect_candidates("alpha", "alpha", scene="local")["candidates"]
    assert identity not in {row["id"] for row in rows}
    assert project_source in {row["id"] for row in rows}
    with override(scope="@1"):
        legacy = env.query.collect_candidates("alpha", "alpha", scene="local")["candidates"]
    assert not {identity, project_source} & {row["id"] for row in legacy}


def test_same_score_preference_preserves_other_score_slots():
    from backend.memory_app.v2.policies.scope import prefer_scene_ties
    rows = [{"id": "project", "score": 5, "scope_priority": 1},
            {"id": "higher", "score": 6}, {"id": "local", "score": 5}]
    actual = prefer_scene_ties(rows, score_key=lambda row: row["score"])
    assert [row["id"] for row in actual] == ["local", "higher", "project"]
    assert [row["score"] for row in actual] == [row["score"] for row in rows]
    assert rows[0]["id"] == "project"
