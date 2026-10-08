from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.companion_core import (
    CompanionMemoryEvaluationError,
    EvaluationMetrics,
    RetrievalHit,
    decision,
    evaluate_fts5_baseline,
    evaluate_retriever,
    evaluate_vector_embeddings,
    hybrid_hits,
    load_evaluation_corpus,
)


ROOT = Path(__file__).resolve().parents[3]
GOLD = ROOT / "config" / "companion" / "evaluation" / "memory-recall-gold-v1.json"


def test_gold_set_is_versioned_fictional_and_filters_unpublished_or_withdrawn() -> None:
    corpus = load_evaluation_corpus(GOLD)
    assert corpus.version == "1.0.0"
    assert len(corpus.documents) == 20 and len(corpus.queries) == 20
    eligible = {item.document_id for item in corpus.eligible_documents}
    assert {"m02", "m06", "m09", "m10", "m19", "m20"}.isdisjoint(eligible)
    assert all(item.source_id.startswith("fiction:") for item in corpus.documents)


def test_loader_rejects_unknown_fields_duplicate_ids_and_bad_no_answer(tmp_path: Path) -> None:
    value = json.loads(GOLD.read_text(encoding="utf-8"))
    value["secret"] = "not allowed"
    target = tmp_path / "bad.json"
    target.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CompanionMemoryEvaluationError, match="schema"):
        load_evaluation_corpus(target)

    value.pop("secret")
    value["queries"][0]["no_answer"] = True
    target.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CompanionMemoryEvaluationError, match="no-answer"):
        load_evaluation_corpus(target)

    value["queries"][0]["no_answer"] = False
    value["documents"][1]["id"] = value["documents"][0]["id"]
    target.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CompanionMemoryEvaluationError, match="identity"):
        load_evaluation_corpus(target)


def test_evaluator_computes_recall_mrr_exclusions_no_answer_and_latency() -> None:
    corpus = load_evaluation_corpus(GOLD)
    answers = {
        "御主喝豆浆要加糖吗": ("m01",),
        "谁养了橘猫团子": ("m03",),
        "负责自动化的林晓养宠物吗": ("m04",),
        "陶艺课现在是星期几": ("m05",),
        "御主喜欢香菜吗": ("m07",),
        "北辰项目做什么": ("m08",),
        "我们怎样互相称呼": ("m11",),
        "御主现在什么时候运动": ("m12",),
        "北辰项目要交付什么格式": ("m13",),
        "周末什么时候整理每周任务": ("m14",),
        "复杂问题的回答顺序是什么": ("m15",),
        "北辰项目能使用个人账号凭据吗": ("m16",),
        "文档默认使用什么语言": ("m17",),
        "桌面提醒几点进入安静时段": ("m18",),
    }
    metrics = evaluate_retriever(
        corpus, backend="fixture",
        search=lambda query, _limit: tuple(RetrievalHit(item, 0.9) for item in answers.get(query, ())),
    )
    assert metrics.query_count == 20
    assert metrics.recall_at_1 == 1 and metrics.recall_at_4 == 1 and metrics.mrr == 1
    assert metrics.excluded_hit_rate == 0 and metrics.no_answer_injection_rate == 0
    assert metrics.latency_p50_ms >= 0 and metrics.latency_p95_ms >= metrics.latency_p50_ms
    assert metrics.mean_retrieved_items == pytest.approx(0.7)
    assert 0 < metrics.mean_retrieved_chars < 100
    assert metrics.p95_retrieved_chars < 100


def test_evaluator_fails_closed_if_retriever_returns_deleted_untrusted_or_duplicate_evidence() -> None:
    corpus = load_evaluation_corpus(GOLD)
    with pytest.raises(CompanionMemoryEvaluationError, match="ineligible"):
        evaluate_retriever(corpus, backend="bad", search=lambda _query, _limit: (RetrievalHit("m09", 1.0),))
    with pytest.raises(CompanionMemoryEvaluationError, match="hit count"):
        evaluate_retriever(corpus, backend="bad", search=lambda _query, _limit: (RetrievalHit("m01", 1.0), RetrievalHit("m01", 0.9)))


def test_hybrid_uses_threshold_deduplicates_and_is_stable() -> None:
    result = hybrid_hits(
        [RetrievalHit("m01", 3.0), RetrievalHit("m03", 2.0)],
        [RetrievalHit("m03", 0.91), RetrievalHit("m04", 0.49), RetrievalHit("m01", 0.80)],
        vector_threshold=0.5,
    )
    assert [item.document_id for item in result] == ["m03", "m01"]
    assert len({item.document_id for item in result}) == 2


def _metrics(**overrides) -> EvaluationMetrics:
    values = dict(
        backend="fixture", query_count=20, recall_at_1=0.7, recall_at_4=0.7, mrr=0.75,
        excluded_hit_rate=0, no_answer_injection_rate=0, latency_p50_ms=10, latency_p95_ms=20,
        mean_retrieved_items=1, mean_retrieved_chars=200, p95_retrieved_chars=500,
    )
    values.update(overrides)
    return EvaluationMetrics(**values)


def test_activation_decision_requires_every_quality_privacy_and_budget_gate() -> None:
    fts = _metrics(backend="fts", recall_at_4=0.70, mrr=0.70)
    hybrid = _metrics(backend="hybrid", recall_at_4=0.80, mrr=0.75)
    assert decision(fts, hybrid, cold_start_ms=1_000, rebuild_ms=2_000, disk_bytes=10_000) == ("ready", ())

    status, reasons = decision(
        fts, _metrics(backend="hybrid", recall_at_4=0.79, mrr=0.69, excluded_hit_rate=0.01, no_answer_injection_rate=0.25, latency_p95_ms=300),
        cold_start_ms=6_000, rebuild_ms=31_000, disk_bytes=300 * 1024 * 1024,
    )
    assert status == "deferred"
    assert set(reasons) == {
        "recall_gain_below_10pp", "mrr_regressed", "excluded_canary_recalled",
        "no_answer_injection_above_5pct", "warm_p95_above_250ms", "cold_start_above_5s",
        "rebuild_above_30s", "disk_above_250mib",
    }


def test_real_fts5_baseline_uses_only_eligible_documents(tmp_path: Path) -> None:
    corpus = load_evaluation_corpus(GOLD)

    metrics, disk_bytes, rebuild_ms = evaluate_fts5_baseline(corpus, work_root=tmp_path)

    assert metrics.backend == "sqlite_fts5"
    assert metrics.query_count == 20
    assert metrics.excluded_hit_rate == pytest.approx(2 / 20)
    assert 0 <= metrics.recall_at_4 <= 1
    assert disk_bytes > 0
    assert rebuild_ms >= 0


def test_vector_evaluation_is_deterministic_and_never_indexes_excluded_documents() -> None:
    corpus = load_evaluation_corpus(GOLD)
    positions = {
        document.document_id: index
        for index, document in enumerate(corpus.eligible_documents)
    }

    def embed(texts):
        vectors = []
        for text in texts:
            vector = [0.0] * len(positions)
            matching = next(
                (item for item in corpus.eligible_documents if item.content == text),
                None,
            )
            vector[positions[matching.document_id] if matching is not None else 0] = 1.0
            vectors.append(vector)
        return vectors

    vector, hybrid, disk_bytes, rebuild_ms, fts_p95 = evaluate_vector_embeddings(
        corpus,
        embed=embed,
    )

    assert vector.backend == "vector"
    assert hybrid.backend == "hybrid_rrf"
    assert vector.excluded_hit_rate == 0
    assert hybrid.excluded_hit_rate == pytest.approx(2 / 20)
    assert disk_bytes == len(corpus.eligible_documents) * len(positions) * 4
    assert rebuild_ms >= 0
    assert fts_p95 >= 0


def test_runner_defers_without_complete_offline_model_and_emits_no_paths(tmp_path: Path) -> None:
    incomplete_model = tmp_path / "incomplete-model"
    incomplete_model.mkdir()
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "work/scripts/run_companion_memory_vector_evaluation.py"),
            "--model-dir",
            str(incomplete_model),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["activation"] == {
        "status": "deferred",
        "reasons": ["offline_model_unavailable"],
    }
    assert report["production_backend_changed"] is False
    assert str(incomplete_model) not in completed.stdout
    assert str(incomplete_model) not in output.read_text(encoding="utf-8")
