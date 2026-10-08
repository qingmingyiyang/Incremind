from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.memory_app.workspace_generation as workspace_module
from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.workspace_generation import (
    _complete_chunked_video_draft as _kernel_chunked_video_draft, _draft, _ground_local_draft, _video_source_chunks,
)
from backend.memory_app.workspace_audio import _cloud_audio_derivative, _split_workspace_wav_chunk
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.tokenhub_asr_provider import tokenhub_egress_manifest
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.secrets import InMemorySecretStore
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.processing_lease import ProcessingLease
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF, SaveCloudAsrProviderSettings
from core.product_core.local_asr_provider_settings import SaveLocalAsrProviderSettings
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


from tests.memory_app.governed_model_fixture import GovernedModel


def _complete_chunked_video_draft(model, source, validate_current, local_model):
    """Isolate chunk parsing; runtime/receipt behavior has real-kernel tests."""
    class ParserOrganizer:
        def complete(self, messages, *, max_tokens, validate_current,
                     timeout_seconds, stage, validate_output):
            return model.complete(messages, max_tokens=max_tokens,
                validate_current=validate_current, timeout_seconds=timeout_seconds)
    return _kernel_chunked_video_draft(source, validate_current, local_model,
                                     organize=ParserOrganizer())


class Model(GovernedModel):
    def __init__(self, failure=False):
        self.failure = failure

    def public(self):
        public = super().public()
        public["generation"].update(base_url="", allow_remote=False)
        return public

    def complete(self, messages, *, max_tokens, validate_current=None, retry_policy=None):
        if self.failure:
            raise RuntimeError("secret provider details")
        if validate_current:
            validate_current()
        if "只根据用户提供的资料回答" in messages[0]["content"]:
            return json.dumps({"answer": "资料记载了原文证据。", "citations": [1]}, ensure_ascii=False), {"usage": {"total_tokens": 10}}
        source = messages[-1]["content"]
        return json.dumps({
            "title": "整理后的标题", "summary": "已整理。", "topics": ["主题"],
            "facts": [{"text": "事实", "evidence": {"start": 0, "end": 2, "quote": source[:2]}}],
            "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": [],
        }, ensure_ascii=False), {}


def _completed_audio_output(store, *, output_id, audio_asset_id, text, provider):
    asset = store.read("audio_asset_refs", audio_asset_id)
    assert asset is not None
    return {
        "id": output_id,
        "ref": f"crp://recognition/media-processing-outputs/{output_id}.json",
        "source_id": asset["source_id"],
        "source_type": "audio",
        "output_kind": "transcript",
        "status": "completed",
        "provider": provider,
        "text": text,
        "metadata": {"audio_asset_id": audio_asset_id},
    }


def test_long_video_uses_inherited_chunks_and_only_source_checked_quotes():
    source = ("同句。" + "甲" * 100 + "。") * 70
    chunks = _video_source_chunks(source)
    assert len(chunks) >= 2
    assert all(len(text) <= 3500 for text, _ in chunks)
    calls = []
    checks = []

    class ChunkModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            if validate_current:
                validate_current()
            calls.append((messages, timeout_seconds))
            if "当前仅处理全文第" in messages[0]["content"]:
                text = messages[-1]["content"]
                return json.dumps({
                    "summary": "片段概括", "topics": [],
                    "facts": [{"text": "原文事实", "evidence": {
                        "start": 0, "end": 2, "quote": text[:2],
                    }}],
                    "todos": [], "uncertainties": [],
                }, ensure_ascii=False), {"model": "test-model", "usage": {"total_tokens": 4}}
            return json.dumps({
                "title": "整体标题", "summary": "整体摘要", "topics": ["主题"],
                "uncertainties": [], "people": [], "dates": [], "suggestions": [],
                "fact_ids": [f"facts-{i}-1" for i in range(1, len(chunks) + 1)],
                "todo_ids": [],
            }, ensure_ascii=False), {"model": "test-model", "usage": {"total_tokens": 4}}

    draft, meta = _complete_chunked_video_draft(
        ChunkModel(), source, lambda: checks.append(True), False,
    )
    assert len(calls) == len(chunks) + 1
    assert [timeout for _messages, timeout in calls] == [90] * len(chunks) + [120]
    assert len(checks) >= 2 * len(calls)
    assert meta["usage"]["total_tokens"] == 4 * len(calls)
    assert len(draft["facts"]) == len(chunks)
    assert len({entry["evidence"]["start"] for entry in draft["facts"]}) == len(chunks)
    assert all(source[e["start"]:e["end"]] == e["quote"] for entry in draft["facts"]
               for e in [entry["evidence"]])


def test_long_unpunctuated_video_line_is_bounded():
    chunks = _video_source_chunks("甲" * 8000)
    assert len(chunks) == 3
    assert all(len(text) <= 3500 for text, _ in chunks)


def test_video_chunks_do_not_expand_long_whitespace_between_sentences():
    source = "甲。" + " " * 7000 + "乙。"
    chunks = _video_source_chunks(source)
    assert len(chunks) == 2
    assert all(len(text) <= 3500 for text, _ in chunks)
    assert [source[offset:offset + len(text)] for text, offset in chunks] == [text for text, _ in chunks]


def test_chunk_merge_rejects_invented_evidence_id():
    source = "甲" * 8000

    class InventingModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            if "当前仅处理全文第" in messages[0]["content"]:
                return json.dumps({
                    "summary": "概括", "topics": [],
                    "facts": [{"text": "事实", "evidence": {"start": 0, "end": 2, "quote": "甲甲"}}],
                    "todos": [], "uncertainties": [],
                }, ensure_ascii=False), {}
            return json.dumps({
                "title": "整体", "summary": "概括", "topics": [], "uncertainties": [],
                "people": [], "dates": [], "suggestions": [],
                "fact_ids": ["facts-999-1"], "todo_ids": [],
            }, ensure_ascii=False), {}

    with pytest.raises(ValueError, match="model_response_invalid"):
        _complete_chunked_video_draft(InventingModel(), source, lambda: None, False)


def test_long_video_corrects_repeated_quote_without_guessing_its_position():
    source = "".join(f"唯一标记{index:03d}。重复短句。" + "正文" * 55 + "。" for index in range(80))
    calls = []

    class QuoteModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            if validate_current:
                validate_current()
            calls.append((messages, timeout_seconds))
            if "当前仅处理全文第" in messages[0]["content"]:
                chunk_text = messages[1]["content"]
                quote = "重复短句" if len(messages) == 2 and len(calls) == 1 else re.search(r"唯一标记\d{3}", chunk_text).group()
                return json.dumps({
                    "summary": "片段摘要", "topics": [],
                    "facts": [{"text": "原文标记", "evidence": {"quote": quote}}],
                    "todos": [], "uncertainties": [],
                }, ensure_ascii=False), {"model": "fixture", "usage": {"total_tokens": 3}}
            return json.dumps({
                "title": "整体标题", "summary": "整体摘要", "topics": [],
                "uncertainties": [], "people": [], "dates": [], "suggestions": [],
                "fact_ids": ["facts-1-1"], "todo_ids": [],
            }, ensure_ascii=False), {"model": "fixture", "usage": {"total_tokens": 3}}

    draft, meta = _complete_chunked_video_draft(QuoteModel(), source, lambda: None, False)
    assert len(calls) == len(_video_source_chunks(source)) + 2
    assert len(calls[1][0]) == 3  # correction is bounded to one extra request
    evidence = draft["facts"][0]["evidence"]
    assert evidence["quote"].startswith("唯一标记")
    assert source[evidence["start"]:evidence["end"]] == evidence["quote"]
    assert meta["usage"]["total_tokens"] == 3 * len(calls)
    assert all(1 <= timeout <= 120 for _messages, timeout in calls)


@pytest.mark.parametrize("first_response", ["not json", '{"summary":"缺少字段"}'])
def test_long_video_corrects_invalid_chunk_format_once(first_response):
    source = "唯一证据。" + "甲" * 7500
    calls = 0

    class FormatModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            nonlocal calls
            calls += 1
            if validate_current:
                validate_current()
            if "当前仅处理全文第" in messages[0]["content"]:
                if calls == 1:
                    return first_response, {"usage": {"total_tokens": 7}}
                return json.dumps({"summary": "片段", "topics": [], "facts": [],
                                   "todos": [], "uncertainties": []}, ensure_ascii=False), {"usage": {"total_tokens": 5}}
            return json.dumps({"title": "整体", "summary": "概括", "topics": [],
                               "uncertainties": [], "people": [], "dates": [], "suggestions": [],
                               "fact_ids": [], "todo_ids": []}, ensure_ascii=False), {"usage": {"total_tokens": 5}}

    draft, meta = _complete_chunked_video_draft(FormatModel(), source, lambda: None, False)
    assert draft["title"] == "整体"
    assert calls == len(_video_source_chunks(source)) + 2
    assert meta["usage"]["total_tokens"] == 7 + 5 * (calls - 1)


def test_long_video_invalid_merge_stays_failed_without_leaking_response(caplog):
    source = "唯一证据。" + "甲" * 7500
    calls = 0
    invalid_response = 'PRIVATE_PROVIDER_RESPONSE:{"fact_ids":["facts-999-1"]}'

    class BadMergeModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            nonlocal calls
            calls += 1
            if validate_current:
                validate_current()
            if "当前仅处理全文第" in messages[0]["content"]:
                return json.dumps({"summary": "片段", "topics": [], "facts": [],
                                   "todos": [], "uncertainties": []}, ensure_ascii=False), {}
            return invalid_response, {}

    with pytest.raises(ValueError, match="model_response_invalid"):
        _complete_chunked_video_draft(BadMergeModel(), source, lambda: None, False)
    assert calls == len(_video_source_chunks(source)) + 2
    assert invalid_response not in caplog.text
    assert "stage=merge" in caplog.text


@pytest.mark.parametrize("bad_id", ["facts-999-1", "todos-1-1"])
def test_long_video_corrects_unknown_or_wrong_type_merge_candidate(bad_id):
    source = "唯一证据。" + "甲" * 7500
    merge_calls = 0

    class MergeModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            nonlocal merge_calls
            if validate_current:
                validate_current()
            if "当前仅处理全文第" in messages[0]["content"]:
                return json.dumps({"summary": "片段", "topics": [],
                                   "facts": [{"text": "证据", "evidence": {"quote": "唯一证据"}}]
                                   if "唯一证据" in messages[1]["content"] else [],
                                   "todos": [], "uncertainties": []}, ensure_ascii=False), {}
            merge_calls += 1
            return json.dumps({"title": "整体", "summary": "概括", "topics": [],
                               "uncertainties": [], "people": [], "dates": [], "suggestions": [],
                               "fact_ids": [bad_id if merge_calls == 1 else "facts-1-1"],
                               "todo_ids": []}, ensure_ascii=False), {}

    draft, _ = _complete_chunked_video_draft(MergeModel(), source, lambda: None, False)
    assert merge_calls == 2
    assert draft["facts"][0]["evidence"]["quote"] == "唯一证据"


def test_long_video_persistent_ambiguous_quote_fails_after_one_correction():
    source = "重复短句。" * 900
    calls = 0

    class AmbiguousModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            nonlocal calls
            calls += 1
            if validate_current:
                validate_current()
            return json.dumps({"summary": "片段", "topics": [],
                               "facts": [{"text": "重复内容", "evidence": {"quote": "重复短句"}}],
                               "todos": [], "uncertainties": []}, ensure_ascii=False), {}

    with pytest.raises(ValueError, match="model_response_invalid"):
        _complete_chunked_video_draft(AmbiguousModel(), source, lambda: None, False)
    assert calls == 2


@pytest.mark.parametrize(("elapsed", "timeouts"), [(40, [90, 50]), (91, [90])])
def test_long_video_correction_shares_stage_timeout(monkeypatch, elapsed, timeouts):
    clock = [0.0]
    observed = []
    monkeypatch.setattr(workspace_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class TimedModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            observed.append(timeout_seconds)
            if len(observed) == 1:
                clock[0] += elapsed
            return "invalid-json", {}

    with pytest.raises(ValueError, match="model_response_invalid"):
        _complete_chunked_video_draft(TimedModel(), "甲" * 7500, lambda: None, False)
    assert observed == timeouts


def test_long_video_authority_revocation_prevents_correction_wire():
    source = "唯一证据。" + "甲" * 7500
    allowed = True
    calls = 0

    def validate_current():
        if not allowed:
            raise ValueError("remote_processing_target_changed")

    class RevokedModel:
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            nonlocal allowed, calls
            calls += 1
            if validate_current:
                validate_current()
            allowed = False
            return "invalid-json", {}

    with pytest.raises(ValueError, match="remote_processing_target_changed"):
        _complete_chunked_video_draft(RevokedModel(), source, validate_current, False)
    assert calls == 1


def test_failed_long_video_keeps_transcript_and_exposes_format_error(tmp_path):
    class InvalidVideoModel(Model):
        def complete(self, messages, *, max_tokens, validate_current=None, timeout_seconds=None):
            if validate_current:
                validate_current()
            return "invalid-json", {}

    http, records = client(tmp_path, InvalidVideoModel())
    item = http.post("/api/workspace/v1/items/link", json={
        "project_id": "alpha", "url": "https://www.bilibili.com/video/BV18hhq6vEDs/",
    }).json()
    source = "原始转写证据。" + "内容" * 3500
    row = records.read("workspace_items", item["id"])
    with records.begin() as tx:
        tx.put("workspace_items", item["id"], {**row.payload, "source_text": source},
               expected_revision=row.revision)
        tx.commit()

    result = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"})
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert result.json()["error"] == "model_response_invalid"
    saved = records.read("workspace_items", item["id"]).payload
    assert saved["source_text"] == source
    assert saved["draft"] is None
    assert http.post(f"/api/workspace/v1/items/{item['id']}/retry", json={"project_id": "alpha"}).json()["status"] == "staged"


def client(tmp_path: Path, model=None):
    records = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    app = FastAPI()
    install_workspace_routes(app, runtime_root=tmp_path, records=records, models=model or Model(),
                             documents=SQLiteDocumentRepository(records, namespace_id="recognition"),
                             service=RecognitionService(records))
    return TestClient(app), records


def test_text_to_confirmed_document_and_candidate(tmp_path):
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/text", json={"project_id": "alpha", "text": "原文证据更多内容"}).json()
    item_id = item["id"]
    assert item["status"] == "staged"
    assert http.get("/api/workspace/v1/items", params={"project_id": "beta"}).json() == {"items": []}
    assert http.get(f"/api/workspace/v1/items/{item_id}/source", params={"project_id": "beta"}).status_code == 404
    processed = http.post(f"/api/workspace/v1/items/{item_id}/process", json={"project_id": "alpha"}).json()
    assert processed["status"] == "ready"
    assert processed["draft"]["facts"][0]["evidence"] == {"start": 0, "end": 2, "quote": "原文"}
    saved = http.put(f"/api/workspace/v1/items/{item_id}/draft", json={
        "project_id": "alpha", "expected_revision": processed["revision"], **processed["draft"],
    })
    assert saved.status_code == 200
    confirmed = http.post(f"/api/workspace/v1/items/{item_id}/confirm", json={
        "project_id": "alpha", "expected_revision": saved.json()["revision"],
    }).json()
    assert confirmed["status"] == "confirmed"
    markdown = SQLiteDocumentRepository(records, namespace_id="recognition").markdown(confirmed["document_id"])
    assert markdown.startswith("# 整理后的标题\n\n## 摘要\n\n已整理。\n")
    assert records.read("documents", confirmed["document_id"]) is not None
    assert len(http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "原文"}).json()["items"]) == 1
    answer = http.post("/api/workspace/v1/ask", json={"project_id": "alpha", "question": "原文证据是什么？"}).json()
    assert answer["model_used"] is True
    assert answer["sources"][0]["id"] == item_id
    recognition = http.post(f"/api/workspace/v1/items/{item_id}/recognition", json={"project_id": "alpha"}).json()
    experience = records.read("recognition_experiences", recognition["experience_id"])
    assert experience.payload["provenance"]["source_refs"][0]["id"] == confirmed["document_id"]
    assert experience.payload["provenance"]["kind"] == "workspace_confirmed_document"
    assert records.read("recognition_candidates", recognition["candidate_id"]).payload["state"] == "pending"
    scope = WorkScope("local-user", "alpha")
    egress = SourceEgressService(records)
    initial = egress.snapshot(scope, [{"type": "experience", "id": recognition["experience_id"], "revision": 1}])
    assert initial["nodes"][0]["effective_purposes"] == ["embedding", "generation", "rerank"]
    egress.set_policy(scope, "experience", recognition["experience_id"], 1, 0, [])
    private = egress.snapshot(scope, [{"type": "experience", "id": recognition["experience_id"], "revision": 1}])
    assert private["nodes"][0]["effective_purposes"] == []
    egress.set_policy(scope, "experience", recognition["experience_id"], 1, 1, ["generation", "embedding", "rerank"])
    published = RecognitionService(records).publish(scope=scope, candidate_id=recognition["candidate_id"],
                                                     expected_revision=1, reviewer="local-user")
    authorized = egress.snapshot(scope, [{"type": "recognition", "id": published.id, "revision": published.revision}])
    assert "generation" in authorized["nodes"][-1]["effective_purposes"]
    recognition_question = published.content[:500]
    recognition_preview = http.post("/api/workspace/v1/ask/preview", json={
        "project_id": "alpha", "question": recognition_question,
    })
    assert recognition_preview.status_code == 200, recognition_preview.text
    assert any(source["type"] == "recognition" for source in recognition_preview.json()["sources"])
    ask_with_recognition = http.post("/api/workspace/v1/ask", json={
        "project_id": "alpha", "question": recognition_question,
    })
    assert ask_with_recognition.status_code == 200, ask_with_recognition.text


def test_draft_revision_conflict_preserves_first_save_and_blocks_stale_confirmation(tmp_path):
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/text", json={
        "project_id": "alpha", "text": "原文证据更多内容",
    }).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    ready = http.post(path + "/process", json={"project_id": "alpha"}).json()
    first_revision = ready["revision"]
    assert http.put(path + "/draft", json={
        "project_id": "beta", "expected_revision": first_revision, **ready["draft"],
    }).status_code == 404
    assert http.post(path + "/confirm", json={
        "project_id": "beta", "expected_revision": first_revision,
    }).status_code == 404
    first_draft = {**ready["draft"], "summary": "窗口 A 保存的摘要"}
    first = http.put(path + "/draft", json={
        "project_id": "alpha", "expected_revision": first_revision, **first_draft,
    })
    assert first.status_code == 200
    saved = first.json()
    assert saved["revision"] == first_revision + 1
    second_draft = {**ready["draft"], "summary": "窗口 B 的旧草稿"}
    second = http.put(path + "/draft", json={
        "project_id": "alpha", "expected_revision": first_revision, **second_draft,
    })
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "draft_revision_conflict"
    assert second.json()["detail"]["current"]["draft"]["summary"] == "窗口 A 保存的摘要"
    assert "original_path" not in second.json()["detail"]["current"]
    assert records.read("workspace_items", item["id"]).payload["draft"]["summary"] == "窗口 A 保存的摘要"
    stale = http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": first_revision,
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "draft_revision_conflict"
    assert records.list("documents") == ()
    confirmed = http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": saved["revision"],
    })
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["reviewed_revision"] == saved["revision"]
    assert "窗口 A 保存的摘要" in SQLiteDocumentRepository(
        records, namespace_id="recognition",
    ).markdown(result["document_id"])
    assert http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": saved["revision"],
    }).status_code == 200
    assert http.post(path + "/confirm", json={
        "project_id": "alpha", "expected_revision": first_revision,
    }).status_code == 409


@pytest.mark.parametrize("bad_revision", [None, True, "2", 0, -1])
def test_draft_and_confirmation_require_positive_integer_revision(tmp_path, bad_revision):
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/text", json={"text": "原文证据"}).json()
    ready = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    revision_body = {} if bad_revision is None else {"expected_revision": bad_revision}
    assert http.put(path + "/draft", json={**ready["draft"], **revision_body}).status_code == 422
    assert http.post(path + "/confirm", json=revision_body).status_code == 422


def test_recognition_uses_current_document_content_and_revision(tmp_path):
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/text", json={"project_id": "alpha", "text": "原文证据更多内容"}).json()
    ready = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "alpha"}).json()
    confirmed = http.post(f"/api/workspace/v1/items/{item['id']}/confirm", json={
        "project_id": "alpha", "expected_revision": ready["revision"],
    }).json()
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    documents.save_user_edit(confirmed["document_id"], markdown="用户校订后的正文", expected_revision=1)
    response = http.post(f"/api/workspace/v1/items/{item['id']}/recognition", json={"project_id": "alpha"})
    assert response.status_code == 200, response.text
    experience = records.read("recognition_experiences", response.json()["experience_id"]).payload
    assert experience["content"] == "用户校订后的正文"
    assert experience["provenance"]["source_refs"][0]["revision"] == 2
    assert records.read("recognition_candidates", response.json()["candidate_id"]).payload["content"] == "用户校订后的正文"
    documents.save_user_edit(confirmed["document_id"], markdown="提取后再次校订的正文", expected_revision=2)
    newer = http.post(f"/api/workspace/v1/items/{item['id']}/recognition", json={"project_id": "alpha"}).json()
    assert newer["candidate_id"] != response.json()["candidate_id"]
    current = records.read("recognition_experiences", newer["experience_id"]).payload
    assert current["content"] == "提取后再次校订的正文"
    assert current["provenance"]["source_refs"][0]["revision"] == 3
    assert records.read("recognition_candidates", response.json()["candidate_id"]).payload["content"] == "用户校订后的正文"
    assert http.post(f"/api/workspace/v1/items/{item['id']}/recognition", json={"project_id": "alpha"}).json() == newer
    documents.archive(confirmed["document_id"], expected_revision=3)
    assert http.post(f"/api/workspace/v1/items/{item['id']}/recognition", json={"project_id": "alpha"}).status_code == 409


def test_shared_document_query_uses_current_revision_and_excludes_archive(tmp_path):
    http, records = client(tmp_path)
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(
        title="Old OS material", document_type="legacy-material",
        markdown="Old revision text", source_refs=({"source_id": "old-source", "locator": "text:0:3"},),
        project_id="alpha"))
    document_id = document["id"]
    assert len(http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "Old revision"}).json()["items"]) == 1
    assert http.get("/api/workspace/v1/search", params={"project_id": "beta", "q": "Old revision"}).json()["items"] == []
    edited = documents.save_user_edit(document_id, markdown="New revision evidence", expected_revision=1)
    assert edited["revision"] == 2
    assert http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "Old revision"}).json()["items"] == []
    result = http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "New revision"}).json()["items"]
    assert result[0]["document_id"] == document_id
    assert http.post("/api/workspace/v1/ask", json={"project_id": "alpha", "question": "New revision"}).json()["sources"][0]["id"] == document_id
    documents.archive(document_id, expected_revision=2)
    assert http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "New revision"}).json()["items"] == []
    assert http.post("/api/workspace/v1/ask", json={"project_id": "alpha", "question": "New revision"}).json()["sources"] == []


def test_document_ask_revalidation_reads_selected_ids_without_enumerating_all_documents(tmp_path, monkeypatch):
    http, records = client(tmp_path)
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(title="Selected", document_type="legacy-material",
        markdown="uniqueselectedneedle", source_refs=({"source_id": "source-1", "locator": "text:0:20"},),
        project_id="alpha"))
    request = {"project_id": "alpha", "question": "uniqueselectedneedle"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    assert len(preview["sources"]) == 1
    def no_full_scan(*args, **kwargs):
        raise AssertionError("selected document validation enumerated the corpus")
    monkeypatch.setattr(SQLiteDocumentRepository, "list", no_full_scan)
    result = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"]})
    assert result.status_code == 200
    assert result.json()["sources"][0]["id"] == document["id"]
    assert http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"]}).json() == result.json()


def test_old_source_without_document_is_queryable_in_its_project(tmp_path):
    http, _ = client(tmp_path)
    store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="recognition")
    store.write("sources", "old-source", {"id": "old-source", "title": "Original material",
                "project_id": "alpha", "metadata": {"content": "Unique retained source sentence"}}, expected_revision=0)
    result = http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "retained source"}).json()["items"]
    assert result[0]["source_id"] == "old-source"
    assert http.get("/api/workspace/v1/search", params={"project_id": "beta", "q": "retained source"}).json()["items"] == []
    answer = http.post("/api/workspace/v1/ask", json={
        "project_id": "alpha", "question": "What does the retained source say?",
    })
    assert answer.status_code == 200, answer.text
    assert answer.json()["sources"][0]["id"] == "old-source"
    assert http.post("/api/workspace/v1/ask", json={
        "project_id": "beta", "question": "What does the retained source say?",
    }).json()["sources"] == []


def test_old_source_change_during_answer_returns_retry_conflict(tmp_path):
    store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="recognition")
    source = {"id": "old-source", "title": "Original material", "project_id": "alpha",
              "metadata": {"content": "Unique retained source sentence"}}
    store.write("sources", "old-source", source, expected_revision=0)

    class ChangingSourceModel(Model):
        def complete(self, messages, *, max_tokens, validate_current=None):
            store.write("sources", "old-source", {**source, "title": "Revised material"}, expected_revision=1)
            return super().complete(messages, max_tokens=max_tokens, validate_current=validate_current)

    http, _ = client(tmp_path, ChangingSourceModel())
    response = http.post("/api/workspace/v1/ask", json={
        "project_id": "alpha", "question": "What does the retained source say?",
    })
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "source_changed_retry"


def test_long_document_late_evidence_stays_in_search_preview_and_answer(tmp_path):
    class InspectingModel(Model):
        sent = ""

        def complete(self, messages, *, max_tokens, validate_current=None):
            if validate_current:
                validate_current()
            self.sent = messages[-1]["content"]
            return super().complete(messages, max_tokens=max_tokens)

    model = InspectingModel()
    http, records = client(tmp_path, model)
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(
        title="Long neutral document", document_type="legacy",
        markdown="a" * 5200 + " tailmarker evidence sits here " + "b" * 600,
        source_refs=({"source_id": "long-evidence-source", "locator": "text:0:1"},),
        project_id="alpha",
    ))
    search = http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "tailmarker"})
    assert search.status_code == 200, search.text
    hit = search.json()["items"][0]
    assert hit["id"] == document["id"]
    assert "tailmarker evidence" in hit["snippet"]
    assert hit["start"] >= 5000

    request = {"project_id": "alpha", "question": "tailmarker evidence"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request)
    assert preview.status_code == 200, preview.text
    excerpt = preview.json()["sources"][0]
    assert "tailmarker evidence" in excerpt["excerpt"]
    assert excerpt["start"] <= 5200 < excerpt["end"]
    assert excerpt["end"] - excerpt["start"] <= 2400
    answer = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview.json()["preview_id"]})
    assert answer.status_code == 200, answer.text
    assert "tailmarker evidence" in model.sent
    assert "tailmarker evidence" in answer.json()["sources"][0]["excerpt"]
    assert answer.json()["sources"][0]["start"] == excerpt["start"]


def test_long_legacy_source_late_evidence_uses_same_source_offsets(tmp_path):
    http, _records = client(tmp_path)
    source_store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="recognition")
    source_store.write("sources", "long-legacy-source", {
        "id": "long-legacy-source", "title": "Neutral legacy source", "project_id": "alpha",
        "metadata": {"content": "a" * 5200 + " sourceendmarker result " + "b" * 200},
    }, expected_revision=0)
    hit = http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": "sourceendmarker"}).json()["items"][0]
    assert hit["kind"] == "source" and "sourceendmarker" in hit["snippet"]
    assert hit["start"] <= 5200 < hit["end"]
    request = {"project_id": "alpha", "question": "sourceendmarker result"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    source = preview["sources"][0]
    assert source["type"] == "source" and "sourceendmarker result" in source["excerpt"]
    assert source["windows"][0]["start"] <= 5200 < source["windows"][0]["end"]
    answer = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["sources"][0]["windows"] == source["windows"]


def test_search_unicode_single_character_and_long_tail_word_have_visible_snippets(tmp_path):
    http, records = client(tmp_path)
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    for term in ("税", "🙂", "é", "x" * 190):
        document = documents.create(DocumentDraft(
            title="Neutral document", document_type="legacy", markdown="a" * 5200 + term,
            source_refs=({"source_id": "source-" + str(ord(term[0])), "locator": "text:0:1"},),
            project_id="alpha",
        ))
        response = http.get("/api/workspace/v1/search", params={"project_id": "alpha", "q": term})
        assert response.status_code == 200, response.text
        hit = next(item for item in response.json()["items"] if item["id"] == document["id"])
        assert term in hit["snippet"]
        assert hit["windows"] and hit["start"] <= 5200 < hit["end"]


def test_remote_ask_freezes_disjoint_windows_and_body_free_receipt(tmp_path):
    class WindowModel(Model):
        calls = 0

        def public(self):
            return {"generation": {"provider": "openai", "base_url": "https://api.example.test/v1",
                                   "model": "remote-qa", "revision": 1, "allow_remote": True}}

        def complete(self, messages, *, max_tokens, validate_current=None):
            if validate_current:
                validate_current()
            self.calls += 1
            assert "firstmarker" in messages[-1]["content"]
            assert "secondmarker" in messages[-1]["content"]
            assert "PRIVATE_MIDDLE" * 100 not in messages[-1]["content"]
            return super().complete(messages, max_tokens=max_tokens)

    model = WindowModel()
    http, records = client(tmp_path, model)
    SQLiteDocumentRepository(records, namespace_id="recognition").create(DocumentDraft(
        title="Two distant facts", document_type="legacy",
        markdown="firstmarker " + "PRIVATE_MIDDLE" * 400 + " secondmarker",
        source_refs=({"source_id": "two-window-source", "locator": "text:0:1"},), project_id="alpha",
    ))
    request = {"project_id": "alpha", "question": "firstmarker secondmarker"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    source = preview["sources"][0]
    assert len(source["windows"]) == 2
    assert "firstmarker" in source["excerpt"] and "secondmarker" in source["excerpt"]
    assert model.calls == 0
    answer = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"],
                                                     "remote_processing_consent": True})
    assert answer.status_code == 200, answer.text
    assert answer.json()["sources"][0]["excerpt"] == source["excerpt"]
    assert answer.json()["sources"][0]["windows"] == source["windows"]
    receipt = records.read("workspace_ask_receipts", preview["preview_id"]).payload
    assert receipt["sources"][0]["windows"] == source["windows"]
    assert "firstmarker" not in json.dumps(receipt)
    assert "secondmarker" not in json.dumps(receipt)
    assert model.calls == 1


def test_remote_ask_runs_directly_and_retains_single_frozen_preview_execution(tmp_path):
    class RemoteAskModel(Model):
        calls = 0

        def public(self):
            return {"generation": {"provider": "openai", "base_url": "https://api.example.test/v1",
                                   "model": "remote-qa", "revision": 1, "allow_remote": True},
                    "generation_mode": {"revision": 2}}

        def complete(self, messages, *, max_tokens, validate_current=None):
            if validate_current:
                validate_current()
            self.calls += 1
            assert "unique remote excerpt" in messages[-1]["content"]
            return super().complete(messages, max_tokens=max_tokens)

    model = RemoteAskModel()
    http, records = client(tmp_path, model)
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    documents.create(DocumentDraft(title="Remote QA", document_type="legacy",
                                   markdown="unique remote excerpt " + "a" * 500,
                                   source_refs=({"source_id": "qa-doc-source", "locator": "text:0:1"},),
                                   project_id="alpha"))
    request = {"project_id": "alpha", "question": "unique remote excerpt"}
    direct = http.post("/api/workspace/v1/ask", json=request)
    assert direct.status_code == 200, direct.text
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    assert preview["execution_location"] == "remote"
    assert preview["model_target"]["model"] == "remote-qa"
    assert preview["sources"][0]["excerpt"].startswith("unique remote excerpt")
    assert len(preview["sources"][0]["excerpt"]) > 280
    assert model.calls == 1
    submission = {**request, "preview_id": preview["preview_id"]}
    assert http.post("/api/workspace/v1/ask", json={**submission, "project_id": "beta",
                                                   "remote_processing_consent": True}).status_code == 404
    assert model.calls == 1
    submitted = {**submission, "remote_processing_consent": True}
    answer = http.post("/api/workspace/v1/ask", json=submission)
    assert answer.status_code == 200, answer.text
    assert answer.json()["sources"][0]["type"] == "document"
    assert http.post("/api/workspace/v1/ask", json=submitted).json() == answer.json()
    assert model.calls == 2
    receipt = records.read("workspace_ask_receipts", preview["preview_id"]).payload
    assert receipt["status"] == "completed" and receipt["attempt"] == 1
    assert receipt["sources"][0]["windows"][0]["end"] > 280
    assert "unique remote excerpt" not in json.dumps(receipt)


def test_remote_ask_source_eligibility_and_model_drift_block_wire(tmp_path):
    class RemoteAskModel(Model):
        revision = 1
        drift_at_wire = False
        wire_calls = 0

        def public(self):
            return {"generation": {"provider": "openai", "base_url": "https://api.example.test/v1",
                                   "model": "remote-qa", "revision": self.revision, "allow_remote": True}}

        def complete(self, messages, *, max_tokens, validate_current=None):
            if self.drift_at_wire:
                self.revision += 1
                self.drift_at_wire = False
            if validate_current:
                validate_current()
            self.wire_calls += 1
            return super().complete(messages, max_tokens=max_tokens)

    model = RemoteAskModel()
    http, records = client(tmp_path, model)
    source_store = JsonObjectStore(tmp_path / ".rebuild-data", namespace_id="recognition")
    source_store.write("sources", "old-source", {"id": "old-source", "title": "Original material",
                       "project_id": "alpha", "metadata": {"content": "retained source evidence"}},
                       expected_revision=0)
    request = {"project_id": "alpha", "question": "retained source evidence"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    documents = SQLiteDocumentRepository(records, namespace_id="recognition")
    document = documents.create(DocumentDraft(title="Archived evidence", document_type="legacy",
                                              markdown="unrelated", source_refs=({"source_id": "old-source", "locator": "text:0:1"},),
                                              project_id="alpha"))
    documents.archive(document["id"], expected_revision=1)
    blocked = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"],
                                                    "remote_processing_consent": True})
    assert blocked.status_code == 409 and blocked.json()["detail"] == "source_changed_retry"
    assert model.wire_calls == 0

    documents.restore(document["id"], expected_revision=2)
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    model.drift_at_wire = True
    blocked = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"],
                                                    "remote_processing_consent": True})
    assert blocked.status_code == 409 and blocked.json()["detail"] == "ask_model_target_changed"
    assert model.wire_calls == 0
    assert records.read("workspace_ask_receipts", preview["preview_id"]).payload["status"] == "failed"
    assert http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"],
                                                   "remote_processing_consent": True}).json()["detail"] == "ask_preview_used"


def test_local_ask_switching_to_remote_after_preparation_stops_before_wire(tmp_path):
    class SwitchingAskModel(Model):
        remote = False
        wire_calls = 0

        def public(self):
            return {"generation": {"provider": "openai", "base_url":
                                    "https://api.example.test/v1" if self.remote else "http://127.0.0.1:8000/v1",
                                    "model": "test-model", "revision": 2 if self.remote else 1}}

        def complete(self, messages, *, max_tokens, validate_current=None):
            self.remote = True
            if validate_current:
                validate_current()
            self.wire_calls += 1
            return super().complete(messages, max_tokens=max_tokens)

    model = SwitchingAskModel()
    http, records = client(tmp_path, model)
    SQLiteDocumentRepository(records, namespace_id="recognition").create(DocumentDraft(
        title="Local evidence", document_type="legacy", markdown="local evidence text",
        source_refs=({"source_id": "qa-local-source", "locator": "text:0:1"},), project_id="alpha"))
    answer = http.post("/api/workspace/v1/ask", json={"project_id": "alpha", "question": "local evidence"})
    assert answer.status_code == 409 and answer.json()["detail"] == "ask_model_target_changed"
    assert model.wire_calls == 0


def test_model_failure_is_persisted_and_retryable(tmp_path):
    model = Model(failure=True)
    http, _ = client(tmp_path, model)
    item = http.post("/api/workspace/v1/items/text", json={"text": "真实原文"}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    result = http.post(path + "/process", json={}).json()
    assert result["status"] == "failed"
    assert "secret" not in json.dumps(result)
    assert http.post(path + "/confirm", json={"expected_revision": result["revision"]}).status_code == 409
    assert http.post(path + "/retry", json={}).json()["status"] == "staged"
    model.failure = False
    assert http.post(path + "/process", json={}).json()["status"] == "ready"


def test_remote_generation_obeys_global_authorization_and_rechecks_target_before_wire(tmp_path):
    class RemoteModel(Model):
        revision = 1
        drift_at_wire = False
        calls = 0
        wire_calls = 0
        allowed = False

        def public(self):
            return {"generation": {
                "base_url": "https://api.example.test/v1", "model": "remote-model",
                "revision": self.revision, "allow_remote": self.allowed, "enabled": True,
            }}

        def complete(self, messages, *, max_tokens, validate_current=None):
            self.calls += 1
            if self.drift_at_wire:
                self.revision += 1
                self.drift_at_wire = False
            if validate_current:
                validate_current()
            self.wire_calls += 1
            return super().complete(messages, max_tokens=max_tokens)

    model = RemoteModel()
    http, records = client(tmp_path, model)
    item = http.post("/api/workspace/v1/items/text", json={"text": "原文证据"}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    rejected = http.post(path + "/process", json={})
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "remote_disabled"
    assert records.read("workspace_items", item["id"]).payload["status"] == "staged"
    assert model.calls == 0

    model.allowed = True
    model.drift_at_wire = True
    drifted = http.post(path + "/process", json={})
    assert drifted.status_code == 409
    assert drifted.json()["detail"] == "remote_processing_target_changed"
    drifted_item = records.read("workspace_items", item["id"]).payload
    assert drifted_item["status"] == "failed"
    assert drifted_item["error"] == "remote_processing_target_changed"
    receipt = drifted_item["remote_processing_receipts"][0]
    assert receipt["item_id"] == item["id"]
    assert receipt["project_id"] == "default"
    assert receipt["run_id"].startswith("workspace-run-")
    assert receipt["send_categories"] == ["model_instructions", "source_text"]
    assert receipt["generation"]["revision"] == 1
    assert "api_key" not in json.dumps(receipt)
    assert model.wire_calls == 0
    assert http.post(path + "/retry", json={}).json()["status"] == "staged"
    ready = http.post(path + "/process", json={}).json()
    assert ready["status"] == "ready"
    assert model.wire_calls == 1
    receipts = records.read("workspace_items", item["id"]).payload["remote_processing_receipts"]
    assert len(receipts) == 2
    assert receipts[0]["run_id"] != receipts[1]["run_id"]


def test_local_generation_switch_to_remote_before_snapshot_is_rejected_without_egress(tmp_path):
    class SwitchingModel(Model):
        remote = False
        wire_calls = 0

        def public(self):
            return {"generation": {
                "base_url": "https://api.example.test/v1" if self.remote else "http://127.0.0.1:8000/v1",
                "model": "test-model", "revision": 2 if self.remote else 1,
                "allow_remote": self.remote, "enabled": True,
            }}

        def complete(self, messages, *, max_tokens, validate_current=None):
            # Simulate a settings edit between process preflight and model snapshot.
            self.remote = True
            if validate_current:
                validate_current()
            self.wire_calls += 1
            return super().complete(messages, max_tokens=max_tokens)

    model = SwitchingModel()
    http, records = client(tmp_path, model)
    item = http.post("/api/workspace/v1/items/text", json={"text": "原文证据"}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    response = http.post(path + "/process", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "remote_processing_target_changed"
    saved = records.read("workspace_items", item["id"]).payload
    assert saved["status"] == "failed"
    assert saved["error"] == "remote_processing_target_changed"
    assert saved["source_text"] == "原文证据"
    assert saved["remote_processing_receipts"] == []
    assert model.wire_calls == 0


def test_link_local_address_and_unverified_draft_are_rejected(tmp_path):
    http, _ = client(tmp_path)
    assert http.post("/api/workspace/v1/items/link", json={"url": "http://127.0.0.1/private"}).status_code == 400
    item = http.post("/api/workspace/v1/items/text", json={"text": "原文证据"}).json()
    ready = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    draft = ready["draft"]
    draft["facts"][0]["evidence"]["quote"] = "伪造依据"
    assert http.put(f"/api/workspace/v1/items/{item['id']}/draft", json={
        **draft, "expected_revision": ready["revision"],
    }).status_code == 422


def test_audio_without_local_model_fails_explicitly(tmp_path):
    http, _ = client(tmp_path)
    result = http.post("/api/workspace/v1/items/file", data={"project_id": "default"},
                       files={"file": ("sample.ogg", b"OggS-test", "audio/ogg")})
    assert result.status_code == 200
    item = result.json()
    assert "original_path" not in item
    source = http.get(f"/api/workspace/v1/items/{item['id']}/source").json()
    assert source["original_download_url"].endswith("project_id=default")
    assert http.get(source["original_download_url"]).content == b"OggS-test"
    processed = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    assert processed["status"] == "failed"
    assert processed["error"] == "asr_unavailable"


def test_audio_uses_explicit_local_setting_and_reaches_review(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
    )
    selected = []

    class LocalTranscriber:
        def __init__(self, object_store, *, settings_override, **_kwargs):
            self.store = object_store
            selected.append(settings_override.command)

        def execute(self, *, audio_asset_id):
            self.store.write("media_processing_outputs", "local-test-output",
                             _completed_audio_output(self.store, output_id="local-test-output",
                                                     audio_asset_id=audio_asset_id,
                                                     text="本机录音原文证据", provider="local-test"),
                             expected_revision=None)
            return SimpleNamespace(status="completed", output_id="local-test-output")

    monkeypatch.setattr("backend.memory_app.workspace_audio.TranscribeGeneratedAudioAsset", LocalTranscriber)
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "recording.ogg", b"OggS-test", "audio/ogg")}).json()
    processed = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={}).json()
    assert processed["status"] == "ready"
    assert processed["source_text"] == "本机录音原文证据"
    assert selected == [(sys.executable, "--version")]
    assert store.list("sources") == ()


def test_audio_cloud_failure_does_not_fall_back_to_enabled_local(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
    )
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "test-secret"})
    monkeypatch.setattr("backend.api.tokenhub_asr_provider.build_secret_store", lambda _root: secrets)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(
        manifest, manifest_id=manifest.manifest_id, confirm=True,
    )
    monkeypatch.setattr("backend.memory_app.workspace_audio._cloud_audio_derivative",
                        lambda path, _root, _run: (path, 1.0))

    class FailedCloud:
        def __init__(self, *_args, **_kwargs):
            pass

        def execute(self, **_kwargs):
            raise RuntimeError("private remote failure")

    def unexpected_local(*_args, **_kwargs):
        raise AssertionError("cloud failure must not use local ASR")

    monkeypatch.setattr("backend.memory_app.workspace_audio.TokenHubChunkedAudioAssetTranscriber", FailedCloud)
    monkeypatch.setattr("backend.memory_app.workspace_audio.TranscribeGeneratedAudioAsset", unexpected_local)
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "recording.ogg", b"OggS-test", "audio/ogg")}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    assert http.get(path + "/original").content == b"OggS-test"
    processed = http.post(path + "/process", json={}).json()
    assert processed["status"] == "failed"
    assert processed["error"] == "audio_transcription_failed"
    assert "private remote failure" not in str(processed)
    assert http.get(path + "/original").content == b"OggS-test"

    class CompletedCloud:
        def __init__(self, _runtime_root, object_store, **_kwargs):
            self.store = object_store

        def execute(self, *, audio_asset_id, **_kwargs):
            self.store.write("media_processing_outputs", "cloud-test-output",
                             _completed_audio_output(self.store, output_id="cloud-test-output",
                                                     audio_asset_id=audio_asset_id,
                                                     text="云端录音原文证据", provider="tokenhub-asr"),
                             expected_revision=None)
            return SimpleNamespace(status="completed", output_id="cloud-test-output")

    monkeypatch.setattr("backend.memory_app.workspace_audio.TokenHubChunkedAudioAssetTranscriber", CompletedCloud)
    assert http.post(path + "/retry", json={}).json()["status"] == "staged"
    retried = http.post(path + "/process", json={}).json()
    assert retried["status"] == "ready"
    assert retried["source_text"] == "云端录音原文证据"
    assert len(store.list("workbench_asr_bindings")) == 2
    receipts = retried["remote_processing_receipts"]
    assert len(receipts) == 2
    assert receipts[0]["send_categories"] == [
        "audio_data", "audio_chunks", "provider_metadata",
    ]
    assert receipts[0]["asr"]["revision"] == receipts[1]["asr"]["revision"]
    assert receipts[0]["run_id"] != receipts[1]["run_id"]
    assert "test-secret" not in json.dumps(receipts)


def test_audio_model_retry_reuses_completed_cloud_transcript_and_limits_new_consent(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "test-secret"})
    monkeypatch.setattr("backend.api.tokenhub_asr_provider.build_secret_store", lambda _root: secrets)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    monkeypatch.setattr("backend.memory_app.workspace_audio._cloud_audio_derivative",
                        lambda path, _root, _run: (path, 1.0))
    asr_calls = []

    class CompletedCloud:
        def __init__(self, _runtime_root, object_store, **_kwargs):
            self.store = object_store

        def execute(self, *, audio_asset_id, **_kwargs):
            asr_calls.append(audio_asset_id)
            output_id = f"output-{audio_asset_id}"
            self.store.write("media_processing_outputs", output_id,
                             _completed_audio_output(self.store, output_id=output_id,
                                                     audio_asset_id=audio_asset_id,
                                                     text="云端录音原文证据", provider="tokenhub-asr"),
                             expected_revision=None)
            return SimpleNamespace(status="completed", output_id=output_id)

    class RemoteModel(Model):
        revision = 1
        drift_on_next_call = False

        def public(self):
            return {"generation": {"base_url": "https://api.example.test/v1",
                                   "model": "test-model", "revision": self.revision,
                                   "allow_remote": True, "enabled": True}}

        def complete(self, messages, *, max_tokens, validate_current=None):
            if self.drift_on_next_call:
                self.drift_on_next_call = False
                self.revision += 1
            return super().complete(messages, max_tokens=max_tokens,
                                    validate_current=validate_current)

    monkeypatch.setattr("backend.memory_app.workspace_audio.TokenHubChunkedAudioAssetTranscriber", CompletedCloud)
    model = RemoteModel(failure=True)
    http, records = client(tmp_path, model)
    item = http.post("/api/workspace/v1/items/file", data={"project_id": "alpha"}, files={
        "file": ("recording.ogg", b"OggS-test", "audio/ogg"),
    }).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    first = http.post(path + "/process", json={
        "project_id": "alpha", "remote_processing_consent": True,
    }).json()
    assert first["status"] == "failed"
    assert first["source_text"] == "云端录音原文证据"
    assert len(asr_calls) == 1
    saved = records.read("workspace_items", item["id"]).payload
    assert saved["audio_transcription"]["output_id"] == f"output-{asr_calls[0]}"
    assert saved["audio_transcription"]["output_ref"]
    assert saved["audio_transcription"]["original_identity"]["size"] == len(b"OggS-test")
    assert http.get(path + "/original", params={"project_id": "alpha"}).content == b"OggS-test"

    # This run needs only generation. Changing ASR settings must not cause a
    # new audio upload, while the generation target still needs revalidation.
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:01Z").execute(
        enabled=False, confirm_enable=False,
    )
    model.failure = False
    model.drift_on_next_call = True
    # A new app instance must recover the durable transcript from disk.
    http, records = client(tmp_path, model)
    assert http.post(path + "/retry", json={"project_id": "alpha"}).json()["status"] == "staged"
    changed = http.post(path + "/process", json={
        "project_id": "alpha", "remote_processing_consent": True,
    })
    assert changed.status_code == 409
    assert changed.json()["detail"] == "remote_processing_target_changed"
    assert len(asr_calls) == 1
    changed_receipt = records.read("workspace_items", item["id"]).payload["remote_processing_receipts"][1]
    assert changed_receipt["send_categories"] == ["model_instructions", "source_text"]
    assert changed_receipt["asr"] is None

    assert http.post(path + "/retry", json={"project_id": "alpha"}).json()["status"] == "staged"
    second = http.post(path + "/process", json={
        "project_id": "alpha", "remote_processing_consent": True,
    }).json()
    assert second["status"] == "ready"
    assert len(asr_calls) == 1
    receipts = second["remote_processing_receipts"]
    assert len(receipts) == 3
    assert receipts[0]["send_categories"] == [
        "model_instructions", "source_text", "audio_data", "audio_chunks", "provider_metadata",
    ]
    assert receipts[1]["send_categories"] == ["model_instructions", "source_text"]
    assert receipts[1]["asr"] is None
    assert receipts[2]["send_categories"] == ["model_instructions", "source_text"]
    assert receipts[2]["asr"] is None
    assert len({receipt["run_id"] for receipt in receipts}) == 3
    assert "test-secret" not in json.dumps(second)


def _failed_local_audio_with_transcript(tmp_path, monkeypatch):
    store, storage = build_rebuild_object_store(tmp_path)
    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
    )
    asr_calls = []

    class CompletedLocal:
        def __init__(self, object_store, *, settings_override, **_kwargs):
            self.store = object_store
            self.provider = settings_override.provider_name

        def execute(self, *, audio_asset_id):
            asr_calls.append(audio_asset_id)
            output_id = f"output-{audio_asset_id}"
            self.store.write("media_processing_outputs", output_id,
                             _completed_audio_output(self.store, output_id=output_id,
                                                     audio_asset_id=audio_asset_id,
                                                     text="本机录音原文证据", provider=self.provider),
                             expected_revision=None)
            return SimpleNamespace(status="completed", output_id=output_id)

    class CountingModel(Model):
        def __init__(self):
            super().__init__(failure=True)
            self.calls = 0

        def complete(self, messages, *, max_tokens, validate_current=None):
            self.calls += 1
            return super().complete(messages, max_tokens=max_tokens, validate_current=validate_current)

    monkeypatch.setattr("backend.memory_app.workspace_audio.TranscribeGeneratedAudioAsset", CompletedLocal)
    model = CountingModel()
    http, records = client(tmp_path, model)
    item = http.post("/api/workspace/v1/items/file", data={"project_id": "alpha"}, files={
        "file": ("recording.ogg", b"OggS-test", "audio/ogg"),
    }).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    first = http.post(path + "/process", json={"project_id": "alpha"}).json()
    assert first["status"] == "failed"
    assert first["source_text"] == "本机录音原文证据"
    assert len(asr_calls) == model.calls == 1
    checkpoint = records.read("workspace_items", item["id"]).payload["audio_transcription"]
    assert checkpoint["store_kind"] == "workspace_asr_internal"
    assert checkpoint["output_id"] == f"output-{asr_calls[0]}"
    assert checkpoint["output_ref"]
    assert checkpoint["original_identity"]["size"] == len(b"OggS-test")
    return http, records, store, storage, model, asr_calls, item, path, checkpoint


def test_audio_model_retry_reuses_completed_local_transcript_after_restart(tmp_path, monkeypatch):
    _, _, _, _, model, asr_calls, item, path, _ = _failed_local_audio_with_transcript(
        tmp_path, monkeypatch,
    )
    model.failure = False
    http, records = client(tmp_path, model)
    assert http.post(path + "/retry", json={"project_id": "alpha"}).json()["status"] == "staged"
    second = http.post(path + "/process", json={"project_id": "alpha"}).json()
    assert second["status"] == "ready"
    assert second["source_text"] == "本机录音原文证据"
    assert len(asr_calls) == 1
    assert model.calls == 2
    assert records.read("workspace_items", item["id"]).payload["audio_transcription"]["output_id"]
    assert http.get(path + "/original", params={"project_id": "alpha"}).content == b"OggS-test"


@pytest.mark.parametrize("tamper", ["original_changed", "output_deleted"])
def test_audio_model_retry_refuses_invalid_transcript_without_asr_or_model(
    tmp_path, monkeypatch, tamper,
):
    http, records, _, storage, model, asr_calls, item, path, checkpoint = (
        _failed_local_audio_with_transcript(tmp_path, monkeypatch)
    )
    assert http.post(path + "/retry", json={"project_id": "alpha"}).json()["status"] == "staged"
    if tamper == "original_changed":
        Path(records.read("workspace_items", item["id"]).payload["original_path"]).write_bytes(
            b"OggS-changed-recording",
        )
    else:
        internal = JsonObjectStore(tmp_path / "workspace" / "asr-internal",
                                   namespace_id=storage.namespace_id)
        assert internal.delete("media_processing_outputs", checkpoint["output_id"])
    model.failure = False
    response = http.post(path + "/process", json={"project_id": "alpha"})
    assert response.status_code == 409
    assert response.json()["detail"] == "audio_transcription_evidence_invalid"
    assert len(asr_calls) == model.calls == 1
    assert records.read("workspace_items", item["id"]).payload["draft"] is None


def test_superseded_audio_run_cannot_publish_transcript_checkpoint(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveLocalAsrProviderSettings(store).execute(
        enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
    )
    records = None
    item_id = None

    class SupersededLocal:
        def __init__(self, object_store, *, settings_override, **_kwargs):
            self.store = object_store
            self.provider = settings_override.provider_name

        def execute(self, *, audio_asset_id):
            output_id = f"output-{audio_asset_id}"
            self.store.write("media_processing_outputs", output_id,
                             _completed_audio_output(self.store, output_id=output_id,
                                                     audio_asset_id=audio_asset_id,
                                                     text="旧请求转写", provider=self.provider),
                             expected_revision=None)
            with records.begin() as tx:
                row = tx.read("workspace_items", item_id)
                tx.put("workspace_items", item_id,
                       {**row.payload, "processing_run_id": "newer-run"},
                       expected_revision=row.revision)
                tx.commit()
            return SimpleNamespace(status="completed", output_id=output_id)

    monkeypatch.setattr("backend.memory_app.workspace_audio.TranscribeGeneratedAudioAsset", SupersededLocal)
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "recording.ogg", b"OggS-test", "audio/ogg",
    )}).json()
    item_id = item["id"]
    response = http.post(f"/api/workspace/v1/items/{item_id}/process", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "processing_run_changed"
    retained = records.read("workspace_items", item_id).payload
    assert retained["status"] == "processing"
    assert retained["processing_run_id"] == "newer-run"
    assert retained["source_text"] == ""
    assert retained.get("audio_transcription") is None
    assert retained["draft"] is None
    assert retained["error"] is None


@pytest.mark.parametrize("model_outcome", ["success", "failure"])
def test_superseded_audio_run_cannot_publish_ready_or_failed(
    tmp_path, monkeypatch, model_outcome,
):
    http, records, _, _, model, asr_calls, item, path, checkpoint = (
        _failed_local_audio_with_transcript(tmp_path, monkeypatch)
    )
    assert http.post(path + "/retry", json={"project_id": "alpha"}).json()["status"] == "staged"

    def superseded_model(messages, *, max_tokens, validate_current=None):
        with records.begin() as tx:
            row = tx.read("workspace_items", item["id"])
            tx.put("workspace_items", item["id"],
                   {**row.payload, "processing_run_id": "newer-run"},
                   expected_revision=row.revision)
            tx.commit()
        if model_outcome == "failure":
            raise RuntimeError("old request failed")
        return Model().complete(messages, max_tokens=max_tokens)

    monkeypatch.setattr(model, "complete", superseded_model)
    response = http.post(path + "/process", json={"project_id": "alpha"})
    assert response.status_code == 409
    assert response.json()["detail"] == "processing_run_changed"
    assert len(asr_calls) == 1
    retained = records.read("workspace_items", item["id"]).payload
    assert retained["status"] == "processing"
    assert retained["processing_run_id"] == "newer-run"
    assert retained["audio_transcription"] == checkpoint
    assert retained["draft"] is None
    assert retained["error"] is None


def test_audio_cloud_target_change_rejects_before_wire_and_keeps_original(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "test-secret"})
    monkeypatch.setattr("backend.api.tokenhub_asr_provider.build_secret_store", lambda _root: secrets)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(
        manifest, manifest_id=manifest.manifest_id, confirm=True,
    )
    wire_calls = []

    def changed_during_conversion(path, _root, _run):
        SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:01Z").execute(
            enabled=True, confirm_enable=True,
        )
        return path, 1.0

    monkeypatch.setattr("backend.memory_app.workspace_audio._cloud_audio_derivative", changed_during_conversion)
    monkeypatch.setattr("backend.memory_app.workspace_audio.TokenHubChunkedAudioAssetTranscriber",
                        lambda *_args, **_kwargs: wire_calls.append("constructed"))
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "recording.ogg", b"OggS-test", "audio/ogg")}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    refused = http.post(path + "/process", json={"remote_processing_consent": True})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "remote_processing_target_changed"
    assert wire_calls == []
    assert records.read("workspace_items", item["id"]).payload["status"] == "failed"
    assert http.get(path + "/original").content == b"OggS-test"
    receipt = records.read("workspace_items", item["id"]).payload["remote_processing_receipts"][0]
    assert receipt["asr"]["revision"] == 1
    assert receipt["send_categories"] == ["audio_data", "audio_chunks", "provider_metadata"]


def test_workspace_cloud_audio_derivative_and_chunk_need_no_ffmpeg_executable(tmp_path):
    original = tmp_path / "recording.wav"
    with wave.open(str(original), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\x00\x00" * 8000)

    converted, duration = _cloud_audio_derivative(original, tmp_path, "workspace-run-test")
    assert duration == 1.0
    with wave.open(str(converted), "rb") as audio:
        assert audio.getframerate() == 16000
        assert audio.getnchannels() == 1
        assert audio.getnframes() == 16000
    chunk_path = tmp_path / "part.wav"
    assert _split_workspace_wav_chunk(str(converted), str(chunk_path), 0.25, 0.75) == str(chunk_path)
    with wave.open(str(chunk_path), "rb") as chunk:
        assert chunk.getnframes() == 8000


def test_audio_upload_uses_real_cloud_dispatch_contract_with_stubbed_http(tmp_path, monkeypatch):
    store, _ = build_rebuild_object_store(tmp_path)
    SaveCloudAsrProviderSettings(store, now="2026-09-24T00:00:00Z").execute(
        enabled=True, confirm_enable=True,
    )
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: "test-secret"})
    monkeypatch.setattr("backend.api.tokenhub_asr_provider.build_secret_store", lambda _root: secrets)
    manifest = tokenhub_egress_manifest(tmp_path)
    ProviderEgressPolicyStore(tmp_path).grant(
        manifest, manifest_id=manifest.manifest_id, confirm=True,
    )
    response = json.loads((Path(__file__).parents[1] / "fixtures" /
                           "tokenhub_hy_asr_sync_completed.json").read_text("utf-8"))["response"]
    calls = []

    def cloud_response(url, headers, body, timeout):
        calls.append((url, len(body)))
        return 200, response

    monkeypatch.setattr("backend.api.tokenhub_asr_provider._http_call", cloud_response)
    recording = tmp_path / "recording.wav"
    with wave.open(str(recording), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 8000)
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "recording.wav", recording.read_bytes(), "audio/wav")}).json()
    processed = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={
        "remote_processing_consent": True,
    }).json()
    assert processed["status"] == "ready"
    assert processed["source_text"]
    assert len(calls) == 1
    assert calls[0][0].endswith("/v1/wand/asrproxy/sync_transcribe")
    outputs = store.list("media_processing_outputs")
    assert len(outputs) == 1
    assert outputs[0]["source_type"] == "audio"
    assert outputs[0]["metadata"]["remote_processing"] is True
    assert store.list("sources") == ()


def test_browser_recording_webm_is_accepted(tmp_path):
    http, _ = client(tmp_path)
    result = http.post("/api/workspace/v1/items/file", data={"project_id": "alpha"},
                       files={"file": ("recording.webm", b"webm-test", "audio/webm")})
    assert result.status_code == 200
    item = result.json()
    assert item["input_kind"] == "audio"
    assert "original_path" not in item
    assert http.get(f"/api/workspace/v1/items/{item['id']}/original", params={"project_id": "default"}).status_code == 404


def test_local_model_near_quote_becomes_literal_source_evidence():
    source = "A traveller arrived in a warm cloak. Then the sun shone warmly."
    raw = {"title": "故事", "summary": "有人到来。", "topics": [],
           "facts": [{"text": "A traveler arrived in a warm cloak.",
                      "evidence": {"quote": "A traveler arrived in a warm cloak."}}],
           "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": []}
    draft = _draft(_ground_local_draft(raw, source), source)
    assert draft["facts"][0] == {"text": "A traveller arrived in a warm cloak.",
                                  "evidence": {"start": 0, "end": 36,
                                               "quote": "A traveller arrived in a warm cloak."}}


def test_local_grounding_keeps_decimal_version_and_full_assertion():
    source = "Seeks to obtain rights for releases prior to 2.1, for relicensing under the PSF Python license."
    raw = {"title": "使命", "summary": "授权目标", "topics": [],
           "facts": [{"text": "Seeks to obtain rights for releases prior to 2.",
                      "evidence": {"quote": "Seeks to obtain rights for releases prior to 2."}},
                     {"text": "Seeks to obtain rights", "evidence": {"quote": "Seeks to obtain rights"}}],
           "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": []}
    facts = _draft(_ground_local_draft(raw, source), source)["facts"]
    assert facts == [{"text": source, "evidence": {"start": 0, "end": len(source), "quote": source}}] * 2


def test_same_content_in_different_items_has_separate_documents(tmp_path):
    http, _ = client(tmp_path)
    document_ids = []
    for project_id in ("alpha", "alpha", "beta"):
        item = http.post("/api/workspace/v1/items/text", json={"project_id": project_id,
                                                               "text": "原文证据更多内容"}).json()
        path = f"/api/workspace/v1/items/{item['id']}"
        ready = http.post(path + "/process", json={"project_id": project_id}).json()
        assert ready["status"] == "ready"
        confirmed = http.post(path + "/confirm", json={
            "project_id": project_id, "expected_revision": ready["revision"],
        })
        assert confirmed.status_code == 200
        document_ids.append(confirmed.json()["document_id"])
    assert len(set(document_ids)) == 3


def test_download_keeps_original_filename_after_model_title_changes(tmp_path):
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "meeting-notes.txt", b"source text", "text/plain")}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    http.post(path + "/process", json={})
    response = http.get(path + "/original")
    assert response.status_code == 200
    assert "meeting-notes.txt" in response.headers["content-disposition"]


def test_local_model_respects_explicit_todos_and_unknowns():
    source = "会议记录\n待办：核对网页引用。\n待办：检查窄屏布局。\n尚未确定：图片和视频何时接入。"
    raw = {"title": "会议", "summary": "讨论功能", "topics": [],
           "facts": [{"text": "图片和视频何时接入。", "evidence": {"quote": "图片和视频何时接入。"}}],
           "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": []}
    draft = _draft(_ground_local_draft(raw, source), source)
    assert draft["facts"] == []
    assert len(draft["todos"]) == 2
    assert all(source[item["evidence"]["start"]:item["evidence"]["end"]] == item["evidence"]["quote"]
               for item in draft["todos"])
    assert draft["uncertainties"] == ["图片和视频何时接入。"]


def test_legacy_processing_without_lease_is_interrupted_even_when_pid_is_live(tmp_path):
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/text", json={"text": "可审核的原文"}).json()
    with records.begin() as tx:
        row = tx.read("workspace_items", item["id"])
        tx.put("workspace_items", item["id"],
               {**row.payload, "status": "processing", "processing_pid": os.getpid()},
               expected_revision=row.revision)
        tx.commit()
    client(tmp_path)
    recovered = records.read("workspace_items", item["id"]).payload
    assert recovered["status"] == "failed"
    assert recovered["error"] == "processing_interrupted"
    assert recovered["source_text"] == "可审核的原文"
    client(tmp_path)
    assert records.read("workspace_items", item["id"]).payload == recovered


def test_second_app_preserves_a_real_live_processing_lease_in_same_pid(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(Model):
        def complete(self, messages, *, max_tokens, validate_current=None):
            entered.set()
            assert release.wait(10)
            return super().complete(messages, max_tokens=max_tokens, validate_current=validate_current)

    http, records = client(tmp_path, BlockingModel())
    item = http.post("/api/workspace/v1/items/text", json={"text": "可审核的原文"}).json()
    result = []
    worker = threading.Thread(target=lambda: result.append(http.post(
        f"/api/workspace/v1/items/{item['id']}/process", json={"project_id": "default"})))
    worker.start()
    try:
        assert entered.wait(10)
        active = records.read("workspace_items", item["id"]).payload
        assert active["status"] == "processing" and active["processing_pid"] == os.getpid()
        assert active["processing_instance_id"] and active["processing_lease_expires_at"]
        second_http, _ = client(tmp_path)
        listed = second_http.get("/api/workspace/v1/items", params={"project_id": "default"}).json()["items"]
        assert listed[0]["status"] == "processing"
        assert "processing_instance_id" not in listed[0]
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert result[0].status_code == 200, result[0].text
    assert records.read("workspace_items", item["id"]).payload["status"] == "ready"


def test_same_app_list_recovers_an_expired_orphan_without_replaying_model(tmp_path, monkeypatch):
    now = [100.0]

    class ClockLease(ProcessingLease):
        def __init__(self, records, collection, instance_id):
            super().__init__(records, collection, instance_id, clock=lambda: now[0], ttl_seconds=10)

    monkeypatch.setattr("backend.memory_app.workspace.ProcessingLease", ClockLease)
    http, records = client(tmp_path)
    item = http.post("/api/workspace/v1/items/text", json={"text": "原件仍在"}).json()
    with records.begin() as tx:
        row = tx.read("workspace_items", item["id"])
        tx.put("workspace_items", item["id"], {
            **row.payload, "status": "processing", "processing_pid": os.getpid(),
            "processing_instance_id": "other-instance", "processing_run_id": "orphan-run",
            "processing_heartbeat_at": 100.0, "processing_lease_expires_at": 110.0,
        }, expected_revision=row.revision)
        tx.commit()
    assert http.get("/api/workspace/v1/items").json()["items"][0]["status"] == "processing"
    now[0] = 111.0
    recovered = http.get("/api/workspace/v1/items").json()["items"][0]
    assert recovered["status"] == "failed" and recovered["error"] == "processing_interrupted"
    assert recovered["source_text"] == "原件仍在"
    assert http.get("/api/workspace/v1/items").json()["items"][0]["revision"] == recovered["revision"]


def test_long_request_heartbeat_keeps_lease_live_past_initial_ttl(tmp_path, monkeypatch):
    from queue import Queue

    clock = [100.0]
    renewals = Queue()

    class FastLease(ProcessingLease):
        def __init__(self, records, collection, instance_id):
            super().__init__(records, collection, instance_id, clock=lambda: clock[0], ttl_seconds=0.3)

        def heartbeat(self, item_id, project_id, run_id):
            renewed = super().heartbeat(item_id, project_id, run_id)
            if renewed:
                renewals.put(self.guard(item_id, project_id, run_id)["processing_heartbeat_at"])
            return renewed

    monkeypatch.setattr("backend.memory_app.workspace.ProcessingLease", FastLease)
    monkeypatch.setattr("backend.memory_app.workspace_intake._PROCESSING_HEARTBEAT_INTERVAL", 0.04)
    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(Model):
        def complete(self, messages, *, max_tokens, validate_current=None):
            entered.set()
            assert release.wait(10)
            return super().complete(messages, max_tokens=max_tokens, validate_current=validate_current)

    http, records = client(tmp_path, BlockingModel())
    item = http.post("/api/workspace/v1/items/text", json={"text": "可审核的原文"}).json()
    result = []
    worker = threading.Thread(target=lambda: result.append(http.post(
        f"/api/workspace/v1/items/{item['id']}/process", json={})))
    worker.start()
    try:
        assert entered.wait(10)
        # Advance beyond the original deadline only after actual async renewals.
        # CPU load must not decide whether a 300 ms fixture lease expires.
        for tick in (100.2, 100.4):
            clock[0] = tick
            while renewals.get(timeout=5) < tick:
                pass
        second_http, _ = client(tmp_path)
        assert second_http.get("/api/workspace/v1/items").json()["items"][0]["status"] == "processing"
        current_row = records.read("workspace_items", item["id"])
        assert current_row.revision == 2  # Heartbeats preserve the frozen source revision.
        heartbeat = records.read("workspace_processing_heartbeats", item["id"]).payload
        assert heartbeat["processing_heartbeat_at"] < heartbeat["processing_lease_expires_at"]
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert result[0].status_code == 200, result[0].text


def test_cancelled_processing_request_stops_lease_and_requires_explicit_retry(tmp_path, monkeypatch):
    model = Model()
    http, records = client(tmp_path, model)
    item = http.post("/api/workspace/v1/items/text", json={"text": "保留原文"}).json()
    original_run = __import__("backend.memory_app.workspace_intake", fromlist=["run_in_threadpool"]).run_in_threadpool

    async def scenario():
        entered = asyncio.Event()

        async def controlled_run(func, *args, **kwargs):
            if getattr(getattr(func, "__self__", None), "models", None) is model:
                entered.set()
                await asyncio.Event().wait()
            return await original_run(func, *args, **kwargs)

        monkeypatch.setattr("backend.memory_app.workspace_intake.run_in_threadpool", controlled_run)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http.app), base_url="http://testserver") as async_http:
            task = asyncio.create_task(async_http.post(
                f"/api/workspace/v1/items/{item['id']}/process", json={}))
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    row = records.read("workspace_items", item["id"]).payload
    assert row["status"] == "failed" and row["error"] == "processing_interrupted"
    assert row["source_text"] == "保留原文"
    assert row["processing_lease_expires_at"] is None
    assert row.get("draft") is None


def test_corrupt_audio_explains_transcription_failure_and_can_retry(tmp_path, monkeypatch):
    def cannot_decode(*_args, **_kwargs):
        raise RuntimeError("private ffmpeg details")

    monkeypatch.setattr("backend.memory_app.workspace_audio._transcribe_output", cannot_decode)
    http, _ = client(tmp_path)
    item = http.post("/api/workspace/v1/items/file", files={"file": (
        "broken.ogg", b"not an audio stream", "audio/ogg")}).json()
    path = f"/api/workspace/v1/items/{item['id']}"
    result = http.post(path + "/process", json={}).json()
    assert result["status"] == "failed"
    assert result["error"] == "audio_transcription_failed"
    assert "private ffmpeg details" not in str(result)
    assert http.post(path + "/retry", json={}).json()["status"] == "staged"


def test_ask_preserves_recognition_conditions_beyond_evidence_window(tmp_path):
    sent = []

    class CapturingModel(Model):
        def complete(self, messages, **kwargs):
            sent.append(messages)
            return super().complete(messages, **kwargs)

    http, records = client(tmp_path, CapturingModel())
    service = RecognitionService(records)
    scope = WorkScope("local-user", "alpha")
    experience_id = service.stage_experience(scope=scope, content="网页原型经验")
    condition = "仅适用于网页原型阶段，桌面发行时必须重新评估。"
    candidate = service.propose(scope=scope, content="网页原型可以后置桌面壳。" + "验证材料。" * 160,
                                source_experience_ids=[experience_id], conditions=[condition])
    recognition = service.publish(scope=scope, candidate_id=candidate.candidate_id,
                                  expected_revision=candidate.revision, reviewer="local-user")
    SourceEgressService(records).set_policy(scope, "experience", experience_id, 1, 0, ["generation", "embedding", "rerank"])
    request = {"project_id": "alpha", "question": "网页原型"}
    preview_response = http.post("/api/workspace/v1/ask/preview", json=request)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["sources"][0]["id"] == recognition.id
    from backend.memory_app.context_adapter import format_recognition_content
    expected = format_recognition_content(recognition.retrieval_projection())
    assert preview["sources"][0]["excerpt"] == expected
    answer = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"]})
    assert answer.status_code == 200, answer.text
    assert expected in sent[-1][-1]["content"]
    assert answer.json()["sources"][0]["excerpt"] == expected


def _published_conditional_recognition(records, content, conditions, *, authorized=True):
    service = RecognitionService(records)
    scope = WorkScope("local-user", "alpha")
    experience = service.stage_experience(scope=scope, content="公开合成依据")
    candidate = service.propose(scope=scope, content=content,
                                source_experience_ids=[experience], conditions=conditions)
    result = service.publish(scope=scope, candidate_id=candidate.candidate_id,
                             expected_revision=candidate.revision, reviewer="local-user")
    SourceEgressService(records).set_policy(scope, "experience", experience, 1, 0,
                                          ["generation", "embedding", "rerank"] if authorized else [])
    return result


def _condition_for_token_size(item, target):
    from backend.memory_app.context_adapter import format_recognition_content
    from backend.memory_app.v2.budget import text_tokens
    base = {**item.retrieval_projection(), "conditions":[""]}
    condition = "甲" * (target - text_tokens(format_recognition_content(base)))
    assert text_tokens(format_recognition_content({**base, "conditions":[condition]})) == target
    return condition


@pytest.mark.parametrize("remaining,reason", [
    (-1, "recognition_evidence_budget_insufficient"),
    (0, "recognition_evidence_budget_insufficient"),
    (1, "recognition_evidence_budget_insufficient"),
    (10, "recognition_evidence_budget_insufficient"),
    (21, "recognition_evidence_budget_insufficient"),
    (22, None),
])
def test_ask_conditions_budget_boundaries_keep_complete_evidence(tmp_path, remaining, reason):
    from backend.memory_app.context_adapter import format_recognition_content
    model = Model()
    http, records = client(tmp_path, model)
    item = _published_conditional_recognition(records, "alphaomega extra proof", [])
    from backend.memory_app.v2.budget import WINDOW_TOKENS, text_tokens
    condition = _condition_for_token_size(item, WINDOW_TOKENS + 22 - remaining)
    item = RecognitionService(records).revise(scope=WorkScope("local-user", "alpha"), recognition_id=item.id,
        expected_revision=item.revision, content=item.content, conditions=[condition])
    request = {"project_id": "alpha", "question": "alphaomega"}
    response = http.post("/api/workspace/v1/ask/preview", json=request)
    assert response.status_code == 200, response.text
    preview = response.json()
    if reason:
        assert preview["no_match"] and not preview["sources"]
        assert preview["excluded_sources"] == [{"type": "recognition", "id": item.id,
                                                "revision": item.revision, "reason": reason}]
        direct = http.post("/api/workspace/v1/ask", json=request).json()
        assert direct == preview
        assert records.list("workspace_ask_receipts") == ()
    else:
        source = preview["sources"][0]
        assert source["conditions"] == [condition]
        assert text_tokens(source["excerpt"]) == WINDOW_TOKENS
        assert source["windows"] == [{"start": 0, "end": 22}]
        assert source["excerpt"].startswith("alphaomega")
        assert not preview["excluded_sources"]


def test_ask_budget_exclusions_are_authorized_relevant_and_survive_answer(tmp_path):
    http, records = client(tmp_path)
    too_long = ["甲" * 1800]
    excluded = _published_conditional_recognition(records, "alphaomega condition bound", too_long)
    _published_conditional_recognition(records, "alphaomega private condition", too_long, authorized=False)
    _published_conditional_recognition(records, "unrelated material", too_long)
    included = _published_conditional_recognition(records, "alphaomega useful evidence", ["prototype only"])
    request = {"project_id": "alpha", "question": "alphaomega"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    assert [entry["id"] for entry in preview["excluded_sources"]] == [excluded.id]
    assert [entry["id"] for entry in preview["sources"]] == [included.id]
    answer = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"]})
    assert answer.status_code == 200, answer.text
    assert answer.json()["excluded_sources"] == preview["excluded_sources"]
    source = answer.json()["sources"][0]
    assert source["conditions"] == ["prototype only"]
    assert source["excerpt"] == preview["sources"][0]["excerpt"]
    assert source["windows"] == preview["sources"][0]["windows"]
    assert "prototype only" not in json.dumps(records.list("workspace_ask_receipts")[0].payload)


def test_ask_rejects_condition_revision_changed_after_preview(tmp_path):
    http, records = client(tmp_path)
    item = _published_conditional_recognition(records, "alphaomega evidence", ["prototype only"])
    request = {"project_id": "alpha", "question": "alphaomega"}
    preview = http.post("/api/workspace/v1/ask/preview", json=request).json()
    RecognitionService(records).revise(scope=WorkScope("local-user", "alpha"), recognition_id=item.id,
                                       expected_revision=item.revision, content=item.content,
                                       conditions=["production only"])
    response = http.post("/api/workspace/v1/ask", json={**request, "preview_id": preview["preview_id"]})
    assert response.status_code == 409
    assert response.json()["detail"] == "source_changed_retry"
    assert records.list("workspace_ask_receipts") == ()


@pytest.mark.parametrize("content,remaining", [
    ("禁止alphaomega，因为会删除生产资料。", 10),
    ("不可以上线，因为尚未通过安全核验。", 4),
    ("alphaomega可以执行。例外：生产环境禁止执行。", 15),
])
def test_ask_never_truncates_recognition_meaning_to_make_room_for_conditions(tmp_path, content, remaining):
    from backend.memory_app.context_adapter import format_recognition_content
    http, records = client(tmp_path)
    item = _published_conditional_recognition(records, content, [])
    from backend.memory_app.v2.budget import WINDOW_TOKENS
    condition = _condition_for_token_size(item, WINDOW_TOKENS + remaining)
    item = RecognitionService(records).revise(scope=WorkScope("local-user", "alpha"), recognition_id=item.id,
        expected_revision=item.revision, content=content, conditions=[condition])
    response = http.post("/api/workspace/v1/ask/preview", json={
        "project_id": "alpha", "question": "alphaomega" if "alphaomega" in content else "可以上线"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data.get("no_match") is True
    assert data["excluded_sources"] == [{"type": "recognition", "id": item.id,
                                         "revision": item.revision, "reason": "recognition_evidence_budget_insufficient"}]


@pytest.mark.parametrize("size", [1200, 1201])
def test_ask_keeps_unconditional_recognition_whole_or_excludes_it(tmp_path, size):
    from backend.memory_app.context_adapter import format_recognition_content
    http, records = client(tmp_path)
    item = _published_conditional_recognition(records, "alphaomega", [])
    from backend.memory_app.v2.budget import text_tokens, WINDOW_TOKENS
    base = item.retrieval_projection()
    content = "alphaomega" + "甲" * (size - text_tokens(format_recognition_content(base)))
    assert text_tokens(format_recognition_content({**base, "content":content})) == size
    item = RecognitionService(records).revise(scope=WorkScope("local-user", "alpha"), recognition_id=item.id,
        expected_revision=item.revision, content=content)
    response = http.post("/api/workspace/v1/ask/preview", json={"project_id": "alpha", "question": "alphaomega"})
    assert response.status_code == 200, response.text
    data = response.json()
    if size == WINDOW_TOKENS:
        assert data["sources"][0]["excerpt"] == format_recognition_content(item.retrieval_projection())
        assert text_tokens(data["sources"][0]["excerpt"]) == size
        assert data["sources"][0]["windows"] == [{"start": 0, "end": len(content)}]
    else:
        assert data["no_match"] is True
        assert data["excluded_sources"][0]["id"] == item.id
        assert data["excluded_sources"][0]["reason"] == "recognition_evidence_budget_insufficient"
