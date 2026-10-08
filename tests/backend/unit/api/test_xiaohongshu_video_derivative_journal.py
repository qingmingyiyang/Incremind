from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from backend.api.governed_staged_video import GovernedStagedVideoDerivative, GovernedStagedVideoOutcome
from backend.api.xiaohongshu_video_derivative_journal import XiaohongshuVideoDerivativeJournal, XiaohongshuVideoDerivativeJournalError
from core.job_runner.media_execution_receipt import media_job_uri_segment


JOB = "media_hands:xhs:video-journal"
MANIFEST = "crp://default/source-manifests/xhs-65f1234567890abc12345678"


def _outcome(root: Path) -> GovernedStagedVideoOutcome:
    base = root / ".rebuild-data" / "media-hands" / media_job_uri_segment(JOB) / "xiaohongshu" / "derivatives"
    base.mkdir(parents=True, exist_ok=True)
    audio = base / "audio-16k-mono.wav"; audio.write_bytes(b"wav")
    frames = []
    for ordinal in range(2):
        path = base / f"frame-{ordinal + 1:03d}.jpg"; path.write_bytes(f"frame{ordinal}".encode())
        frames.append(GovernedStagedVideoDerivative("frame", ordinal, "image/jpeg", str(path), path.stat().st_size))
    return GovernedStagedVideoOutcome(GovernedStagedVideoDerivative("audio", 0, "audio/wav", str(audio), audio.stat().st_size), tuple(frames), 120)


def _commit(root: Path):
    journal = XiaohongshuVideoDerivativeJournal(root)
    checkpoint = journal.commit(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", outcome=_outcome(root))
    return journal, checkpoint


def test_commit_restore_is_atomic_safe_and_idempotent(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    replay = XiaohongshuVideoDerivativeJournal(tmp_path).commit(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", outcome=_outcome(tmp_path))
    restored = XiaohongshuVideoDerivativeJournal(tmp_path).restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", receipt={"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash})
    assert replay == checkpoint == journal.checkpoint(job_id=JOB)
    assert restored.wall_ms == 120 and restored.audio.byte_count == 3 and [item.ordinal for item in restored.frames] == [0, 1]
    raw = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-video-derivative-journals").glob("**/*.json")).read_text(encoding="utf-8")
    assert set(json.loads(raw)) == {"schema_version", "job_segment", "asset_segment", "manifest_ref", "manifest_revision", "source_id", "source_asset_id", "wall_ms", "derivatives", "state_hash"}
    assert str(tmp_path) not in raw and "url" not in raw and "token" not in raw


def test_restore_fails_closed_for_binding_receipt_path_and_content_drift(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    receipt = {"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash}
    with pytest.raises(XiaohongshuVideoDerivativeJournalError, match="binding drifted"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v2", source_id="source-1", source_asset_id="video-1", receipt=receipt)
    with pytest.raises(XiaohongshuVideoDerivativeJournalError, match="receipt state drifted"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", receipt={"output_ref": checkpoint.output_ref, "state_hash": "sha256:" + "0" * 64})
    frame = Path(_outcome(tmp_path).frames[0].staged_path); frame.write_bytes(b"other0")
    with pytest.raises(XiaohongshuVideoDerivativeJournalError, match="content drifted"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", receipt=receipt)
    journal_path = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-video-derivative-journals").glob("**/*.json"))
    payload = json.loads(journal_path.read_text(encoding="utf-8")); payload["derivatives"][0]["relative_path"] = "../../secret.wav"
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(XiaohongshuVideoDerivativeJournalError):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", receipt=receipt)


def test_restore_rejects_derivative_symlink(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    audio = Path(_outcome(tmp_path).audio.staged_path)
    actual = audio.with_name("actual.wav"); audio.replace(actual)
    try:
        audio.symlink_to(actual)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(XiaohongshuVideoDerivativeJournalError, match="symlink"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-1", receipt={"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash})


def test_asset_scoped_records_cannot_overwrite_each_other(tmp_path: Path) -> None:
    journal, first = _commit(tmp_path)
    second = journal.commit(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1", source_asset_id="video-2", outcome=_outcome(tmp_path))
    assert first.output_ref != second.output_ref
    assert journal.checkpoint(job_id=JOB, source_asset_id="video-1") == first
    assert journal.checkpoint(job_id=JOB, source_asset_id="video-2") == second
    with pytest.raises(XiaohongshuVideoDerivativeJournalError, match="ambiguous"):
        journal.checkpoint(job_id=JOB)


def test_restore_accepts_pre_asset_scoped_single_video_checkpoint(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    current_path = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-video-derivative-journals").glob("**/*.json"))
    payload = json.loads(current_path.read_text(encoding="utf-8"))
    payload.pop("asset_segment")
    canonical = {key: value for key, value in payload.items() if key != "state_hash"}
    payload["state_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    legacy_path = current_path.parents[1] / f"{media_job_uri_segment(JOB)}.json"
    legacy_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    current_path.unlink()
    current_path.parent.rmdir()

    legacy = journal.checkpoint(job_id=JOB)
    assert legacy.output_ref.endswith("/video-derivatives/xiaohongshu")
    restored = journal.restore(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-v1", source_id="source-1",
        source_asset_id="video-1", receipt={"output_ref": legacy.output_ref, "state_hash": legacy.state_hash},
    )
    assert restored.wall_ms == 120
    assert checkpoint.output_ref != legacy.output_ref
