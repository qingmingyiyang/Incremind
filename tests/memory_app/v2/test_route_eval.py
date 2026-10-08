"""Offline routing metric regressions, without a model or application runtime."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "route_eval.py"


def load_evaluator():
    spec = importlib.util.spec_from_file_location("route_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def part(intent, span, depends_on=()):
    return {"intent": intent, "span": span, "instruction": None, "depends_on": list(depends_on)}


def fixture(parts=None, text="记下预算。预算多少？写份方案。", has_files=False):
    return {"version": 1, "cases": [{"id": "case-1", "category": "all_three", "text": text,
        "has_files": has_files, "parts": parts or [part("remember", "记下预算。"),
        part("ask", "预算多少？", [0]), part("do", "写份方案。", [0])],
        "tags": [], "rationale": "合成输入"}]}


def prediction(data):
    return {"version": 1, "cases": [{"id": row["id"], "parts": copy.deepcopy(row["parts"])} for row in data["cases"]]}


def test_perfect_predictions_and_equivalent_permutation():
    evaluate = load_evaluator().evaluate
    data = fixture()
    expected = evaluate(data, prediction(data))
    for name in ("intent_set_accuracy", "span_accuracy", "dependency_accuracy"):
        assert expected["metrics"][name]["accuracy"] == 1
    pred = prediction(data)
    pred["cases"][0]["parts"] = [part("do", "写份方案。", [1]), part("remember", "记下预算。"), part("ask", "预算多少？", [1])]
    assert evaluate(data, pred)["metrics"] == expected["metrics"]


def test_only_boundary_punctuation_and_whitespace_are_ignored():
    module = load_evaluator()
    data = fixture([part("remember", "今天，晴天。")], "今天，晴天。")
    pred = prediction(data)
    pred["cases"][0]["parts"][0]["span"] = "  今天，晴天！ \n"
    assert module.evaluate(data, pred)["metrics"]["span_accuracy"]["accuracy"] == 1
    pred["cases"][0]["parts"][0]["span"] = "今天晴天"
    assert module.evaluate(data, pred)["metrics"]["span_accuracy"]["accuracy"] == 0


def test_wrong_dependency_is_scored_by_identity():
    module = load_evaluator()
    data = fixture()
    pred = prediction(data)
    pred["cases"][0]["parts"][2]["depends_on"] = [1]
    metrics = module.evaluate(data, pred)["metrics"]
    assert metrics["span_accuracy"]["accuracy"] == 1
    assert metrics["dependency_accuracy"] == {"correct": 2, "total": 3, "accuracy": 2 / 3}


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "wrong_intent"])
def test_incomplete_or_extra_parts_cannot_get_full_span_or_dependency_credit(change):
    module = load_evaluator()
    data = fixture()
    pred = prediction(data)
    parts = pred["cases"][0]["parts"]
    if change == "missing":
        parts.pop()
    elif change == "duplicate":
        parts.append(copy.deepcopy(parts[0]))
    elif change == "extra":
        parts.append(part("ask", "额外内容"))
    else:
        parts[0]["intent"] = "ask"
    metrics = module.evaluate(data, pred)["metrics"]
    assert metrics["span_accuracy"]["accuracy"] < 1
    assert metrics["dependency_accuracy"]["accuracy"] < 1


def test_missing_case_is_zero_and_unknown_case_is_error():
    module = load_evaluator()
    data = fixture()
    result = module.evaluate(data, {"version": 1, "cases": []})
    assert all(metric["accuracy"] == 0 for metric in result["metrics"].values())
    pred = prediction(data)
    pred["cases"][0]["id"] = "unknown"
    with pytest.raises(ValueError, match="unknown"):
        module.evaluate(data, pred)


@pytest.mark.parametrize("change", ["duplicate_id", "version", "files", "intent", "span", "dependency", "empty_cases"])
def test_invalid_fixture_is_rejected(change):
    module = load_evaluator()
    data = fixture()
    row = data["cases"][0]
    if change == "duplicate_id":
        data["cases"].append(copy.deepcopy(row))
    elif change == "version":
        data["version"] = True
    elif change == "files":
        row["has_files"] = "false"
    elif change == "intent":
        row["parts"][0]["intent"] = "invalid"
    elif change == "span":
        row["parts"][0]["span"] = "原文没有的内容"
    elif change == "dependency":
        row["parts"][0]["depends_on"] = [9]
    else:
        data["cases"] = []
    with pytest.raises(ValueError):
        module.evaluate(data)


def test_baseline_calls_real_rules_and_preserves_files_and_scope_body():
    module = load_evaluator()
    data = fixture([part("ask", "怎么安排？")], "#项目/场景 怎么安排？")
    result = module.evaluate(data)
    assert result["cases"][0]["predicted_parts"] == [part("ask", "怎么安排？")]
    data["cases"][0]["has_files"] = True
    assert module.evaluate(data)["cases"][0]["predicted_parts"][0]["intent"] == "remember"
    data = fixture([part("remember", "第一行\n#项目 第二行")], "第一行\n#项目 第二行")
    result = module.evaluate(data)
    assert result["cases"][0]["predicted_parts"][0]["span"] == data["cases"][0]["text"]
    assert result["cases"][0]["baseline_span_fallback"] is True


def test_file_only_empty_span_and_inspiration():
    module = load_evaluator()
    data = fixture([part("remember", "")], "", True)
    assert module.evaluate(data)["metrics"]["span_accuracy"]["accuracy"] == 1
    data = fixture([part("inspiration", "灵感：做一个书架")], "灵感：做一个书架")
    assert module.evaluate(data)["metrics"]["intent_set_accuracy"]["accuracy"] == 1


def test_cli_external_predictions_and_source_bootstrap(tmp_path):
    data = fixture()
    source = tmp_path / "cases.json"
    guesses = tmp_path / "predictions.json"
    output = tmp_path / "nested" / "result.json"
    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    guesses.write_text(json.dumps(prediction(data), ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run([sys.executable, str(SCRIPT), "--cases", str(source), "--predictions", str(guesses),
        "--output", str(output)], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["metrics"]["dependency_accuracy"]["accuracy"] == 1


@pytest.mark.parametrize("change", ["duplicate_id", "bool_dependency", "duplicate_dependency", "missing_parts", "null_part"])
def test_invalid_predictions_are_rejected(change):
    module = load_evaluator()
    data = fixture()
    pred = prediction(data)
    row = pred["cases"][0]
    if change == "duplicate_id":
        pred["cases"].append(copy.deepcopy(row))
    elif change == "bool_dependency":
        row["parts"][1]["depends_on"] = [False]
    elif change == "duplicate_dependency":
        row["parts"][1]["depends_on"] = [0, 0]
    elif change == "missing_parts":
        del row["parts"]
    else:
        row["parts"][0] = None
    with pytest.raises(ValueError):
        module.evaluate(data, pred)


@pytest.mark.parametrize("change", ["cycle", "three_layers", "self_edge"])
def test_invalid_gold_dependency_graph_is_rejected(change):
    module = load_evaluator()
    data = fixture()
    parts = data["cases"][0]["parts"]
    if change == "cycle":
        parts[0]["depends_on"] = [1]
    elif change == "three_layers":
        parts[2]["depends_on"] = [1]
    else:
        parts[0]["depends_on"] = [0]
    with pytest.raises(ValueError):
        module.evaluate(data)


def test_overlapping_gold_spans_are_rejected():
    module = load_evaluator()
    data = fixture([part("remember", "记下预算"), part("ask", "预算多少")], "记下预算多少")
    with pytest.raises(ValueError, match="overlap"):
        module.evaluate(data)


def test_repeated_gold_text_can_use_separate_occurrences():
    module = load_evaluator()
    data = fixture([part("remember", "记下"), part("ask", "记下")], "记下，记下")
    metrics = module.evaluate(data, prediction(data))["metrics"]
    assert all(metric["accuracy"] == 1 for metric in metrics.values())


def test_complete_synthetic_corpus_has_a_perfect_score():
    module = load_evaluator()
    data = json.loads(module.DEFAULT_CASES.read_text(encoding="utf-8-sig"))
    report = module.evaluate(data, prediction(data))
    assert report["case_count"] >= 60
    assert all(metric["accuracy"] == 1 for metric in report["metrics"].values())
