from backend.memory_app.v2.ladder import plan_ladder
from tests.memory_app.v2.test_ladder_budget import candidate


def test_duplicate_skips_keep_scanning_and_trace():
    rows = [candidate(str(i), "alpha beta gamma delta epsilon", score=10-i) for i in range(3)]
    rows.append(candidate("other", "omega zeta theta", score=1))
    result = plan_ladder(rows, "alpha omega unknown")
    assert [c["id"] for c in result["chosen"]] == ["0", "other"]
    assert sum(t.get("skipped_duplicate", 0) for t in result["trace"]) == 2
    assert sum(t["skipped_budget"] for t in result["trace"]) == 0


def test_refutes_ordinary_pair_and_expansion_survive():
    rows = [candidate(str(i), "alpha beta gamma delta epsilon", score=10-i) for i in range(3)]
    rows[2]["expansion_only"] = True
    def neighbors(identity):
        return [{"other_id": other, "kind": "refutes"} for other in ("1", "2")] if identity == "0" else []
    result = plan_ladder(rows, "alpha unknown", neighbors=neighbors)
    assert {c["id"] for c in result["chosen"]} == {"0", "1", "2"}


def test_same_document_l3_l2_l1_l0_remain_drillable():
    rows = [candidate(layer, "alpha", layer=layer, document_ids=["doc"]) for layer in ("L3", "L2", "L1", "L0")]
    result = plan_ladder(rows, "alpha missing unknown 具体")
    assert [c["layer"] for c in result["chosen"]] == ["L3", "L2", "L1", "L0"]


def test_same_document_same_layer_is_still_duplicate():
    rows = [candidate(str(i), "alpha", document_ids=["doc"]) for i in range(2)]
    assert len(plan_ladder(rows, "alpha missing")["chosen"]) == 1


def test_recognition_provenance_does_not_hide_distinct_content():
    rows = [candidate(str(i), "shared provenance " * 20, kind="recognition", entry={"content": text})
            for i, text in enumerate(("apples grow", "zebra swims"))]
    assert len(plan_ladder(rows, "missing")["chosen"]) == 2


import pytest
from types import SimpleNamespace
from backend.memory_app.v2.recall_dedup import cached_candidate_vectors, identity, similarity
from backend.recognition_retrieval import SQLiteEmbeddingCache, HttpEmbeddingProvider
from tests.memory_app.v2.test_ladder import env as env, recognition
from tests.memory_app.v2.test_insight_links import VectorModel


def test_real_cache_changes_production_selection_without_wire(env, monkeypatch):
    first = recognition(env, "alpha apples grow")
    second = recognition(env, "alpha zebras swim")
    collected = env.query.collect_candidates("alpha", "alpha unknown")
    before = env.query.prepare_ask("alpha", "alpha unknown", collected=collected)
    assert len(before["chosen"]) == 2
    model = HttpEmbeddingProvider(None, "https://example.invalid/v1/embeddings", "vectors", config_revision="1")
    cache = SQLiteEmbeddingCache(str(env.root / "recognition-vectors.sqlite3"))
    try:
        for obj in (first, second):
            cache.write(model_id=model.cache_identity, entry=SimpleNamespace(
                project_id="alpha", id=obj.id, revision=obj.revision), vector=[1., 0.])
    finally:
        cache.close()
    class CachedOnly(VectorModel):
        def snapshot(self, purpose):
            raise AssertionError("No adapter or credential access allowed")
    env.query.models = CachedOnly()
    result = env.query.prepare_ask("alpha", "alpha unknown", collected=collected)
    assert len(result["chosen"]) == 1
    assert result["trace"][0]["skipped_duplicate"] == 1
    class Revised(CachedOnly):
        def public(self):
            value = super().public()
            value["embedding"]["revision"] = 2
            return value
    env.query.models = Revised()
    assert len(env.query.prepare_ask("alpha", "alpha unknown", collected=collected)["chosen"]) == 2


@pytest.mark.parametrize("other, expected", [([.8, .6], .8), ([.7, .714142842854285], .7), ([-1., 0.], -1.)])
def test_cached_cosine_is_raw_not_shifted(other, expected):
    a, b = candidate("a", "a"), candidate("b", "z")
    assert similarity(a, b, {identity(a): [1., 0.], identity(b): other}) == pytest.approx(expected)


def test_dimension_mismatch_falls_back_to_characters():
    a, b = candidate("a", "alpha"), candidate("b", "alpha")
    assert similarity(a, b, {identity(a): [1.], identity(b): [1., 0.]}) == 1


@pytest.mark.parametrize("text, count", [("abcde", 1), ("abcdx", 2)])
def test_character_threshold_is_inclusive(text, count):
    rows = [candidate("a", "abcdef", score=2), candidate("b", text)]
    assert len(plan_ladder(rows, "missing")["chosen"]) == count


def test_duplicate_related_expansion_has_its_own_trace():
    rows = [candidate("a", "alpha"), candidate("b", "alpha", expansion_only=True)]
    result = plan_ladder(rows, "missing", neighbors=lambda i: [{"kind": "related", "other_id": "b"}] if i == "a" else [])
    assert [c["id"] for c in result["chosen"]] == ["a"]
    assert sum(t.get("skipped_duplicate", 0) for t in result["trace"]) == 1
    assert all(t["skipped_budget"] == 0 for t in result["trace"])


def test_long_duplicates_compare_the_same_fitted_evidence():
    from core.search_and_recall.evidence_windows import EvidenceWindow
    text = "alpha " + "".join(chr(0x4e00 + i) for i in range(2400))
    rows = [candidate(i, text, layer="L2", windows=(EvidenceWindow(0, len(text), text),)) for i in ("a", "b")]
    # Different original vectors must not override identical fitted evidence.
    result = plan_ladder(rows, "alpha unknown", token_budget=12000,
                         cached_vectors={identity(rows[0]): (1., 0.), identity(rows[1]): (0., 1.)})
    assert len(result["chosen"]) == 1
    assert sum(t.get("skipped_duplicate", 0) for t in result["trace"]) == 1


def test_missing_cache_does_not_create_file(env):
    env.query.models = VectorModel()
    assert cached_candidate_vectors(env.query, []) == {}
    assert not (env.root / "recognition-vectors.sqlite3").exists()


def test_read_only_cache_preserves_corrupt_rows_and_rejects_writes(tmp_path):
    import sqlite3
    path = tmp_path / "vectors.sqlite3"
    entry = SimpleNamespace(project_id="alpha", id="entry", revision=1)
    cache = SQLiteEmbeddingCache(str(path))
    cache.write(model_id="model", entry=entry, vector=[1., 0.])
    cache.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE recognition_embedding_cache SET vector_json = 'bad'")
    cache = SQLiteEmbeddingCache(str(path), read_only=True)
    try:
        with pytest.raises(ValueError, match="cached vector is invalid"):
            cache.read(model_id="model", entry=entry)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cache.write(model_id="model", entry=entry, vector=[0., 1.])
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cache.purge_invalid(project_id="alpha", current_revisions={})
    finally:
        cache.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT vector_json FROM recognition_embedding_cache").fetchall() == [("bad",)]
    absent = tmp_path / "absent.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        SQLiteEmbeddingCache(str(absent), read_only=True)
    assert not absent.exists()


def test_fixed_duplicate_interference_gold_covers_both_facts(tmp_path):
    import json
    import sqlite3
    from pathlib import Path
    from tools.memory_eval import seed
    from backend.shared.llm.litellm_gateway import _estimate_input_tokens
    fixture = json.loads((Path(__file__).parents[2] / "fixtures/memory_eval/dedup_interference.json").read_text(encoding="utf-8"))
    keeper = sqlite3.connect(tmp_path / "records.sqlite3")
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        query, identities = seed(tmp_path, fixture)
        names = {actual: logical for logical, actual in identities.items()}
        total_tokens = 0
        for question in fixture["questions"]:
            plan = query.prepare_ask(question["project_id"], question["question"])
            for fact in question["required_facts"]:
                assert any(names.get(c["entry"]["id"]) in fact["any_document_ids"]
                           and fact["answer_text"] in c["excerpt"] for c in plan["chosen"])
            assert sum(row.get("skipped_duplicate", 0) for row in plan["trace"]) > 0
            total_tokens += _estimate_input_tokens([{"role": "user", "content": "\n\n".join(c["excerpt"] for c in plan["chosen"])}])
        # Frozen pre-dedup result: three questions, 2524 evidence tokens.
        assert total_tokens <= 2524
        assert query.models.attempts == 0
    finally:
        keeper.close()
