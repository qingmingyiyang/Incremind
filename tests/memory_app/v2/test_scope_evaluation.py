"""Annotated scene fixtures exercise the same real offline CLI pipeline."""
import json
from pathlib import Path

from backend.memory_app.v2.policies import override
from tools.memory_eval import evaluate


CORPUS = Path(__file__).resolve().parents[2] / "fixtures/memory_eval/corpus.json"


def test_scene_category_has_six_annotated_method_fact_and_sibling_cases():
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    questions = [item for item in fixture["questions"] if item["category"] == "scene_inheritance"]
    assert len(questions) >= 6
    assert all(item["scene"] == "casey" and item["require_all"] is True for item in questions)
    assert all(len(item["expected_ids"]) == 2 and len(item["forbidden_ids"]) == 3 for item in questions)
    assert all(set(item["expected_evidence"]) == set(item["expected_ids"]) for item in questions)
    assert len({item["id"] for item in fixture["questions"]}) == len(fixture["questions"])


def test_real_scene_evaluation_uses_annotations_and_improves_without_model_calls(tmp_path):
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    selected = {name: [item for item in fixture[name]
                       if (item.get("project_id") or "").startswith("eval-inherit-")]
                for name in ("documents", "insights", "questions")}
    path = tmp_path / "scope-corpus.json"
    path.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
    reports = []
    for version in ("@1", "@2"):
        with override(scope=version):
            reports.append(evaluate(path))
    assert [report["categories"]["scene_inheritance"]["hits"] for report in reports] == [0, 6]
    assert all(report["model_attempts"] == 0 for report in reports)
    assert all(row["hit"] and not set(row["forbidden_ids"]) & set(row["selected_ids"])
               for row in reports[1]["questions"])
