from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "work/scripts/compare_recognition.py"
_SPEC = importlib.util.spec_from_file_location("compare_recognition", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
comparison = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(comparison)


def _fixture() -> dict[str, object]:
    value = {"version": "approved-v1", "data_class": "approved_deidentified_holdout", "entries": [
        {"id": "expected", "revision": 1, "current_revision": 1, "project_id": "project-a", "content": "网页优先", "status": "active", "authorized": True, "source_refs": []},
        {"id": "other", "revision": 1, "current_revision": 1, "project_id": "project-a", "content": "桌面后置", "status": "active", "authorized": True, "source_refs": []},
    ], "cases": [{"case_id": "one", "dataset_split": "heldout", "project_id": "project-a", "query": "网页优先", "expected_ids": ["expected"], "forbidden_ids": []}]}
    for entry in value["entries"]:
        entry["evaluation_egress_purposes"] = ["embedding", "rerank"]
    return value


class _Models:
    def __init__(self, *, embedding: bool = True, rerank: bool = True) -> None:
        self.changed = False
        self.calls = 0
        self._enabled = {"embedding": embedding, "rerank": rerank}

    def public(self):
        self.calls += 1
        return {purpose: {"purpose": purpose, "provider": "openai", "base_url": "https://example.test/v1", "model": purpose + "-model", "allow_remote": True, "enabled": enabled, "configured": enabled, "has_api_key": enabled, "revision": 4 if not self.changed else 5} for purpose, enabled in self._enabled.items()}


class _Embedding:
    config_revision = "4"
    model = "embedding-model"
    def embed(self, texts):
        return tuple((1.0, 0.0) if "网页" in text else (0.0, 1.0) for text in texts)


class _Reranker:
    def rerank(self, *, query, candidates):
        return {candidate["id"]: 1.0 if candidate["id"] == "expected" else 0.0 for candidate in candidates}


def _factory(models, purpose, *, validate_current):
    adapter = _Embedding() if purpose == "embedding" else _Reranker()
    adapter.validate_current = validate_current
    return adapter


def _clock():
    _clock.value += 0.001
    return _clock.value
_clock.value = 0.0


def test_comparison_freezes_enabled_identity_reports_modes_latency_and_does_not_mutate_fixture() -> None:
    fixture = _fixture()
    original = copy.deepcopy(fixture)
    result = comparison.compare(fixture=fixture, models=_Models(), adapter_factory=_factory, clock=_clock)

    assert fixture == original
    assert [row["mode"] for row in result["modes"]] == ["keyword", "vector", "keyword_vector", "keyword_vector_rerank"]
    assert all(row["uncached"] is True and row["latency_ms"]["p95"] is not None for row in result["modes"])
    assert result["modes"][1]["stage_status"][0]["stages"]["vector"] == "used"
    assert result["modes"][1]["stage_status"][0]["stages"]["keyword"] == "disabled"
    assert result["modes"][3]["stage_status"][0]["stages"]["rerank"] == "used"


def test_comparison_observes_http_usage_per_mode_and_case_without_retaining_payloads():
    from backend.recognition_retrieval import HttpEmbeddingProvider, HttpReranker

    class Client:
        def post_json(self, *, endpoint, payload):
            if "input" in payload:
                return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))],
                        "usage": {"prompt_tokens": 7, "total_tokens": 7, "secret": "private-response"}}
            return {"results": [{"index": i, "relevance_score": 1.0} for i in range(len(payload["documents"]))],
                    "usage": {"input_tokens": 11, "total_tokens": 11}}

    def factory(models, purpose, **kwargs):
        return (HttpEmbeddingProvider(Client(), "https://private-route.test", "model") if purpose == "embedding"
                else HttpReranker(Client(), "https://private-route.test", "model"))

    fixture = _fixture()
    fixture["cases"].append({**fixture["cases"][0], "case_id": "two"})
    result = comparison.compare(fixture=fixture, models=_Models(), adapter_factory=factory)
    assert result["modes"][0]["provider_usage"]["purposes"] == {}
    for row in result["modes"][1:]:
        usage = row["provider_usage"]
        embedding = usage["purposes"]["embedding"]
        assert embedding["transport_calls"] == 2  # documents and query share one batch per case
        assert embedding["usage"]["total_tokens"]["total"] == 14
        assert [case["purposes"]["embedding"]["usage"]["total_tokens"]["total"] for case in usage["per_case"]] == [7, 7]
        assert embedding["cost"] is None
    rerank = result["modes"][-1]["provider_usage"]["purposes"]["rerank"]
    assert rerank["transport_calls"] == 2
    assert rerank["usage"]["input_tokens"]["total"] == 22
    encoded = json.dumps(result)
    assert "private-response" not in encoded and "private-route" not in encoded


def test_usage_observer_preserves_partial_unknown_usage_and_failed_calls():
    class Client:
        calls = 0
        def post_json(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"usage": {"total_tokens": 9, "prompt_tokens": True, "output_tokens": -1,
                                  "input_tokens": "10", "completion_tokens": 1.5}}
            if self.calls == 2:
                return {"usage": {"total_tokens": 0}}
            if self.calls == 3:
                return {"usage": "private-malformed-response"}
            raise RuntimeError("private-error")

    observer = comparison._ObservedTransport(Client())
    for _ in range(3):
        observer.post_json(endpoint="unused", payload={"private": "body"})
    with pytest.raises(RuntimeError):
        observer.post_json(endpoint="unused", payload={})
    report = comparison._usage_report(observer.events)
    assert report["transport_calls"] == 4 and report["responses_received"] == 3
    assert report["usage"]["total_tokens"] == {"reported_calls": 2, "observed_sum": 9, "total": None}
    assert report["usage"]["prompt_tokens"]["total"] is None
    assert "private" not in json.dumps(observer.events)


def test_custom_adapters_report_usage_unavailable_instead_of_zero():
    result = comparison.compare(fixture=_fixture(), models=_Models(), adapter_factory=_factory)
    usage = result["modes"][1]["provider_usage"]["purposes"]["embedding"]
    assert usage["status"] == "not_instrumented"
    assert "transport_calls" not in usage


def test_usage_is_retained_when_response_cannot_be_used_and_no_calls_when_sources_denied():
    from backend.recognition_retrieval import HttpEmbeddingProvider

    class Client:
        def post_json(self, **kwargs):
            return {"data": [], "usage": {"total_tokens": 13}}

    def factory(*args, **kwargs):
        return HttpEmbeddingProvider(Client(), "https://example.test", "fixture")

    result = comparison.compare(fixture=_fixture(), models=_Models(rerank=False), adapter_factory=factory)
    row = result["modes"][1]
    assert row["stage_status"][0]["stages"]["vector"] == "degraded"
    assert row["provider_usage"]["purposes"]["embedding"]["usage"]["total_tokens"]["total"] == 13
    fixture = _fixture()
    for entry in fixture["entries"]:
        entry["evaluation_egress_purposes"] = []
    result = comparison.compare(fixture=fixture, models=_Models(rerank=False), adapter_factory=factory)
    usage = result["modes"][1]["provider_usage"]["purposes"]["embedding"]
    assert usage["status"] == "no_requests" and usage["transport_calls"] == 0
    assert usage["usage"]["total_tokens"]["total"] is None


@pytest.mark.parametrize(("embedding", "rerank", "modes"), [(True, False, ["keyword", "vector", "keyword_vector"]), (False, True, ["keyword", "keyword_rerank"])])
def test_comparison_only_runs_modes_supported_by_frozen_configuration(embedding, rerank, modes) -> None:
    result = comparison.compare(fixture=_fixture(), models=_Models(embedding=embedding, rerank=rerank), adapter_factory=_factory, clock=_clock)
    assert [row["mode"] for row in result["modes"]] == modes


def test_unlabelled_evaluation_sources_never_reach_providers_but_remain_lexical():
    fixture = _fixture()
    for entry in fixture["entries"]:
        del entry["evaluation_egress_purposes"]

    class ForbiddenAdapter:
        config_revision = "4"
        model = "test"
        def embed(self, texts):
            pytest.fail("unlabelled documents reached embedding")
        def rerank(self, **kwargs):
            pytest.fail("unlabelled documents reached rerank")

    result = comparison.compare(fixture=fixture, models=_Models(),
        adapter_factory=lambda *args, **kwargs: ForbiddenAdapter())
    assert result["source_egress_policy"] == "explicit_dataset_purposes_default_deny"
    assert result["modes"][0]["evaluation"]["cases"][0]["actual_ids"] == ["expected"]
    vector_trace = result["modes"][1]["evaluation"]["cases"][0]["trace"]["vector"]
    assert vector_trace["status"] == "policy_restricted"
    assert vector_trace["excluded_by_policy"] == 2


def test_embedding_and_rerank_evaluation_permissions_are_independent():
    fixture = _fixture()
    fixture["cases"][0]["query"] = "网页优先 桌面后置"
    fixture["entries"][0]["evaluation_egress_purposes"] = ["embedding"]
    fixture["entries"][1]["evaluation_egress_purposes"] = ["rerank"]
    embedded, reranked = [], []

    class Embedding(_Embedding):
        def embed(self, texts):
            embedded.extend(texts)
            return super().embed(texts)
    class Reranker(_Reranker):
        def rerank(self, *, query, candidates):
            reranked.extend(candidate["id"] for candidate in candidates)
            return super().rerank(query=query, candidates=candidates)

    comparison.compare(fixture=fixture, models=_Models(),
        adapter_factory=lambda models, purpose, **kwargs: Embedding() if purpose == "embedding" else Reranker())
    assert "网页优先" in embedded
    assert "桌面后置" not in embedded
    assert reranked == ["other"]


@pytest.mark.parametrize("purposes", [None, "embedding", ["generation"], ["embedding", "embedding"], [{}]])
def test_invalid_evaluation_authorization_rejected_before_model_access(purposes):
    fixture = _fixture()
    fixture["entries"][0]["evaluation_egress_purposes"] = purposes
    models = _Models()
    with pytest.raises(comparison.ComparisonError, match="evaluation_egress_purposes_invalid"):
        comparison.compare(fixture=fixture, models=models, adapter_factory=_factory)
    assert models.calls == 0


def test_missing_or_not_permitted_configuration_fails_closed() -> None:
    with pytest.raises(comparison.ComparisonError, match="not_enabled"):
        comparison.compare(fixture=_fixture(), models=_Models(embedding=False, rerank=False), adapter_factory=_factory)
    models = _Models()
    public = models.public
    def not_permitted():
        value = public()
        value["embedding"]["allow_remote"] = False
        return value
    models.public = not_permitted
    with pytest.raises(comparison.ComparisonError, match="not_permitted"):
        comparison.compare(fixture=_fixture(), models=models, adapter_factory=_factory)


def test_current_guard_fails_instead_of_reporting_a_changed_configuration() -> None:
    models = _Models()
    def changing_factory(models, purpose, *, validate_current):
        models.changed = True
        return _factory(models, purpose, validate_current=validate_current)
    with pytest.raises(comparison.ComparisonError, match="changed"):
        comparison.compare(fixture=_fixture(), models=models, adapter_factory=changing_factory)


def test_runtime_read_only_check_never_creates_missing_database_or_schema(tmp_path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(comparison.ComparisonError, match="database_missing"):
        comparison._runtime_models(missing)
    assert not missing.exists()
    root = tmp_path / "existing"
    root.mkdir()
    database = root / "recognition.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE untouched (value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(comparison.ComparisonError, match="database_invalid"):
        comparison._runtime_models(root)
    check = sqlite3.connect(database)
    assert check.execute("SELECT name FROM sqlite_master WHERE name = 'crp_structured_records'").fetchone() is None
    assert check.execute("SELECT name FROM sqlite_master WHERE name = 'untouched'").fetchone() == ("untouched",)
    check.close()


@pytest.mark.parametrize("invalid", ["consent", "output", "dataset", "top_k"])
def test_cli_rejects_invalid_inputs_before_opening_runtime(tmp_path, monkeypatch, capsys, invalid):
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps(_fixture() if invalid != "dataset" else {"private": "do-not-print"}), encoding="utf-8")
    argv = ["compare", "--runtime-root", str(tmp_path / "runtime"), "--dataset", str(dataset)]
    if invalid != "consent":
        argv.append("--allow-remote-evaluation")
    if invalid != "output":
        argv += ["--output", str(tmp_path / "report.json")]
    if invalid == "top_k":
        argv += ["--top-k", "0"]
    opened = []
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(comparison, "_runtime_models", lambda root: opened.append(root))
    with pytest.raises(SystemExit) as error:
        comparison.main()
    assert error.value.code == 2
    assert opened == []
    assert "do-not-print" not in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_failed_vector_stage_is_reported_as_degraded_without_private_error():
    class FailedEmbedding:
        model = "test"
        config_revision = "4"

        def embed(self, texts):
            raise RuntimeError("private-provider-error https://secret.test secret-key")

    report = comparison.compare(fixture=_fixture(), models=_Models(rerank=False),
                                adapter_factory=lambda *a, **k: FailedEmbedding())
    assert report["modes"][1]["stage_status"][0]["stages"]["vector"] == "degraded"
    serialized = json.dumps(report)
    assert "private-provider-error" not in serialized
    assert "secret-key" not in serialized
    assert "secret.test" not in serialized


def test_vector_only_skips_keyword_projection_and_keeps_scope_filters(monkeypatch):
    from backend.recognition_retrieval import service
    fixture = _fixture()
    for item_id, changes in [
        ("cross-project", {"project_id": "project-b"}),
        ("unauthorized", {"authorized": False}),
        ("revoked", {"status": "revoked"}),
        ("old-revision", {"current_revision": 2}),
    ]:
        fixture["entries"].append(dict(fixture["entries"][0], id=item_id, **changes))
    sent = []
    class Embedding:
        model = "test"
        config_revision = "1"
        def embed(self, texts):
            sent.extend(texts)
            return tuple((1.0, 0.0) if text == "桌面后置" else (0.0, 1.0) for text in texts)
    def forbidden_fts(*args):
        raise AssertionError("pure-vector must not execute keyword projection")
    monkeypatch.setattr(service, "_fts_keyword_candidate_ids", forbidden_fts)
    result = service.retrieve("project-a", "桌面后置", fixture["entries"],
                              keyword_enabled=False, embedding_provider=Embedding(), limit=1)
    assert [hit.id for hit in result.hits] == ["other"]
    assert result.hits[0].keyword_score == 0.0
    assert result.trace["keyword"]["status"] == "disabled"
    assert result.trace["excluded"] == {"other_project": 1, "unauthorized": 1, "stale": 2, "invalid": 0}
    assert len(sent) == 3


def test_vector_only_failure_does_not_silently_return_keyword_results():
    from backend.recognition_retrieval.service import retrieve, RecognitionRetrievalError
    class Broken:
        def embed(self, texts):
            raise RuntimeError("private provider failure")
    result = retrieve("project-a", "网页优先", _fixture()["entries"],
                      keyword_enabled=False, embedding_provider=Broken())
    assert result.hits == ()
    assert result.trace["vector"]["status"] == "degraded"
    assert result.trace["keyword"]["status"] == "disabled"
    with pytest.raises(RecognitionRetrievalError, match="requires an embedding"):
        retrieve("project-a", "网页优先", _fixture()["entries"], keyword_enabled=False)
