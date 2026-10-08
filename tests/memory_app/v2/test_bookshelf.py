from backend.memory_app.v2.bookshelf import consult_bookshelf
from backend.memory_app.recall_preferences import set_preference
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish, ask, add_document

env = _env_fixture


def test_bookshelf_instruction_is_reserved_with_contradiction_instruction():
    from backend.memory_app.v2.budget import input_tokens, ask_instruction, source_texts, user_text
    from backend.shared.llm.litellm_gateway import _estimate_input_tokens

    chosen = [{"id": "old", "title": "old", "excerpt": "alpha", "bookshelf": True}]
    messages = [
        {"role": "system", "content": ask_instruction([*chosen, {"link_kind": "refutes"}])},
        {"role": "user", "content": user_text(source_texts(chosen), "alpha?")},
    ]
    assert input_tokens(chosen, "alpha?", reserve_refutes=True) == _estimate_input_tokens(messages)


def forgotten(env, insight, *, by="auto"):
    scope = WorkScope("local-user", "alpha")
    set_preference(
        env.records, scope, insight.id, recognition_revision=insight.revision, preference_revision=0, state="forgotten"
    )
    if by == "auto":
        with env.records.begin() as tx:
            row = tx.read("recognition_recall_preferences", insight.id)
            tx.put(
                "recognition_recall_preferences",
                insight.id,
                {**row.payload, "by": "auto"},
                expected_revision=row.revision,
            )
            tx.commit()


def test_automatic_forgotten_statement_is_found_and_cited_then_cooled(env):
    insight, _ = publish(env)
    body = env.records.read("recognitions", insight.id)
    forgotten(env, insight)
    response = ask(env)
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is False
    assert receipt["citations"][0]["id"] == insight.id and receipt["citations"][0]["bookshelf"] is True
    assert receipt["trace"][0]["bookshelf"] == {"hits": 1, "used": 1}
    assert "以前记过、已经淡忘" in env.model.messages[0]["content"]
    recall = env.records.read("recognition_recall_preferences", insight.id)
    usage = env.records.read("v2_usage_insight", insight.id)
    assert recall.payload["state"] == "cooled" and recall.payload["by"] == "auto"
    assert usage.payload["score"] == 0.5 and usage.payload["count"] == 2
    assert env.records.read("recognitions", insight.id) == body


def test_manual_forgotten_never_enters_spines(env):
    insight, _ = publish(env)
    forgotten(env, insight, by="user")
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    consult_bookshelf(env.domains.query, "alpha", "alpha beta gamma?", plan)
    assert plan["bookshelf"]["hits"] == 0 and plan["bookshelf"]["used"] == 0
    assert plan["chosen"] == []


def test_manual_forgotten_document_never_enters_any_ask_layer(env):
    doc, item = add_document(env)
    original = env.records.read("documents", doc)
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc,
               {"project_id": "alpha", "state": "forgotten", "by": "user"}, expected_revision=0)
        tx.commit()
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    consult_bookshelf(env.domains.query, "alpha", "alpha beta gamma?", plan)
    assert plan["chosen"] == []
    assert plan["bookshelf"]["hits"] == 0
    assert env.records.read("documents", doc) == original


def test_document_forgotten_after_preview_invalidates_the_plan(env):
    import pytest
    from backend.recognition import RecognitionError

    doc, _ = add_document(env)
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    assert plan["chosen"]
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc,
               {"project_id": "alpha", "state": "forgotten", "by": "user"}, expected_revision=0)
        tx.commit()
    with pytest.raises(RecognitionError, match="excluded from recall"):
        env.domains.query.validate_ask_plan(plan)


def legacy_source(env, docs, *, content=(
    "alpha beta gamma independently recorded legacy archive with distinct "
    "contextual evidence and historical observations."
)):
    store = env.domains.query.source_store
    source = "qa-legacy-source"
    store.write("sources", source, {"id": source, "project_id": "alpha", "title": "Legacy alpha",
                "identity_method": "legacy_import", "metadata": {"content_snapshot": content}},
                expected_revision=0)
    for doc in docs:
        row = env.documents.read(doc)
        env.documents.save_user_edit(doc, expected_revision=row["revision"],
            markdown=env.documents.markdown(doc), source_refs=[*row["source_refs"],
                {"source_id": source, "locator": "source://" + source}])
    return source


def test_duplicate_legacy_source_remains_a_candidate_without_bypassing_forgetting(env):
    doc, _ = add_document(env)
    source = legacy_source(env, [doc], content="alpha beta gamma")
    query = env.domains.query
    collected = query.collect_candidates("alpha", "alpha beta gamma?")
    assert any(c["kind"] == "source" and c["entry"]["id"] == source
               for c in collected["candidates"])
    plan = query.prepare_ask("alpha", "alpha beta gamma?", collected=collected)
    assert not any(c["kind"] == "source" for c in plan["chosen"])
    assert next(row for row in plan["trace"] if row["layer"] == "L0")["skipped_duplicate"] == 1
    query.validate_ask_plan(plan)
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc,
               {"project_id": "alpha", "state": "forgotten", "by": "user"}, expected_revision=0)
        tx.commit()
    collected = query.collect_candidates("alpha", "alpha beta gamma?")
    assert collected["candidates"] == []
    plan = query.prepare_ask("alpha", "alpha beta gamma?", collected=collected)
    consult_bookshelf(query, "alpha", "alpha beta gamma?", plan)
    assert plan["chosen"] == [] and plan["bookshelf"]["hits"] == 0


def test_legacy_source_of_forgotten_document_cannot_bypass_recall(env):
    doc, _ = add_document(env)
    legacy_source(env, [doc])
    visible = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    assert any(c["kind"] == "source" for c in visible["chosen"])
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc,
               {"project_id": "alpha", "state": "forgotten", "by": "user"}, expected_revision=0)
        tx.commit()
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    assert plan["chosen"] == []


def test_shared_source_keeps_the_unforgotten_document_path(env):
    old, _ = add_document(env)
    retained, _ = add_document(env)
    source = legacy_source(env, [old, retained])
    with env.records.begin() as tx:
        tx.put("v2_document_recall", old,
               {"project_id": "alpha", "state": "forgotten", "by": "user"}, expected_revision=0)
        tx.commit()
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    assert any(c["kind"] == "source" and c["entry"]["id"] == source for c in plan["chosen"])
    assert all(c["kind"] != "document" or c["entry"]["id"] != old for c in plan["chosen"])
    env.domains.query.validate_ask_plan(plan)


def test_sufficient_ask_records_spines_without_opening(env):
    retained, _ = publish(env)
    old, _ = publish(env)
    forgotten(env, old)
    response = ask(env)
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert [c["id"] for c in receipt["citations"]] == [retained.id]
    assert receipt["trace"][0]["bookshelf"] == {"hits": 1, "used": 0}
    assert receipt["context"]["bookshelf"]["spines"][0]["id"] == old.id
    assert env.records.read("recognition_recall_preferences", old.id).payload["state"] == "forgotten"


def test_cooled_document_spine_is_dynamic_and_relearning_normalizes(env):
    doc, _ = add_document(env)
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc, {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
        tx.commit()
    response = ask(env, text="alpha absent1 absent2 absent3?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["bookshelf"]["hits"] == 1
    assert any(c.get("bookshelf") for c in receipt["citations"])
    assert env.records.read("v2_document_recall", doc).payload["state"] == "normal"
    assert env.records.read("v2_usage_document", doc).payload["score"] >= 0.5


def test_bookshelf_document_windows_do_not_overlap_the_summary(env):
    doc, _ = add_document(env)
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc,
               {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
        tx.commit()
    question = "alpha absent1 absent2 absent3?"
    plan = env.domains.query.prepare_ask("alpha", question)
    consult_bookshelf(env.domains.query, "alpha", question, plan)
    chosen = next(c for c in plan["chosen"] if c.get("bookshelf"))
    windows = chosen["windows"]
    assert all(left.end <= right.start for left, right in zip(windows, windows[1:]))
    markdown = env.documents.markdown(doc)
    assert chosen["excerpt"] == "\n…\n".join(markdown[w.start:w.end] for w in windows)


def test_overlapping_windows_keep_all_evidence_without_exceeding_budget():
    from backend.memory_app.v2.bookshelf import _document_windows
    from backend.memory_app.v2.budget import text_tokens
    from core.search_and_recall.evidence_windows import EvidenceWindow

    markdown = "界" * 2000
    original = [EvidenceWindow(0, 1100, markdown[:1100]), EvidenceWindow(900, 2000, markdown[900:])]
    assert all(text_tokens(w.text) <= 1200 for w in original)
    assert text_tokens(markdown) > 1200
    windows = _document_windows(markdown, original)
    assert all(text_tokens(w.text) <= 1200 for w in windows)
    assert all(a.end <= b.start for a, b in zip(windows, windows[1:]))
    assert "".join(w.text for w in windows) == markdown
    assert all(w.text == markdown[w.start:w.end] for w in windows)


def test_at_most_two_spines_open_and_uncited_one_stays_forgotten(env):
    insights = [publish(env)[0] for _ in range(4)]
    for insight in insights:
        forgotten(env, insight)
    env.model.numbers = [1]
    response = ask(env)
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["bookshelf"] == {"hits": 4, "used": 2}
    states = [env.records.read("recognition_recall_preferences", i.id).payload["state"] for i in insights]
    assert states.count("cooled") == 1 and states.count("forgotten") == 3


def test_private_spines_are_excluded_and_mid_call_forget_aborts(env):
    from backend.memory_app.v2.privacy import set_private_project

    insight, _ = publish(env)
    forgotten(env, insight)
    set_private_project(env.records, "alpha", True, 0)
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    consult_bookshelf(env.domains.query, "alpha", "alpha beta gamma?", plan)
    assert plan["bookshelf"]["hits"] == 0
    row = env.records.read("v2_private_scopes", "alpha")
    set_private_project(env.records, "alpha", False, row.revision)
    pref = env.records.read("recognition_recall_preferences", insight.id)

    def revoke_answer():
        if '"queries"' not in env.model.messages[0]["content"]:
            set_preference(
                env.records,
                WorkScope("local-user", "alpha"),
                insight.id,
                recognition_revision=insight.revision,
                preference_revision=pref.revision,
                state="forgotten",
            )

    env.model.after = revoke_answer
    response = ask(env)
    assert response.status_code == 409 and response.json()["detail"] == "source_changed_retry"
    assert env.records.read("recognition_recall_preferences", insight.id).payload["by"] == "user"


def test_usage_write_failure_keeps_automatic_forgotten_state(env, monkeypatch):
    from backend.memory_app.v2.usage import UsageService

    insight, _ = publish(env)
    forgotten(env, insight)

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic write")

    monkeypatch.setattr(UsageService, "record_usage", broken)
    response = ask(env)
    assert response.status_code == 200, response.text
    assert env.records.read("recognition_recall_preferences", insight.id).payload["state"] == "forgotten"
    assert env.records.read("v2_usage_insight", insight.id) is None


def test_bookshelf_respects_same_budget_and_keeps_atomic_conditions(env):
    insight, _ = publish(env, text="alpha beta gamma " + "条件不可裁掉" * 1400)
    forgotten(env, insight)
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    consult_bookshelf(env.domains.query, "alpha", "alpha beta gamma?", plan)
    assert plan["bookshelf"]["hits"] == 1 and plan["bookshelf"]["used"] == 0
    assert plan["chosen"] == [] and plan["trace"][0]["skipped_budget"] == 1
    small, _ = publish(env)
    forgotten(env, small)
    env.model.generation_budget_limits = lambda **kwargs: {"window": 1, "reserve": 1}
    plan = env.domains.query.prepare_ask("alpha", "alpha beta gamma?")
    consult_bookshelf(env.domains.query, "alpha", "alpha beta gamma?", plan)
    assert plan["chosen"] == [] and plan["bookshelf"]["used"] == 0


def test_vectors_prioritize_synonym_spine_and_cache_without_body_copies(env, monkeypatch):
    from backend.memory_app.retrieval_models import ConfiguredTransport
    from tests.memory_app.v2.test_insight_links import VectorModel

    doc, _ = add_document(env)
    before = len(env.records.list("recognition_experiences"))
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc, {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
        tx.commit()
    original_public = env.model.public

    def public():
        return {**VectorModel().public(), **original_public()}

    env.model.public = public
    env.model.snapshot = lambda purpose: {**public()[purpose], "api_key": "synthetic-key"}
    calls = []

    def wire(transport, *, endpoint, payload):
        transport._check_current()
        calls.append(payload["input"])
        return {
            "data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(payload["input"])],
            "usage": {"prompt_tokens": 5},
        }

    monkeypatch.setattr(ConfiguredTransport, "post_json", wire)
    query = env.domains.query
    for expected_wire_count in (4, 6, 6):
        plan = query.prepare_ask("alpha", "synonym?")
        consult_bookshelf(query, "alpha", "synonym?", plan)
        assert plan["bookshelf"]["hits"] == 1 and plan["bookshelf"]["used"] == 1
        assert len(calls) == expected_wire_count
    assert calls == [
        ["synonym?", "Synthetic\nalpha\n# Synthetic\n\n## 摘要"],
        ["synonym?", "Synthetic\nalpha\n\n\n## 正文\nalpha"],
        ["synonym?", "Synthetic\nalpha\nalpha beta gamma"],
        ["synonym?", "Synthetic\nalpha"],
        ["synonym?"],
        ["synonym?"],
    ]
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(env.root / "recognition-vectors.sqlite3")) as cache:
        assert cache.execute("SELECT COUNT(*) FROM recognition_embedding_cache").fetchone()[0] == 4
        assert [row[1] for row in cache.execute("PRAGMA table_info(recognition_embedding_cache)")] == [
            "project_id", "recognition_id", "revision", "model_id", "vector_json"]
    assert len(env.records.list("recognition_experiences")) == before
    from tests.memory_app.v2.kernel_receipts import wire_receipts, requests
    receipts = wire_receipts(env.records)
    assert len(receipts) == 6 and all(r["status"] == "succeeded" for r in receipts)
    assert all(r["privacy"]["consent_refs"] == ["crp://default/model-settings/embedding"] for r in requests(env.records))
    env.model.public = lambda: {**public(), "embedding": {**VectorModel().public()["embedding"], "allow_remote": False}}
    # Avoid the changed public wrapper referring to itself through original_public.
    plan = query.prepare_ask("alpha", "synonym?")
    consult_bookshelf(query, "alpha", "synonym?", plan)
    assert len(calls) == 6 and plan["bookshelf"]["hits"] == 0


def test_document_full_entry_and_scene_revalidation_blocks_stale_spines(env):
    import pytest
    from backend.recognition import RecognitionError

    doc, _ = add_document(env)
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc, {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
        tx.commit()
    query = env.domains.query
    plan = query.prepare_ask("alpha", "alpha?")
    consult_bookshelf(query, "alpha", "alpha?", plan)
    env.documents.save_user_edit(doc, expected_revision=2, markdown="# Changed\n\n## 摘要\nalpha\n\n## 正文\nchanged")
    with pytest.raises(RecognitionError):
        plan["bookshelf_guard"]()


def test_opened_document_keeps_original_only_evidence(env):
    doc, _ = add_document(env, summary="alpha", body="alpha", original="alpha beta gamma")
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc, {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
        tx.commit()
    query = env.domains.query
    question = "alpha beta gamma absent1 absent2 absent3?"
    plan = query.prepare_ask("alpha", question)
    assert any(c["layer"] == "L0" and "beta gamma" in c["excerpt"] for c in plan["chosen"])
    consult_bookshelf(query, "alpha", question, plan)
    assert any(c["layer"] == "L0" and "beta gamma" in c["excerpt"] for c in plan["chosen"])


def test_book_replacement_preserves_selected_variant_window(env):
    from core.search_and_recall.evidence_windows import EvidenceWindow

    doc, _ = add_document(env, summary="alpha", body="filler " * 1200 + "needle")
    with env.records.begin() as tx:
        tx.put("v2_document_recall", doc, {"project_id": "alpha", "state": "cooled", "by": "auto"}, expected_revision=0)
        tx.commit()
    query = env.domains.query
    variant = next(c for c in query.collect_candidates("alpha", "needle")["candidates"] if c["layer"] == "L1")
    markdown = env.documents.markdown(doc)
    start = markdown.index("needle")
    # A previously bounded, real-coordinate variant hit must survive consultation.
    variant = {**variant, "windows": (EvidenceWindow(start, start + 6, "needle"),), "excerpt": "needle"}
    question = "alpha absent1 absent2 absent3?"
    plan = query.prepare_ask("alpha", question)
    plan["chosen"].append(variant)
    consult_bookshelf(query, "alpha", question, plan)
    opened = next(c for c in plan["chosen"] if c.get("bookshelf"))
    assert "needle" in opened["excerpt"]
    assert any(w.start == start and w.end == start + 6 for w in opened["windows"])


def test_both_eval_modes_use_same_material_and_no_models(tmp_path):
    from tools.memory_eval import seed

    fixture = {
        "documents": [],
        "insights": [{"id": "forgot-one", "project_id": "alpha", "text": "alpha beta gamma", "forgotten": True}],
    }
    for mode, hits in (("manual", 0), ("auto-forgotten", 1)):
        root = tmp_path / mode
        root.mkdir()
        query, identities = seed(root, fixture, forgetting_mode=mode)
        assert identities["forgot-one"] == "forgot-one"
        plan = query.prepare_ask("alpha", "alpha beta gamma?")
        consult_bookshelf(query, "alpha", "alpha beta gamma?", plan)
        assert plan["bookshelf"]["hits"] == hits
    assert fixture["insights"][0]["forgotten"] is True


def test_revoke_during_ranking_returns_existing_retry_response(env, monkeypatch):
    from backend.memory_app.v2.links import InsightLinks

    insight, _ = publish(env)
    forgotten(env, insight)
    original = InsightLinks._rank

    def ranked(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        pref = env.records.read("recognition_recall_preferences", insight.id)
        set_preference(
            env.records,
            WorkScope("local-user", "alpha"),
            insight.id,
            recognition_revision=insight.revision,
            preference_revision=pref.revision,
            state="forgotten",
        )
        return result

    monkeypatch.setattr(InsightLinks, "_rank", ranked)
    response = ask(env)
    assert response.status_code == 409 and response.json()["detail"] == "source_changed_retry"
