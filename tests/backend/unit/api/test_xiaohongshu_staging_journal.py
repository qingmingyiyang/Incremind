from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.api.xiaohongshu_asset_materializer import XiaohongshuStagedAsset
from backend.api.xiaohongshu_staging_journal import XiaohongshuStagingJournal, XiaohongshuStagingJournalError
from core.job_runner.media_execution_receipt import media_job_uri_segment


JOB = "media_hands:xhs:journal"
MANIFEST = "crp://default/source-manifests/xhs-65f1234567890abc12345678"


def _assets(root: Path) -> tuple[XiaohongshuStagedAsset, ...]:
    path = root / ".rebuild-data" / "media-hands" / media_job_uri_segment(JOB) / "xiaohongshu" / "000-image-1.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
    return (
        XiaohongshuStagedAsset("image-1", 0, "image", "image/jpeg", str(path), 5),
        XiaohongshuStagedAsset("text-2", 1, "text", "text/plain", None, 0),
    )


def _commit(root: Path):
    journal = XiaohongshuStagingJournal(root)
    checkpoint = journal.commit(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", assets=_assets(root))
    return journal, checkpoint


def test_commit_is_restart_safe_idempotent_and_only_persists_safe_fields(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    replay = XiaohongshuStagingJournal(tmp_path).commit(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", assets=_assets(tmp_path),
    )
    restored = XiaohongshuStagingJournal(tmp_path).restore(
        job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1",
        receipt={"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash},
    )

    assert replay == checkpoint == journal.checkpoint(job_id=JOB)
    assert [item.staged_path for item in restored] == [str(tmp_path / ".rebuild-data" / "media-hands" / media_job_uri_segment(JOB) / "xiaohongshu" / "000-image-1.jpg"), None]
    raw = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-journals").glob("*.json")).read_text(encoding="utf-8")
    assert set(json.loads(raw)) == {"schema_version", "job_segment", "manifest_ref", "manifest_revision", "assets", "state_hash"}
    assert "http" not in raw and "token" not in raw and str(tmp_path) not in raw and "staged_path" not in raw


def test_restore_fails_closed_for_missing_tampered_or_escaped_assets(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    receipt = {"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash}
    staged = Path(_assets(tmp_path)[0].staged_path or "")
    staged.unlink()
    with pytest.raises(XiaohongshuStagingJournalError, match="unavailable"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", receipt=receipt)

    _assets(tmp_path)
    journal_path = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-journals").glob("*.json"))
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["assets"][0]["relative_path"] = "../../private.jpg"
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(XiaohongshuStagingJournalError):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", receipt=receipt)


def test_restore_rejects_receipt_or_journal_drift_and_symlink(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    with pytest.raises(XiaohongshuStagingJournalError):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", receipt={"output_ref": checkpoint.output_ref, "state_hash": "sha256:" + "0" * 64})

    journal_path = next((tmp_path / ".rebuild-data" / "media-hands" / "xiaohongshu-journals").glob("*.json"))
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["url"] = "https://private.example/?token=secret"
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(XiaohongshuStagingJournalError, match="fields"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", receipt={"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash})


def test_restore_rejects_staged_symlink(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    staged = Path(_assets(tmp_path)[0].staged_path or "")
    replacement = staged.with_name("actual.jpg")
    staged.replace(replacement)
    try:
        staged.symlink_to(replacement)
    except OSError as error:  # pragma: no cover - Windows policy may deny links
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(XiaohongshuStagingJournalError, match="symlink"):
        journal.restore(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v1", receipt={"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash})


def test_commit_rejects_content_drift(tmp_path: Path) -> None:
    journal, _checkpoint = _commit(tmp_path)
    with pytest.raises(XiaohongshuStagingJournalError, match="drifted"):
        journal.commit(job_id=JOB, manifest_ref=MANIFEST, manifest_revision="xhs-manifest-v2", assets=_assets(tmp_path))


def test_restore_rejects_same_size_content_and_manifest_binding_drift(tmp_path: Path) -> None:
    journal, checkpoint = _commit(tmp_path)
    receipt = {"output_ref": checkpoint.output_ref, "state_hash": checkpoint.state_hash}
    staged = Path(_assets(tmp_path)[0].staged_path or "")
    staged.write_bytes(b"other")
    with pytest.raises(XiaohongshuStagingJournalError, match="content drifted"):
        journal.restore(
            job_id=JOB, manifest_ref=MANIFEST,
            manifest_revision="xhs-manifest-v1", receipt=receipt,
        )

    staged.write_bytes(b"image")
    with pytest.raises(XiaohongshuStagingJournalError, match="manifest binding drifted"):
        journal.restore(
            job_id=JOB, manifest_ref=MANIFEST,
            manifest_revision="xhs-manifest-v2", receipt=receipt,
        )
