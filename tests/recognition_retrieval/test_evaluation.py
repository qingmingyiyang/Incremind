from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _ROOT / "work/scripts/evaluate_recognition.py"
_SPEC = importlib.util.spec_from_file_location("evaluate_recognition", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
evaluation = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(evaluation)


def _entry(item_id: str, **overrides: object) -> dict[str, object]:
    return {
        "id": item_id,
        "revision": 1,
        "current_revision": 1,
        "project_id": "project-a",
        "content": f"content for {item_id}",
        "status": "active",
        "authorized": True,
        "source_refs": [f"experience:{item_id}"],
        **overrides,
    }


def _fixture() -> dict[str, object]:
    return {
        "version": "real-holdout-v1",
        "data_class": "approved_deidentified_holdout",
        "entries": [_entry("expected"), _entry("distractor"), _entry("blocked", authorized=False)],
        "cases": [
            {
                "case_id": "answerable",
                "dataset_split": "heldout",
                "project_id": "project-a",
                "query": "answerable query",
                "expected_ids": ["expected"],
                "forbidden_ids": ["blocked"],
            },
            {
                "case_id": "no-answer",
                "dataset_split": "heldout",
                "project_id": "project-a",
                "query": "no answer query",
                "expected_ids": [],
                "forbidden_ids": ["distractor", "blocked"],
            },
        ],
    }


def _retriever(order_by_query: dict[str, list[str]]):
    def invoke(project_id, query, entries, *, limit, **_options):
        assert project_id == "project-a"
        assert limit >= 5
        hits = [SimpleNamespace(id=item_id) for item_id in order_by_query[query]]
        return SimpleNamespace(hits=hits, payload=lambda: {"trace": {
            "project_id": project_id,
            "api_key": "must-not-leak",
            "vector": {"status": {"unexpected": "structure"}, "reason": "must-not-leak"},
        }})
    return invoke


def _temporal_fixture():
    fixture = _fixture()
    fixture["temporal_policy"] = "as_of_task"
    for entry in fixture["entries"]:
        entry["available_at"] = "2026-09-15T09:00:00+08:00"
    for case in fixture["cases"]:
        case["occurred_at"] = "2026-09-15T01:00:00Z"
    return fixture


def test_temporal_filter_hides_future_corpus_and_detects_leaked_results():
    fixture = _temporal_fixture()
    fixture["entries"][1]["available_at"] = "2026-09-15T01:00:01Z"
    seen = []

    def retrieve_at_time(project_id, query, entries, **kwargs):
        seen.append([entry["id"] for entry in entries])
        return SimpleNamespace(hits=[SimpleNamespace(id="distractor")], trace={
            "vector": {"status": "used", "candidate_ids": ["distractor"]}})

    report = evaluation.evaluate(retrieve_at_time, fixture=fixture)
    assert seen == [["expected", "blocked"], ["expected", "blocked"]]
    assert report["temporal_validation"] == "as_of_task"
    for case in report["cases"]:
        assert case["future_entries_excluded"] == 1
        assert case["invalid_or_unauthorized_ids"] == ["distractor"]
        assert case["candidate_stages"]["vector"]["invalid_or_unauthorized_ids"] == ["distractor"]


@pytest.mark.parametrize("label", ["expected_ids", "optional_ids"])
def test_future_positive_temporal_labels_are_rejected(label):
    fixture = _temporal_fixture()
    fixture["entries"][1]["available_at"] = "2026-09-15T01:00:01Z"
    fixture["cases"][0][label] = ["distractor"]
    with pytest.raises(ValueError, match="unavailable at task time"):
        evaluation.validate_fixture(fixture)


@pytest.mark.parametrize("value", [None, "2026-09-15", "2026-09-15T01:00:00", "not-a-date"])
@pytest.mark.parametrize("group,field", [("entries", "available_at"), ("cases", "occurred_at")])
def test_temporal_dates_require_explicit_valid_timezones(group, field, value):
    fixture = _temporal_fixture()
    fixture[group][0][field] = value
    with pytest.raises(ValueError, match=field):
        evaluation.validate_fixture(fixture)


def test_temporal_metadata_requires_policy_and_legacy_reports_unverified():
    fixture = _temporal_fixture()
    del fixture["temporal_policy"]
    with pytest.raises(ValueError, match="requires as_of_task"):
        evaluation.validate_fixture(fixture)
    report = evaluation.evaluate(_retriever({"answerable query": ["expected"], "no answer query": []}), fixture=_fixture())
    assert report["temporal_validation"] == "not_provided"
    assert report["temporal_split_validation"] == "not_provided"


@pytest.mark.parametrize("heldout_time", ["2026-09-15T09:00:00+08:00", "2026-09-15T00:59:59Z"])
def test_temporal_holdout_rejects_equal_or_earlier_tasks_before_retrieval(heldout_time):
    fixture = _temporal_fixture()
    for entry in fixture["entries"]:
        entry["available_at"] = "2026-09-14T00:00:00Z"
    fixture["cases"][0]["dataset_split"] = "development"
    fixture["cases"][1]["occurred_at"] = heldout_time
    calls = []
    with pytest.raises(ValueError, match="strictly later"):
        evaluation.evaluate(lambda *args, **kwargs: calls.append(args), fixture=fixture)
    assert calls == []


def test_temporal_split_reports_chronological_or_single_partition_evidence():
    fixture = _temporal_fixture()
    retriever = _retriever({"answerable query": ["expected"], "no answer query": []})
    assert evaluation.evaluate(retriever, fixture=fixture)["temporal_split_validation"] == "single_split_only"
    fixture["cases"][0]["dataset_split"] = "development"
    fixture["cases"][1]["occurred_at"] = "2026-09-15T09:00:01+08:00"
    # Input ordering does not determine the temporal split.
    fixture["cases"].reverse()
    report = evaluation.evaluate(retriever, fixture=fixture)
    assert report["temporal_split_validation"] == "development_before_heldout"


def test_timestamped_split_rejects_unknown_label_instead_of_ignoring_it():
    fixture = _temporal_fixture()
    fixture["cases"][0]["dataset_split"] = "developmnt"
    with pytest.raises(ValueError, match="only development or heldout"):
        evaluation.validate_fixture(fixture)


def test_ranking_metrics_distinguish_ordering_with_the_same_hit_set() -> None:
    fixture = _fixture()
    fixture["cases"] = [fixture["cases"][0]]
    better = evaluation.evaluate(
        _retriever({"answerable query": ["expected", "distractor"]}), fixture=fixture, top_k=2
    )
    worse = evaluation.evaluate(
        _retriever({"answerable query": ["distractor", "expected"]}), fixture=fixture, top_k=2
    )

    good = better["summary"]["heldout"]
    bad = worse["summary"]["heldout"]
    assert good["mean_recall_at_k"] == bad["mean_recall_at_k"] == 1.0
    assert good["mean_precision_at_k"] == bad["mean_precision_at_k"] == 0.5
    assert good["mean_mrr_at_k"] == 1.0
    assert bad["mean_mrr_at_k"] == 0.5
    assert good["mean_ndcg_at_k"] > bad["mean_ndcg_at_k"]
    assert "api_key" not in better["cases"][0]["trace"]
    assert better["cases"][0]["trace"]["vector"] == {}


def test_no_answer_group_is_separate_and_never_divides_by_zero() -> None:
    result = evaluation.evaluate(
        _retriever({"answerable query": ["expected"], "no answer query": ["distractor"]}),
        fixture=_fixture(),
        top_k=1,
    )

    summary = result["summary"]["heldout"]
    no_answer = result["cases"][1]
    assert summary["answerable_cases"] == 1
    assert summary["no_answer_cases"] == 1
    assert summary["no_answer_false_recall_cases"] == 1
    assert no_answer["recall_at_k"] is None
    assert no_answer["mrr_at_k"] is None
    assert no_answer["ndcg_at_k"] is None
    assert result["summary_by_data_class_and_split"]["approved_deidentified_holdout:heldout"] == summary


def test_an_all_no_answer_split_reports_null_ranking_means() -> None:
    fixture = _fixture()
    fixture["cases"] = [fixture["cases"][1]]
    result = evaluation.evaluate(_retriever({"no answer query": []}), fixture=fixture, top_k=3)

    summary = result["summary"]["heldout"]
    assert summary["answerable_cases"] == 0
    assert summary["no_answer_cases"] == 1
    assert summary["mean_recall_at_5"] is None
    assert summary["mean_precision_at_5_answerable"] is None
    assert summary["mean_recall_at_k"] is None
    assert summary["mean_mrr_at_k"] is None
    assert summary["mean_ndcg_at_k"] is None


def test_custom_fixture_path_is_loaded_without_accessing_application_data(tmp_path) -> None:
    path = tmp_path / "approved-heldout.json"
    fixture = _fixture()
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    result = evaluation.evaluate(
        _retriever({"answerable query": ["expected"], "no answer query": []}), fixture_path=path, top_k=2
    )

    assert result["dataset"] == "real-holdout-v1"
    assert result["data_class"] == "approved_deidentified_holdout"
    assert result["top_k"] == 2


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["cases"][0].update(expected_ids=["blocked"], forbidden_ids=[]), "must be current, authorized"),
        (lambda value: value["cases"][0].update(expected_ids=["expected", "expected"]), "duplicate IDs"),
        (lambda value: value["cases"][0].update(forbidden_ids=["unknown"]), "unknown ID"),
        (lambda value: value["entries"].append(_entry("expected", project_id="project-b")), "globally unique"),
    ],
)
def test_invalid_golden_and_ambiguous_catalog_entries_are_rejected(mutate, message: str) -> None:
    fixture = copy.deepcopy(_fixture())
    mutate(fixture)

    with pytest.raises(ValueError, match=message):
        evaluation.evaluate(_retriever({"answerable query": [], "no answer query": []}), fixture=fixture)


def test_duplicate_and_illegal_returned_ids_are_reported_without_set_masking() -> None:
    result = evaluation.evaluate(
        _retriever({"answerable query": ["expected", "expected", "blocked", "unknown"]}),
        fixture={**_fixture(), "cases": [_fixture()["cases"][0]]},
        top_k=4,
    )

    row = result["cases"][0]
    assert row["actual_ids"] == ["expected", "expected", "blocked", "unknown"]
    assert row["duplicate_hit_ids"] == ["expected"]
    assert row["invalid_or_unauthorized_ids"] == ["blocked", "unknown"]
    assert row["forbidden_ids"] == ["blocked"]
    assert result["summary"]["heldout"]["duplicate_hit_count"] == 1


def test_ndcg_uses_rank_discounts_and_counts_a_repeated_relevant_id_once() -> None:
    fixture = {
        "version": "ranking-v1",
        "data_class": "synthetic_non_personal",
        "entries": [_entry("a"), _entry("b"), _entry("x")],
        "cases": [{
            "case_id": "ranking", "dataset_split": "heldout", "project_id": "project-a", "query": "ranking query",
            "expected_ids": ["a", "b"], "forbidden_ids": [],
        }],
    }
    ranked = evaluation.evaluate(_retriever({"ranking query": ["x", "a", "b"]}), fixture=fixture, top_k=3)
    duplicated = evaluation.evaluate(_retriever({"ranking query": ["a", "a", "b"]}), fixture=fixture, top_k=3)

    ranked_row = ranked["cases"][0]
    duplicate_row = duplicated["cases"][0]
    assert ranked_row["mrr_at_k"] == 0.5
    assert ranked_row["recall_at_k"] == 1.0
    assert ranked_row["precision_at_k"] == pytest.approx(2 / 3)
    assert ranked_row["ndcg_at_k"] == pytest.approx(0.6934264036)
    assert duplicate_row["duplicate_hit_ids"] == ["a"]
    assert duplicate_row["ndcg_at_k"] < 1.0


def test_optional_hits_count_as_allowed_context_without_inflating_required_recall():
    fixture = _fixture()
    fixture["cases"] = [dict(fixture["cases"][0], optional_ids=["distractor"])]
    result = evaluation.evaluate(_retriever({"answerable query": ["distractor", "distractor"]}),
                                 fixture=fixture, top_k=2)
    row = result["cases"][0]
    assert row["recall_at_k"] == row["mrr_at_k"] == row["ndcg_at_k"] == 0
    assert row["allowed_precision_at_k"] == row["returned_allowed_precision_at_k"] == 0.5
    assert row["optional_hit_ids"] == ["distractor"]
    assert row["duplicate_hit_ids"] == ["distractor"]
    assert result["summary"]["heldout"]["mean_allowed_precision_at_k"] == 0.5


def test_optional_only_cases_are_separate_from_required_and_no_answer_cases():
    fixture = _fixture()
    fixture["cases"][0].update(expected_ids=[], optional_ids=["expected"])
    result = evaluation.evaluate(_retriever({"answerable query": ["expected"], "no answer query": []}),
                                 fixture=fixture, top_k=1)
    summary = result["summary"]["heldout"]
    assert summary["optional_only_cases"] == 1
    assert summary["answerable_cases"] == 0
    assert summary["no_answer_cases"] == 1
    assert summary["no_answer_false_recall_cases"] == 0
    assert summary["mean_recall_at_k"] is None
    assert summary["mean_allowed_precision_at_k"] == 1.0


@pytest.mark.parametrize("optional", [["expected"], ["blocked"], ["unknown"], ["distractor", "distractor"]])
def test_invalid_optional_labels_fail_before_retrieval(optional):
    fixture = _fixture()
    fixture["cases"][0]["optional_ids"] = optional
    def must_not_run(*args, **kwargs):
        pytest.fail("invalid labels reached retrieval")
    with pytest.raises(ValueError):
        evaluation.evaluate(must_not_run, fixture=fixture)


@pytest.mark.parametrize("changes", [{"project_id": "project-b"}, {"authorized": False}, {"status": "revoked"}, {"current_revision": 2}])
def test_optional_labels_cannot_bypass_current_project_authority(changes):
    fixture = _fixture()
    fixture["entries"][1].update(changes)
    fixture["cases"][0]["optional_ids"] = ["distractor"]
    with pytest.raises(ValueError, match="current, authorized"):
        evaluation.validate_fixture(fixture)


def test_candidate_coverage_distinguishes_retrieval_from_final_ranking_loss():
    fixture = _fixture()
    fixture["cases"] = [fixture["cases"][0]]
    def retrieve(*args, **kwargs):
        return SimpleNamespace(hits=[SimpleNamespace(id="distractor")], payload=lambda: {"trace": {
            "keyword": {"status": "used", "candidate_ids": ["distractor", "expected"]},
            "vector": {"status": "used", "candidate_ids": ["expected", "expected", "blocked"]},
            "rerank": {"status": "degraded", "reason": "private-error"},
        }})
    report = evaluation.evaluate(retrieve, fixture=fixture, top_k=1)
    row = report["cases"][0]
    assert row["recall_at_k"] == 0
    assert row["candidate_stages"]["keyword"]["recall_at_k"] == 0
    assert row["candidate_stages"]["keyword"]["recall_at_reported_depth"] == 1
    assert row["candidate_stages"]["vector"]["recall_at_reported_depth"] == 1
    assert row["candidate_stages"]["vector"]["invalid_or_unauthorized_ids"] == ["blocked"]
    assert row["candidate_stages"]["rerank"] is None
    stages = report["summary"]["heldout"]["candidate_stages"]
    assert stages["keyword"]["reported_cases"] == 1
    assert stages["rerank"]["reported_cases"] == 0
    assert stages["rerank"]["mean_recall_at_k"] is None
    assert "private-error" not in json.dumps(report)


@pytest.mark.parametrize("stage", [
    {"status": "disabled", "candidate_ids": []},
    {"status": "used", "candidate_ids": ["unknown-private-id"]},
    {"status": "degraded"},
])
def test_unavailable_or_rejected_candidate_trace_is_not_scored_as_empty(stage):
    fixture = _fixture()
    fixture["cases"] = [fixture["cases"][0]]
    def retrieve(*args, **kwargs):
        return SimpleNamespace(hits=[], payload=lambda: {"trace": {"vector": stage}})
    report = evaluation.evaluate(retrieve, fixture=fixture)
    assert report["cases"][0]["candidate_stages"]["vector"] is None
    assert "unknown-private-id" not in json.dumps(report)


def test_explicit_empty_candidate_list_is_measured_zero_not_missing():
    fixture = _fixture()
    def retrieve(*args, **kwargs):
        return SimpleNamespace(hits=[], payload=lambda: {"trace": {
            "vector": {"status": "empty", "candidate_ids": []}}})
    report = evaluation.evaluate(retrieve, fixture=fixture)
    summary = report["summary"]["heldout"]["candidate_stages"]["vector"]
    assert summary["reported_cases"] == 2
    assert summary["answerable_reported_cases"] == 1
    assert summary["mean_recall_at_k"] == 0
