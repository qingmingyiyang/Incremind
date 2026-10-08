from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.api.xiaohongshu_asset_analysis_journal import (
    XiaohongshuAssetAnalysisJournal,
    XiaohongshuAssetAnalysisJournalError,
)


JOB = "media_hands:xhs:mixed-analysis"
MANIFEST = "crp://default/source-manifests/xhs-65f1234567890abc12345678"
CONSUMED = {"media_cpu_milliseconds": 12, "audio_milliseconds": 20, "vision_frames": 2, "wall_milliseconds": 30}


def _commit(journal: XiaohongshuAssetAnalysisJournal, *, asset_id: str = "video-1"):
    return journal.commit(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1",
        asset_id=asset_id, ordinal=1, kind="video", provider_revisions={"asr": "asr-r1", "ocr": "ocr-r1"},
        result={"transcript_segments": [{"start_ms": 0, "end_ms": 20, "text": "字幕"}], "frame_ocr": [{"ordinal": 0, "text": "画面"}]},
        consumed=CONSUMED,
    )


def test_commit_restore_is_asset_scoped_atomic_and_receipt_bound(tmp_path: Path) -> None:
    journal = XiaohongshuAssetAnalysisJournal(tmp_path)
    checkpoint = _commit(journal)
    replay = _commit(XiaohongshuAssetAnalysisJournal(tmp_path))
    restored = journal.restore(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1",
        asset_id="video-1", ordinal=1, kind="video", provider_revisions={"asr": "asr-r1", "ocr": "ocr-r1"},
        receipt={"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash},
    )
    assert replay == checkpoint == journal.checkpoint(job_id=JOB, asset_id="video-1")
    assert restored.result["transcript_segments"] == [{"start_ms": 0, "end_ms": 20, "text": "字幕"}]
    assert restored.consumed == CONSUMED
    raw = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-asset-analysis-journals").glob("**/*.json")).read_text(encoding="utf-8")
    assert set(json.loads(raw)) == {"schema_version", "job_segment", "asset_segment", "manifest_ref", "manifest_revision", "source_id", "asset_id", "ordinal", "kind", "provider_revisions", "result", "consumed", "state_hash"}
    assert str(tmp_path) not in raw and "http" not in raw and "token" not in raw


def test_image_and_text_have_typed_provider_and_result_contracts(tmp_path: Path) -> None:
    journal = XiaohongshuAssetAnalysisJournal(tmp_path)
    image = journal.commit(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1",
        asset_id="image-1", ordinal=0, kind="image", provider_revisions={"ocr": "ocr-r1"},
        result={"ocr_text": "图片文字"}, consumed={"media_cpu_milliseconds": 4, "audio_milliseconds": 0, "vision_frames": 1, "wall_milliseconds": 4},
    )
    text = journal.commit(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1",
        asset_id="text-1", ordinal=2, kind="text", provider_revisions={}, result={"body": "正文"},
        consumed={"media_cpu_milliseconds": 0, "audio_milliseconds": 0, "vision_frames": 0, "wall_milliseconds": 0},
    )
    assert image.output_ref != text.output_ref
    with pytest.raises(XiaohongshuAssetAnalysisJournalError, match="providers"):
        journal.commit(
            job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1",
            asset_id="image-2", ordinal=3, kind="image", provider_revisions={}, result={"ocr_text": "图片文字"},
            consumed={"media_cpu_milliseconds": 0, "audio_milliseconds": 0, "vision_frames": 0, "wall_milliseconds": 0},
        )


def test_restore_fails_closed_for_binding_receipt_schema_and_text_drift(tmp_path: Path) -> None:
    journal = XiaohongshuAssetAnalysisJournal(tmp_path)
    checkpoint = _commit(journal)
    receipt = {"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash}
    with pytest.raises(XiaohongshuAssetAnalysisJournalError, match="binding drifted"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v2", source_id="source-1", asset_id="video-1", ordinal=1, kind="video", provider_revisions={"asr": "asr-r1", "ocr": "ocr-r1"}, receipt=receipt)
    with pytest.raises(XiaohongshuAssetAnalysisJournalError, match="receipt state drifted"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", asset_id="video-1", ordinal=1, kind="video", provider_revisions={"asr": "asr-r1", "ocr": "ocr-r1"}, receipt={"output_ref": checkpoint.output_ref, "state_hash": "sha256:" + "0" * 64})
    path = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-asset-analysis-journals").glob("**/*.json"))
    payload = json.loads(path.read_text(encoding="utf-8")); payload["result"]["frame_ocr"][0]["text"] = "https://secret.example/token"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(XiaohongshuAssetAnalysisJournalError):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", asset_id="video-1", ordinal=1, kind="video", provider_revisions={"asr": "asr-r1", "ocr": "ocr-r1"}, receipt=receipt)
