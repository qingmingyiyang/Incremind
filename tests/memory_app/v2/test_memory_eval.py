import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_category_scoring_requires_complete_and_current_evidence():
    from tools.memory_eval import score_selection

    chosen = [{"id": "new", "excerpt": "new answer"}, {"id": "old", "excerpt": "old answer"}]
    update = {"category": "knowledge_update", "expected_ids": ["new"], "forbidden_ids": ["old"]}
    assert score_selection(update, chosen, {}, .8)["hit"] is False
    assert score_selection(update, chosen[:1], {}, .8)["hit"] is True
    multi = {"category": "multi_source", "expected_ids": ["new", "other"]}
    assert score_selection(multi, chosen[:1], {}, .8)["hit"] is False
    assert score_selection(multi, chosen[:1] + [{"id": "other", "excerpt": "other"}], {}, .8)["hit"] is True
    long = {"category": "long_document", "expected_ids": ["new"], "answer_text": "tail answer"}
    assert score_selection(long, chosen[:1], {}, .8)["hit"] is False
    assert score_selection(long, [{"id": "new", "excerpt": "tail answer"}], {}, .8)["hit"] is True
    assert score_selection(long, [{"id": "new", "excerpt": "head"}, {"id": "other", "excerpt": "tail answer"}], {}, .8)["hit"] is False


def test_abstention_scoring_uses_actual_coverage_boundary():
    from tools.memory_eval import score_selection

    question = {"category": "abstention", "expected_ids": [], "should_abstain": True}
    assert score_selection(question, [], {}, 1)["hit"] is True
    chosen = [{"id": "other", "excerpt": "unrelated"}]
    assert score_selection(question, chosen, {}, .599)["hit"] is True
    failed = score_selection(question, chosen, {}, .6)
    assert failed["hit"] is False
    assert failed["false_recall"] is True
    assert failed["should_abstain"] is True


def test_citation_correctness_counts_selected_objects_and_preserves_legacy_hit():
    from tools.memory_eval import score_selection

    question = {"category": "detail", "expected_ids": ["doc"]}
    selected = [{"id": "actual", "excerpt": "a"}, {"id": "actual", "excerpt": "b"}, {"id": "noise", "excerpt": "c"}]
    row = score_selection(question, selected, {"actual": "doc"}, .8)
    assert row["hit"] is True
    assert row["citation_correct_count"] == 1
    assert row["citation_count"] == 2
    assert row["recall_count"] == 3


def test_expected_evidence_must_belong_to_each_expected_object():
    from tools.memory_eval import score_selection

    item = {"category": "multi_source", "expected_ids": ["a", "b"],
            "expected_evidence": {"a": "第一事实", "b": "第二事实"}}
    selected = [{"id": "a", "excerpt": "第一事实与第二事实"}, {"id": "b", "excerpt": "只有标题"}]
    scored = score_selection(item, selected, {}, .9)
    assert scored["hit"] is False
    assert scored["citation_correct_count"] == 2
    selected[1]["excerpt"] = "第二事实"
    assert score_selection(item, selected, {}, .9)["hit"] is True


def test_extended_corpus_has_grounded_annotations_and_late_long_answers():
    fixture = json.loads((ROOT / "tests/fixtures/memory_eval/corpus.json").read_text(encoding="utf8"))
    assert Counter(q["category"] for q in fixture["questions"]) == dict.fromkeys(
        ["detail", "overview", "followup", "synonym", "forgotten", "knowledge_update",
         "temporal", "abstention", "multi_source", "long_document", "scene_inheritance"], 6)
    docs = {d["id"]: d for d in fixture["documents"]}
    insights = {i["id"]: i for i in fixture["insights"]}
    objects = {**docs, **insights}
    known = set(objects) | {d["source_id"] for d in docs.values()}
    assert len(known) == len(docs) * 2 + len(insights)
    assert len({q["id"] for q in fixture["questions"]}) == 66
    for q in fixture["questions"]:
        assert set(q["expected_ids"]) <= known
        assert set(q.get("forbidden_ids", [])) <= known
        assert not set(q["expected_ids"]) & set(q.get("forbidden_ids", []))
        for identity, answer in q.get("expected_evidence", {}).items():
            obj = objects[identity]
            assert obj["project_id"] == q["project_id"]
            assert answer in obj.get("body", obj.get("text", ""))
        if q["category"] == "knowledge_update":
            new = insights[q["expected_ids"][0]]
            assert new["confirmed_supersedes"] is True
            assert new["supersedes"] in q["forbidden_ids"]
        if q["category"] == "temporal":
            assert q["expected_evidence"]
            assert all(identity in insights for identity in q["expected_ids"])
        if q["category"] == "abstention":
            assert q["should_abstain"] is True
            assert q["expected_ids"] == []
        if q["category"] == "multi_source":
            assert 2 <= len(q["expected_ids"]) <= 3
            assert set(q["expected_evidence"]) == set(q["expected_ids"])
        if q["category"] == "long_document":
            doc = docs[q["expected_ids"][0]]
            body = doc["body"]
            assert len(body) >= 8000
            start, end = q["answer_locator"]["start"], q["answer_locator"]["end"]
            assert start >= len(body) / 2
            assert body[start:end] == q["answer_text"]
            assert q["answer_text"] not in doc["summary"]


def test_seed_records_historical_publication_and_confirmed_supersession(tmp_path):
    from tools.memory_eval import EVALUATION_TIME, seed

    fixture = json.loads((ROOT / "tests/fixtures/memory_eval/corpus.json").read_text(encoding="utf8"))
    pair = next(i for i in fixture["insights"] if i.get("confirmed_supersedes"))
    chosen_ids = {pair["id"], pair["supersedes"]}
    insights = [i for i in fixture["insights"] if i["id"] in chosen_ids]
    document_ids = {i.get("document_id") for i in insights}
    small = {"documents": [d for d in fixture["documents"] if d["id"] in document_ids], "insights": insights}
    query, _ = seed(tmp_path, small)
    for item in insights:
        row = query.records.read("recognitions", item["id"])
        assert row.revision == 1
        assert row.payload["created_at"] == item["created_at"]
    interference = query.records.read("v2_insight_interference", pair["supersedes"])
    assert interference is not None
    assert pair["created_at"] in interference.payload.values()
    proposals = query.records.list("recognition_relation_proposals")
    assert len(proposals) == 1
    assert proposals[0].payload["created_at"] == pair["created_at"]
    assert proposals[0].payload["reviewed_at"] == pair["created_at"]
    dates = {item["id"]: item["created_at"] for item in insights}
    versions = query.records.list("recognition_versions")
    assert len(versions) == 2
    assert all(row.payload["recorded_at"] == dates[row.payload["recognition_id"]] for row in versions)
    assert query.records.read("recognition_recall_preferences", pair["supersedes"]).payload["state"] == "cooled"
    experiences = query.records.list("recognition_experiences")
    assert len(experiences) == 2
    assert all(row.payload["created_at"] == EVALUATION_TIME for row in experiences)
    assert query.models.attempts == 0


def test_small_evaluation_is_repeatable_including_rendered_source_evidence(tmp_path):
    from tools.memory_eval import evaluate

    fixture = json.loads((ROOT / "tests/fixtures/memory_eval/corpus.json").read_text(encoding="utf8"))
    projects = {"eval-update-1", "eval-time-1"}
    docs = [d for d in fixture["documents"] if d["project_id"] in projects or d["id"] == "doc-01"]
    doc_ids = {d["id"] for d in docs}
    small = {"documents": docs,
             "insights": [i for i in fixture["insights"] if i.get("document_id") in doc_ids or i["id"] == "persona-1"],
             "questions": [q for q in fixture["questions"] if q["id"] in {"q-overview-1", "q-update-1", "q-temporal-1"}]}
    assert len(small["questions"]) == 3
    path = tmp_path / "clock-corpus.json"
    path.write_text(json.dumps(small, ensure_ascii=False), encoding="utf8")
    first, second = evaluate(path), evaluate(path)
    assert first == second
    assert any('"recorded_at"' in e["excerpt"] for q in first["questions"] for e in q["selected_evidence"])
    assert str(tmp_path) not in json.dumps(first)


def test_evaluation_cli_is_repeatable_and_uses_annotated_objects(tmp_path):
    reports = []
    for index in range(2):
        output = tmp_path / f"eval-{index}.json"
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/memory_eval.py"), "--output", str(output)],
            cwd=ROOT, capture_output=True, text=True, timeout=900,
        )
        assert result.returncode == 0, result.stderr
        reports.append(json.loads(output.read_text(encoding="utf-8")))
    assert reports[0] == reports[1]
    report = reports[0]
    assert report["fixture_counts"] == {"documents": 86, "sources": 86, "insights": 79, "questions": 72}
    assert set(report["categories"]) == {"overview", "detail", "synonym", "followup", "forgotten",
                                         "knowledge_update", "temporal", "abstention", "multi_source", "long_document",
                                         "scene_inheritance", "comparative_extraction"}
    assert len(report["questions"]) == 72
    assert sum(c["total"] for c in report["categories"].values()) == 72
    assert report["categories"]["scene_inheritance"]["total"] == 6
    assert report["categories"]["comparative_extraction"]["total"] == 6
    assert sum(c["total"] for name, c in report["categories"].items()
        if name not in {"scene_inheritance", "comparative_extraction"}) == 60
    recalled = [q for q in report["questions"] if q["category"] != "comparative_extraction"]
    comparisons = [q for q in report["questions"] if q["category"] == "comparative_extraction"]
    assert len(recalled) == 66 and len(comparisons) == 6
    assert all((q["expected_ids"] or q["should_abstain"]) and isinstance(q["selected_ids"], list) for q in recalled)
    assert all(q["estimated_tokens"] >= 0 for q in recalled)
    assert all(q["expected"] and isinstance(q["actual"]["candidates"], list)
        and isinstance(q["actual"]["supports"], list) for q in comparisons)
    assert all(q["synthetic_model_attempts"] == 1 and q["remote_model_attempts"] == 0
        and q["estimated_prompt_tokens"] >= 0 and q["estimated_completion_tokens"] >= 0 for q in comparisons)
    assert report["categories"]["overview"]["hits"] > 0
    assert report["categories"]["detail"]["hits"] > 0
    assert report["categories"]["forgotten"]["hits"] == 0
    assert any(identity.startswith("insight-") for q in recalled for identity in q["selected_ids"])
    assert not any(identity.startswith("persona-") for q in recalled for identity in q["selected_ids"])
    # Profile context is independent of the numbered evidence scored by this CLI.
    from tools.memory_eval import seed
    from backend.memory_app.v2.profile import profile_messages
    fixture = json.loads((ROOT / "tests/fixtures/memory_eval/corpus.json").read_text(encoding="utf8"))
    query, identities = seed(tmp_path / "profile-context", fixture)
    question = fixture["questions"][0]
    plan = query.prepare_ask(question["project_id"], question["question"])
    persona_ids = {actual for logical, actual in identities.items() if logical.startswith("persona-")}
    profile = plan["profile"]
    assert persona_ids
    assert {item["id"] for item in profile["items"]}.issubset(persona_ids)
    assert profile["items"] and 0 < profile["tokens"] <= 600
    assert all(item["content"] in profile["text"] for item in profile["items"])
    evidence_message = {"role": "user", "content": "\n\n".join(c["excerpt"] for c in plan["chosen"])}
    messages = profile_messages(profile, [evidence_message])
    assert messages == [{"role": "system", "content": profile["text"]}, evidence_message]
    assert not persona_ids.intersection(c["id"] for c in plan["chosen"])
    legacy = {"overview", "detail", "synonym", "followup", "forgotten"}
    assert all(q["hit"] == bool(set(q["expected_ids"]) & set(q["selected_ids"]))
               for q in report["questions"] if q["category"] in legacy)
    assert report["model_attempts"] == 0
    assert report['synthetic_model_attempts'] == 6 and report['remote_model_attempts'] == 0
    for name, category in report["categories"].items():
        assert category["total"] == 6
        if name == 'comparative_extraction':
            assert category['synthetic_model_attempts'] == 6 and category['remote_model_attempts'] == 0
            assert category['average_prompt_tokens'] >= 0 and category['average_completion_tokens'] >= 0
            continue
        assert category["average_recall_count"] >= 0
        assert category["citation_correct_count"] <= category["citation_count"]
    abstention = report["categories"]["abstention"]
    assert abstention["false_recalls"] == abstention["total"] - abstention["hits"]
