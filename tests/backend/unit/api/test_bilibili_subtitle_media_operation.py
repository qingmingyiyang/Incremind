from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from backend.api.bilibili_subtitle_media_operation import (
    BilibiliSubtitleMediaOperationError,
    BilibiliSubtitleMediaOperationProvider,
    DocumentBackedCanonicalMediaOutputVerifier,
)
from backend.api.governed_local_asr import GovernedLocalAsrRunner
from backend.security import DownloadedBinary
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.media_hands import MediaOperationRequest, SourcePermissionSnapshot
from core.source_processing import SourceManifestArtifactRepository, SourceManifestCodec
from core.storage_provider import JsonObjectStore


BVID = "BV1xx411c7mD"


class _TextNetwork:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.responses[url]


def _manifest():
    return SourceManifestCodec.decode(
        {
            "schema_version": "1.0.0",
            "source_id": "bili-source",
            "source_ref": "crp://default/sources/bili-source",
            "platform": "bilibili",
            "input_identity": f"https://www.bilibili.com/video/{BVID}/",
            "resolver_revision": "bilibili-view-api-v1",
            "normalizer_revision": "bilibili-manifest-v1-permission-r1",
            "content_kind": "video",
            "body": None,
            "metadata": {"bvid": BVID, "cid": 101, "title": "字幕视频", "duration_seconds": 5},
            "permission": {
                "decision": "granted",
                "evidence_refs": [
                    "crp://default/evidence/projects/project-1/bili-metadata-r1",
                    "crp://default/source-permissions/projects/project-1/bili-grant/r1",
                ],
            },
            "provenance_refs": ["crp://default/evidence/projects/project-1/bili-metadata-r1"],
            "assets": [
                {
                    "asset_id": "video-1", "ordinal": 0, "kind": "video", "media_type": None,
                    "role": "primary", "locator": f"https://www.bilibili.com/video/{BVID}/",
                    "source_ref": "crp://default/sources/bili-source/assets/video-1",
                    "relations": [], "evidence_refs": ["crp://default/evidence/projects/project-1/bili-metadata-r1"],
                }
            ],
        }
    )


def _request(
    artifact, *, download_budget: int = 1_000_000, wall_budget: int = 10_000,
    asr_budget: int = 0,
):
    return MediaOperationRequest(
        job_id="media_hands:bili-source:analyze_source",
        source_id="bili-source",
        operation="analyze_source",
        manifest_ref=artifact.public_ref,
        manifest_revision=artifact.revision,
        checkpoint=None,
        budget={
            "max_download_bytes": download_budget,
            "max_media_cpu_ms": 0,
            "max_asr_audio_ms": asr_budget,
            "max_vision_frames": 0,
            "max_model_input_tokens": 0,
            "max_model_output_tokens": 0,
            "max_wall_ms": wall_budget,
        },
        permission_snapshot=SourcePermissionSnapshot(
            project_id="project-1", manifest_ref=artifact.public_ref,
            manifest_revision=artifact.revision,
            grant_ref="crp://default/source-permissions/projects/project-1/bili-grant",
            grant_revision="r1", revocation_generation=0,
        ),
    )


def _parts(tmp_path: Path, responses: dict[str, str]):
    root = tmp_path.resolve()
    store = JsonObjectStore(root / ".rebuild-data", namespace_id="default")
    artifacts = SourceManifestArtifactRepository(store, namespace_id="default")
    artifact = artifacts.put(project_id="project-1", manifest_id="bili-subtitle-r1", manifest=_manifest())
    documents = AggregateRepositoryFactory(
        runtime_root=root, namespace_id="default", json_store=store
    ).document_repository()
    return artifacts, artifact, documents, _TextNetwork(responses)


def _factory(network):
    return lambda max_bytes, timeout, control: network


def _responses(*, subtitle: bool = True):
    catalog = f"https://api.bilibili.com/x/player/v2?bvid={BVID}&cid=101"
    subtitle_url = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/zh.json"
    values = {catalog: json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}})}
    if subtitle:
        values[catalog] = json.dumps({"code": 0, "data": {"subtitle": {"subtitles": [{"lan": "zh-Hans", "subtitle_url": subtitle_url}]}}})
        values[subtitle_url] = json.dumps({"body": [
            {"from": 0, "to": 1.25, "content": "第一句"},
            {"from": 1.25, "to": 3.0, "content": "第二句"},
        ]})
    return values


def test_official_subtitle_provider_writes_one_replayable_canonical_document(tmp_path: Path) -> None:
    artifacts, artifact, documents, network = _parts(tmp_path, _responses())
    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts=artifacts, documents=documents, network_factory=_factory(network),
        now=lambda: "2026-08-25T01:00:00Z", monotonic=lambda: 0.0,
    )
    request = _request(artifact)

    first = provider.execute(request)
    second = provider.execute(request)

    assert first.output == second.output
    assert first.output["kind"] == "document"
    assert first.consumed["max_download_bytes"] > 0
    assert first.consumed["max_wall_ms"] == 0
    assert first.checkpoint["checkpoint_uri"].endswith("/bilibili-official-subtitle-r1")
    document = documents.read(str(first.output["object_id"]))
    assert document is not None and document["type"] == "media_transcript"
    assert document["revision"] == 1 and len(documents.list()) == 1
    assert "## 稳定切片" in documents.markdown(str(first.output["object_id"]), revision=1)
    DocumentBackedCanonicalMediaOutputVerifier(documents).assert_output_committed(
        output=first.output, request=request
    )


def test_verifier_rejects_document_when_request_source_snapshot_drifts(tmp_path: Path) -> None:
    artifacts, artifact, documents, network = _parts(tmp_path, _responses())
    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts, documents, _factory(network), now=lambda: "2026-08-25T01:00:00Z"
    )
    request = _request(artifact)
    receipt = provider.execute(request)
    drifted = replace(request, source_id="another-source")
    with pytest.raises(BilibiliSubtitleMediaOperationError, match="source_snapshot"):
        DocumentBackedCanonicalMediaOutputVerifier(documents).assert_output_committed(
            output=receipt.output, request=drifted
        )


def test_no_official_subtitle_reports_unavailable_asr_fallback_without_document(tmp_path: Path) -> None:
    artifacts, artifact, documents, network = _parts(tmp_path, _responses(subtitle=False))
    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts, documents, _factory(network), now=lambda: "2026-08-25T01:00:00Z"
    )
    with pytest.raises(BilibiliSubtitleMediaOperationError, match="asr_fallback_unavailable:official_subtitle_unavailable"):
        provider.execute(_request(artifact))
    assert documents.list() == ()


def test_download_budget_rejects_before_document_write(tmp_path: Path) -> None:
    artifacts, artifact, documents, network = _parts(tmp_path, _responses())
    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts, documents, _factory(network), now=lambda: "2026-08-25T01:00:00Z"
    )
    with pytest.raises(BilibiliSubtitleMediaOperationError, match="subtitle_download_budget_exceeded"):
        provider.execute(_request(artifact, download_budget=1))
    assert documents.list() == ()


def test_each_network_request_uses_remaining_job_bytes_and_wall_deadline(tmp_path: Path) -> None:
    artifacts, artifact, documents, network = _parts(tmp_path, _responses())
    calls = []

    def factory(max_bytes, timeout, control):
        calls.append((max_bytes, timeout, control))
        return network

    ticks = iter((0.0, 1.0, 2.0, 2.0, 2.0))
    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts, documents, factory,
        now=lambda: "2026-08-25T01:00:00Z", monotonic=lambda: next(ticks),
    )
    budget = 1_000_000
    provider.execute(_request(artifact, download_budget=budget, wall_budget=10_000))

    first_size = len(next(iter(_responses().values())).encode("utf-8"))
    assert calls[0][:2] == (budget, 9.0)
    assert calls[1][0] == budget - first_size
    assert calls[1][1] == 8.0


def test_execution_control_interrupts_network_before_document_write(tmp_path: Path) -> None:
    artifacts, artifact, documents, network = _parts(tmp_path, _responses())

    class Cancelled(RuntimeError):
        pass

    class Control:
        calls = 0

        def checkpoint(self):
            self.calls += 1
            if self.calls == 2:
                raise Cancelled("cancelled during network")

    def factory(max_bytes, timeout, control):
        class ControlledNetwork:
            def fetch_text(self, url):
                assert control is not None
                control()
                return network.fetch_text(url)
        return ControlledNetwork()

    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts, documents, factory, now=lambda: "2026-08-25T01:00:00Z"
    )
    with pytest.raises(Cancelled, match="cancelled during network"):
        provider.execute(replace(_request(artifact), control=Control()))
    assert network.calls == []
    assert documents.list() == ()


def test_missing_official_subtitle_runs_governed_audio_and_local_asr_fallback(
    tmp_path: Path,
) -> None:
    responses = _responses(subtitle=False)
    playurl = f"https://api.bilibili.com/x/player/playurl?bvid={BVID}&cid=101&fnval=16&fnver=0&fourk=0"
    responses[playurl] = json.dumps({"code": 0, "data": {"dash": {"audio": [{
        "baseUrl": "https://cdn.bilivideo.com/audio.m4s",
        "bandwidth": 64000,
        "codecs": "mp4a.40.2",
    }]}}})
    artifacts, artifact, documents, network = _parts(tmp_path, responses)
    app_root = tmp_path / "app"
    staging = app_root / ".rebuild-data" / "media-hands"
    model = app_root / "data" / "models" / "faster-whisper" / "small"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
    from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest
    manager = FasterWhisperModelManager(model.parent)
    write_downloaded_model_manifest(model, manager.download_spec("small"))

    class Binary:
        calls = 0

        def download(self, url, **kwargs):
            self.calls += 1
            path = staging / kwargs["relative_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio")
            return DownloadedBinary(path, 5, "audio/mp4")

        def staged_path(self, relative_path):
            return staging / relative_path

    class Probe:
        def duration_ms(self, path, *, control_check=None):
            return 2500

    asr_calls = []

    def asr_command(argv, env, timeout, control):
        asr_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            "language": "zh",
            "segments": [
                {"start_seconds": 0, "end_seconds": 1.2, "text": "本地第一句"},
                {"start_seconds": 1.2, "end_seconds": 2.5, "text": "本地第二句"},
            ],
        }), "")

    binary = Binary()
    provider = BilibiliSubtitleMediaOperationProvider(
        artifacts, documents, _factory(network),
        now=lambda: "2026-08-25T01:00:00Z",
        binary_network=binary,
        local_asr=GovernedLocalAsrRunner(
            app_root, staging, "small", Probe(), command_runner=asr_command
        ),
    )
    class Control:
        def __init__(self):
            self.receipts = {}
            self.inputs = {}

        def checkpoint(self):
            return None

        def begin_recipe_step(self, step_name, input_state_hash):
            previous = self.inputs.setdefault(step_name, input_state_hash)
            assert previous == input_state_hash
            return self.receipts.get(step_name)

        def complete_recipe_step(
            self, step_name, *, output_ref, output_state_hash, consumed
        ):
            self.receipts[step_name] = {
                "step_name": step_name, "output_ref": output_ref,
                "output_state_hash": output_state_hash, "consumed": dict(consumed),
            }

        def mark_recipe_step_unknown(self, step_name):
            raise AssertionError(f"unexpected unknown step: {step_name}")

    control = Control()
    request = replace(_request(artifact, asr_budget=3000), control=control)
    receipt = provider.execute(request)
    replay = provider.execute(request)

    assert receipt.consumed["max_asr_audio_ms"] == 2500
    assert receipt.consumed["max_download_bytes"] > 5
    markdown = documents.markdown(str(receipt.output["object_id"]), revision=1)
    assert "Transcript source: local_asr (zh)" in markdown
    assert "本地第一句" in markdown
    assert replay.output == receipt.output
    assert binary.calls == 1 and len(asr_calls) == 1
    assert network.calls.count(playurl) == 1
    assert set(control.receipts) == {"fetch_audio", "probe_audio", "transcribe_document"}
