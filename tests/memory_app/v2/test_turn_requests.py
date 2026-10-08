import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.turn_requests import freeze_product_turn, validate_frozen_inputs
from backend.recognition import WorkScope, RecognitionConflict
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_workbench_ask import env, add_document, publish


class Models:
    def __init__(self):
        self.allowed = True

    def public(self):
        return {"generation": {"allow_remote": self.allowed}}


@pytest.fixture
def materials(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    with records.begin() as tx:
        for identity in ("public-item", "private-item"):
            tx.put("workspace_items", identity, {"id": identity, "project_id": "alpha",
                "source_text": identity + "-body"}, expected_revision=0)
        tx.commit()
    SourceEgressService(records).set_policy(WorkScope("local-user", "alpha"),
        "original_item", "private-item", 1, 0, [])
    descriptors = [{"project_id": "alpha", "type": "original_item", "id": identity, "revision": 1}
                   for identity in ("private-item", "public-item")]
    return records, Models(), descriptors


def freeze(records, models, descriptors, kind="memory.organize", **kwargs):
    loaded = []

    def load(item):
        identity = item["id"]
        loaded.append(identity)
        return item["payload"]["source_text"]

    value = freeze_product_turn(kind, records=records, models=models, project_id="alpha",
        materials=descriptors, load_text=load, turn_id="turn-" + "a" * 32,
        session_id="session-alpha", operation_id="op-product-turn-0001",
        idempotency_key="product-turn-request-0001", created_at="2026-10-02T00:00:00Z", **kwargs)
    return value, loaded


@pytest.mark.parametrize("kind", ["memory.organize", "memory.propose_insights", "memory.consolidate", "memory.link_suggest"])
def test_private_material_is_excluded_before_loading_any_aux_input(materials, kind):
    records, models, descriptors = materials
    first, loaded = freeze(records, models, descriptors, kind)
    second, _ = freeze(records, models, descriptors, kind)
    assert first == second
    assert loaded == ["public-item"]
    assert "private-item" not in json.dumps(first["input"])
    assert "private-item-body" not in json.dumps(first)
    assert first["privacy"]["excluded_refs"][0]["object_id"] == "private-item"
    assert first["privacy"]["allow_remote"] is True
    schema = json.loads((Path(__file__).resolve().parents[3] / "core-contracts/ai/turn-request.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(first))
    validate_frozen_inputs(records, models, first)
    models.allowed = False
    with pytest.raises(RecognitionConflict):
        validate_frozen_inputs(records, models, first)


def test_explicit_local_organize_retains_private_body_but_never_remote_authority(materials):
    records, models, descriptors = materials
    value, loaded = freeze(records, models, descriptors, local_only=True)
    assert sorted(loaded) == ["private-item", "public-item"]
    assert "private-item-body" in value["input"]["text"]
    assert value["privacy"]["mode"] == "local_only"
    assert value["privacy"]["allow_remote"] is False
    validate_frozen_inputs(records, models, value)


def test_source_revision_and_scope_cannot_drift(materials):
    records, models, descriptors = materials
    value, _ = freeze(records, models, descriptors)
    with records.begin() as tx:
        row = tx.read("workspace_items", "public-item")
        tx.put("workspace_items", "public-item", {**row.payload, "source_text": "changed"}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        validate_frozen_inputs(records, models, value)
    descriptors[0]["project_id"] = "unrelated"
    with pytest.raises(RecognitionConflict):
        freeze(records, models, descriptors)


def test_reference_cannot_borrow_a_different_public_objects_authority(materials):
    records, models, descriptors = materials
    descriptors[0]["sources"] = [{"type": "original_item", "id": "public-item", "revision": 1}]
    with pytest.raises(RecognitionConflict):
        freeze(records, models, descriptors[:1])


def test_aux_freezing_has_no_usage_or_thread_writes(materials):
    records, models, descriptors = materials
    value, _ = freeze(records, models, descriptors, capabilities=[])
    from core.ai_kernel import SynchronousAIRuntime, ScopedCapabilityRegistry, InMemoryTurnEventStore, InMemoryTurnPayloadStore

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            assert "private-item-body" not in request["input"]["text"]
            return {"type": "complete", "summary": "synthetic"}

    events = InMemoryTurnEventStore()
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=events, payloads=InMemoryTurnPayloadStore())
    result = runtime.submit_turn(value)
    assert events.events_after(result.turn_id)[-1]["type"] == "turn.completed"
    for collection in ("v2_threads", "v2_turns", "v2_usage_insight", "v2_usage_document"):
        assert records.list(collection) == ()


@pytest.mark.parametrize("material_type", ["document", "recognition"])
def test_domain_derived_material_keeps_its_original_privacy_and_revision(env, material_type):
    document, original_id = add_document(env)
    identity = document if material_type == "document" else publish(env, doc=document)[0].id
    collection = "documents" if material_type == "document" else "recognitions"
    descriptor = {"project_id": "alpha", "type": material_type, "id": identity,
                  "revision": env.records.read(collection, identity).revision}
    loaded = []

    def freeze_derived():
        return freeze_product_turn("memory.propose_insights", records=env.records, models=env.model,
            project_id="alpha", materials=[descriptor], load_text=lambda material: loaded.append(material["id"]) or "synthetic derived text",
            turn_id="turn-" + "a" * 32, session_id="session-alpha", operation_id="op-product-turn-0001",
            idempotency_key="product-turn-request-0001", created_at="2026-10-02T00:00:00Z")

    value = freeze_derived()
    assert loaded == [identity]
    item = env.records.read("workspace_items", original_id)
    SourceEgressService(env.records).set_policy(WorkScope("local-user", "alpha"), "original_item", original_id,
        item.revision, 0, [])
    with pytest.raises(RecognitionConflict):
        validate_frozen_inputs(env.records, env.model, value)
    loaded.clear()
    excluded = freeze_derived()
    assert loaded == []
    assert excluded["privacy"]["excluded_refs"][0]["object_id"] == identity


def test_recreated_json_source_cannot_rebind_an_already_loaded_body(materials, tmp_path, monkeypatch):
    from core.storage_provider import JsonObjectStore
    from backend.memory_app.v2 import privacy
    records, models, _ = materials
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    body = {"id": "json-source", "project_id": "alpha", "source_text": "old body"}
    store.write("sources", "json-source", body, expected_revision=0)
    original_resolve = privacy.resolve_turn_material
    replaced = False

    def replace_after_loading(*args):
        nonlocal replaced
        result = original_resolve(*args)
        if not replaced:
            replaced = True
            store.delete("sources", "json-source")
            store.write("sources", "json-source", {**body, "source_text": "new body"}, expected_revision=0)
        return result

    monkeypatch.setattr(privacy, "resolve_turn_material", replace_after_loading)
    with pytest.raises(RecognitionConflict):
        freeze(records, models, [{"project_id": "alpha", "type": "original_source", "id": "json-source", "revision": 1}])
