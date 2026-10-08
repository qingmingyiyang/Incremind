from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.video_intake.service as video_intake_service
from backend.video_intake.models import LibraryRecord, RecordDetail
from backend.video_summary.domain.models import SummaryDocument, Transcript, TranscriptSegment


class _QuestionGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def acomplete_text(self, _messages, *, wire_attempt_sink=None, **_kwargs):
        assert wire_attempt_sink is not None
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        await asyncio.sleep(0)
        if self.fail:
            attempt.failed_transport(error_code="ai.connection_error")
            raise RuntimeError("question failed")
        attempt.succeeded(
            usage={"prompt_tokens": 8, "completion_tokens": 2},
            cache_observation={"cache_read_input_tokens": 3},
        )
        return "答案 [00:00]"


class _Gateway:
    pass


class _Enhancer:
    received_sink = None

    def __init__(self, _gateway) -> None:
        pass

    async def enhance(self, _video, transcript, *, wire_attempt_sink=None):
        type(self).received_sink = wire_attempt_sink
        assert wire_attempt_sink is not None
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        attempt.succeeded(usage={"prompt_tokens": 5, "completion_tokens": 1})
        return transcript


class _FailingEnhancer(_Enhancer):
    async def enhance(self, _video, _transcript, *, wire_attempt_sink=None):
        type(self).received_sink = wire_attempt_sink
        assert wire_attempt_sink is not None
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        attempt.failed_transport(error_code="ai.connection_error")
        raise RuntimeError("enhance failed")


class _Summarizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.received_sink = None

    async def summarize(self, _video, _transcript, *, wire_attempt_sink=None):
        self.received_sink = wire_attempt_sink
        assert wire_attempt_sink is not None
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        if self.fail:
            attempt.failed_transport(error_code="ai.connection_error")
            raise RuntimeError("summary failed")
        attempt.succeeded(usage={"prompt_tokens": 11, "completion_tokens": 4})
        return SummaryDocument(markdown="# Summary", summary_data={"title": "Summary"})


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        openai=SimpleNamespace(
            base_url="https://private-endpoint.example.invalid/v1",
            provider="test",
            model="model-1",
            api_key="super-secret-api-key",
        ),
    )


@pytest.mark.asyncio
async def test_question_persists_isolated_non_authoritative_attempt_observation(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    record = LibraryRecord(id="record-1", bvid="BV1", title="视频", source_url="https://example.invalid/video")
    record_dir = tmp_path / "record-1"
    gateway = _QuestionGateway()
    monkeypatch.setattr(service, "detail", lambda _record_id: RecordDetail(record=record, transcript={"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "本地事实"}]}))
    monkeypatch.setattr(service.storage, "record_dir", lambda _record: record_dir)
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_litellm_completion_gateway", lambda *_args, **_kwargs: gateway)

    answer, _references = await service.ask(record.id, "这个视频说了什么？")

    assert answer == "答案 [00:00]"
    paths = list((record_dir / "data" / "wire-attempts").glob("*/question.json"))
    assert len(paths) == 1
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "non_authoritative_wire_attempt_observation"
    assert payload["stage"] == "question"
    assert payload["records"][0]["model_identity"] == "test:model-1"
    assert payload["records"][0]["cache_observation"] == {"cache_read_input_tokens": 3}
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "这个视频说了什么" not in serialized
    assert str(tmp_path) not in serialized
    assert "private-endpoint.example.invalid" not in serialized
    assert "super-secret-api-key" not in serialized


@pytest.mark.asyncio
async def test_concurrent_questions_keep_request_and_record_ledgers_isolated(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    records = {
        record_id: LibraryRecord(
            id=record_id,
            bvid=f"BV-{record_id}",
            title=record_id,
            source_url=f"https://example.invalid/{record_id}",
        )
        for record_id in ("record-a", "record-b")
    }
    monkeypatch.setattr(
        service,
        "detail",
        lambda record_id: RecordDetail(
            record=records[record_id],
            transcript={"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": record_id}]},
        ),
    )
    monkeypatch.setattr(service.storage, "record_dir", lambda record: tmp_path / record.id)
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_litellm_completion_gateway", lambda *_args, **_kwargs: _QuestionGateway())

    await asyncio.gather(
        service.ask("record-a", "A 说了什么？"),
        service.ask("record-b", "B 说了什么？"),
    )

    payloads = []
    for record_id in records:
        paths = list((tmp_path / record_id / "data" / "wire-attempts").glob("*/question.json"))
        assert len(paths) == 1
        payloads.append(json.loads(paths[0].read_text(encoding="utf-8")))
    assert len({payload["request_id"] for payload in payloads}) == 2
    assert all(len(payload["records"]) == 1 for payload in payloads)
    assert all(payload["records"][0]["request_id"] == payload["request_id"] for payload in payloads)


@pytest.mark.asyncio
async def test_empty_recorder_does_not_create_observation_artifact(tmp_path) -> None:
    recorder = video_intake_service._new_wire_attempt_recorder(
        stage="summary",
        model_identity="test:model-1",
    )

    await video_intake_service._persist_wire_attempts_best_effort(
        data_dir=tmp_path / "data",
        recorders=(recorder,),
    )

    assert not (tmp_path / "data" / "wire-attempts").exists()


@pytest.mark.asyncio
async def test_question_keeps_answer_when_observation_write_fails(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    record = LibraryRecord(id="record-1", bvid="BV1", title="视频", source_url="https://example.invalid/video")
    monkeypatch.setattr(service, "detail", lambda _record_id: RecordDetail(record=record, transcript={"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "本地事实"}]}))
    monkeypatch.setattr(service.storage, "record_dir", lambda _record: tmp_path / "record-1")
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_litellm_completion_gateway", lambda *_args, **_kwargs: _QuestionGateway())
    monkeypatch.setattr(video_intake_service, "atomic_write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")))

    answer, _references = await service.ask(record.id, "事实是什么？")

    assert answer == "答案 [00:00]"


@pytest.mark.asyncio
async def test_question_failure_still_persists_terminal_attempt(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    record = LibraryRecord(id="record-1", bvid="BV1", title="视频", source_url="https://example.invalid/video")
    record_dir = tmp_path / "record-1"
    monkeypatch.setattr(service, "detail", lambda _record_id: RecordDetail(record=record, transcript={"segments": [{"start_seconds": 0.0, "end_seconds": 1.0, "text": "本地事实"}]}))
    monkeypatch.setattr(service.storage, "record_dir", lambda _record: record_dir)
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_litellm_completion_gateway", lambda *_args, **_kwargs: _QuestionGateway(fail=True))

    with pytest.raises(RuntimeError, match="question failed"):
        await service.ask(record.id, "事实是什么？")

    paths = list((record_dir / "data" / "wire-attempts").glob("*/question.json"))
    assert len(paths) == 1
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert payload["records"][0]["status"] == "failed_transport"
    assert payload["records"][0]["error_code"] == "ai.connection_error"


@pytest.mark.asyncio
async def test_subtitle_enhance_and_summary_use_distinct_sinks_and_persist_on_failure(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    data_dir = tmp_path / "record" / "data"
    data_dir.mkdir(parents=True)
    subtitle_path = tmp_path / "record" / "subtitle.srt"
    subtitle_path.write_text("unused", encoding="utf-8")
    transcript = Transcript(language="zh", segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="字幕")])
    summarizer = _Summarizer(fail=True)
    runtime = SimpleNamespace(gateway=_Gateway(), summarizer=summarizer)
    record = LibraryRecord(id="record-1", bvid="BV1", title="视频", source_url="https://example.invalid/video", duration_seconds=1.0)
    monkeypatch.setattr(video_intake_service, "parse_subtitle", lambda _path: transcript)
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_video_summary_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(video_intake_service, "LiteLLMTranscriptEnhancer", _Enhancer)
    monkeypatch.setattr(service.storage, "save_record", lambda _record: None)

    with pytest.raises(RuntimeError, match="summary failed"):
        await service._generate_from_subtitle(
            record,
            tmp_path / "record" / "media.mp4",
            data_dir,
            subtitle_path,
            {},
            enhance=True,
            report=lambda *_args: None,
        )

    assert _Enhancer.received_sink is not None
    assert summarizer.received_sink is not None
    assert _Enhancer.received_sink is not summarizer.received_sink
    paths = list((data_dir / "wire-attempts").glob("*/*.json"))
    assert {path.name for path in paths} == {"transcript_enhance.json", "summary.json"}
    payloads = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in paths}
    assert payloads["transcript_enhance.json"]["records"][0]["status"] == "succeeded"
    assert payloads["summary.json"]["records"][0]["status"] == "failed_transport"
    request_ids = {payload["request_id"] for payload in payloads.values()}
    assert len(request_ids) == 1
    assert all("media.mp4" not in json.dumps(payload) for payload in payloads.values())


@pytest.mark.asyncio
async def test_subtitle_success_persists_enhance_and_summary_observations(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    data_dir = tmp_path / "record" / "data"
    data_dir.mkdir(parents=True)
    transcript = Transcript(language="zh", segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="字幕")])
    summarizer = _Summarizer()
    runtime = SimpleNamespace(gateway=_Gateway(), summarizer=summarizer)
    record = LibraryRecord(id="record-1", bvid="BV1", title="视频", source_url="https://example.invalid/video", duration_seconds=1.0)
    monkeypatch.setattr(video_intake_service, "parse_subtitle", lambda _path: transcript)
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_video_summary_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(video_intake_service, "LiteLLMTranscriptEnhancer", _Enhancer)
    monkeypatch.setattr(service.storage, "save_record", lambda _record: None)

    await service._generate_from_subtitle(
        record,
        tmp_path / "record" / "media.mp4",
        data_dir,
        tmp_path / "record" / "subtitle.srt",
        {},
        enhance=True,
        report=lambda *_args: None,
    )

    payloads = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (data_dir / "wire-attempts").glob("*/*.json")
    }
    assert {payload["records"][0]["status"] for payload in payloads.values()} == {"succeeded"}
    assert {payload["stage"] for payload in payloads.values()} == {"transcript_enhance", "summary"}


@pytest.mark.asyncio
async def test_subtitle_enhance_failure_persists_its_terminal_attempt(tmp_path, monkeypatch) -> None:
    service = video_intake_service.IntakeService(tmp_path)
    data_dir = tmp_path / "record" / "data"
    data_dir.mkdir(parents=True)
    transcript = Transcript(language="zh", segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="字幕")])
    runtime = SimpleNamespace(gateway=_Gateway(), summarizer=_Summarizer())
    record = LibraryRecord(id="record-1", bvid="BV1", title="视频", source_url="https://example.invalid/video", duration_seconds=1.0)
    monkeypatch.setattr(video_intake_service, "parse_subtitle", lambda _path: transcript)
    monkeypatch.setattr(video_intake_service, "load_settings", lambda *_args, **_kwargs: _settings())
    monkeypatch.setattr(video_intake_service, "build_active_provider_egress_guard", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(video_intake_service, "build_video_summary_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(video_intake_service, "LiteLLMTranscriptEnhancer", _FailingEnhancer)

    with pytest.raises(RuntimeError, match="enhance failed"):
        await service._generate_from_subtitle(
            record,
            tmp_path / "record" / "media.mp4",
            data_dir,
            tmp_path / "record" / "subtitle.srt",
            {},
            enhance=True,
            report=lambda *_args: None,
        )

    payload = json.loads(next((data_dir / "wire-attempts").glob("*/transcript_enhance.json")).read_text(encoding="utf-8"))
    assert payload["records"][0]["status"] == "failed_transport"
