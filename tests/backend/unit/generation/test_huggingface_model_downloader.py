from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import sys
import types

import pytest
import backend.video_summary.infrastructure.huggingface_model_downloader as downloader_module

from backend.video_summary.infrastructure.huggingface_model_downloader import (
    HuggingFaceDownloadSpec,
    HuggingFaceModelDownloader,
    MODEL_ARTIFACT_MANIFEST,
    verify_downloaded_model,
)
from backend.video_summary.infrastructure.faster_whisper_models import (
    FASTER_WHISPER_MODEL_SOURCES,
    FasterWhisperModelManager,
)


REVISION = "536b0662742c02347bc0e980a01041f333bce120"


class Reporter:
    def update(self, *args):
        return None

    def completed(self, *args):
        return None

    def raise_if_cancelled(self):
        return None


class FixtureDownloader(HuggingFaceModelDownloader):
    def __init__(self, *, extra: bool = False) -> None:
        self.extra = extra
        self.observed_revision = None

    def _snapshot_download(self, *, spec, temp_dir):
        self.observed_revision = spec.revision
        (temp_dir / "model.bin").write_bytes(b"model-v1")
        (temp_dir / "config.json").write_text("{}", encoding="utf-8")
        cache = temp_dir / ".cache" / "huggingface"
        cache.mkdir(parents=True)
        (cache / "download.json").write_text("private cache metadata", encoding="utf-8")
        if self.extra:
            (temp_dir / "unreviewed.exe").write_bytes(b"no")


def spec(tmp_path: Path, *, revision: str = REVISION) -> HuggingFaceDownloadSpec:
    return HuggingFaceDownloadSpec(
        repo_id="Systran/faster-whisper-small",
        revision=revision,
        target_dir=tmp_path / "small",
        required_files=("model.bin", "config.json"),
        required_file_patterns=(),
        allow_patterns=("model.bin", "config.json"),
    )


def test_download_pins_revision_and_seals_path_safe_artifact_manifest(tmp_path: Path) -> None:
    downloader = FixtureDownloader()
    target = downloader.download(spec(tmp_path), Reporter())

    assert downloader.observed_revision == REVISION
    assert not (target / ".cache").exists()
    manifest = json.loads((target / MODEL_ARTIFACT_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["repo_id"] == "Systran/faster-whisper-small"
    assert manifest["revision"] == REVISION
    assert manifest["endpoint"] == "https://huggingface.co"
    assert [item["path"] for item in manifest["files"]] == ["config.json", "model.bin"]
    assert all(item["size"] > 0 and len(item["sha256"]) == 64 for item in manifest["files"])
    verify_downloaded_model(target, spec(tmp_path))


def test_verifier_rejects_file_and_revision_drift(tmp_path: Path) -> None:
    target = FixtureDownloader().download(spec(tmp_path), Reporter())
    (target / "model.bin").write_bytes(b"drift")
    with pytest.raises(RuntimeError, match="制品与受治理清单不匹配"):
        verify_downloaded_model(target, spec(tmp_path))
    with pytest.raises(RuntimeError, match="身份不匹配"):
        verify_downloaded_model(target, spec(tmp_path, revision="0" * 40))


def test_unpinned_revision_and_unreviewed_file_fail_closed(tmp_path: Path) -> None:
    invalid = FixtureDownloader()
    with pytest.raises(RuntimeError, match="不可变revision"):
        invalid.download(spec(tmp_path, revision="main"), Reporter())
    assert invalid.observed_revision is None
    malicious = FixtureDownloader()
    with pytest.raises(RuntimeError, match="不可变revision"):
        malicious.download(replace(spec(tmp_path), endpoint="https://models.invalid"), Reporter())
    assert malicious.observed_revision is None
    with pytest.raises(RuntimeError, match="未审核的额外文件"):
        FixtureDownloader(extra=True).download(spec(tmp_path), Reporter())
    assert not spec(tmp_path).target_dir.exists()


def test_manifest_link_and_nested_directory_fail_closed(tmp_path: Path, monkeypatch) -> None:
    target = FixtureDownloader().download(spec(tmp_path), Reporter())
    nested = target / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeError, match="只能包含普通文件"):
        verify_downloaded_model(target, spec(tmp_path))
    nested.rmdir()

    manifest = target / MODEL_ARTIFACT_MANIFEST
    original = downloader_module._is_linklike
    monkeypatch.setattr(
        downloader_module,
        "_is_linklike",
        lambda path: path == manifest or original(path),
    )
    with pytest.raises(RuntimeError, match="清单必须是普通文件"):
        verify_downloaded_model(target, spec(tmp_path))


def test_snapshot_download_receives_exact_revision_and_allowlist(tmp_path: Path, monkeypatch) -> None:
    observed = {}
    module = types.ModuleType("huggingface_hub")
    module.snapshot_download = lambda **kwargs: observed.update(kwargs)
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    value = spec(tmp_path)
    HuggingFaceModelDownloader()._snapshot_download(spec=value, temp_dir=tmp_path / "download")

    assert observed == {
        "repo_id": value.repo_id,
        "revision": REVISION,
        "local_dir": tmp_path / "download",
        "max_workers": 4,
        "token": False,
        "allow_patterns": ("model.bin", "config.json"),
        "endpoint": "https://huggingface.co",
    }


def test_faster_whisper_catalog_is_fully_pinned_and_legacy_directory_is_untrusted(tmp_path: Path) -> None:
    assert set(FASTER_WHISPER_MODEL_SOURCES) == {"small", "medium", "large-v3", "large-v3-turbo"}
    assert all(len(revision) == 40 for _repo, revision in FASTER_WHISPER_MODEL_SOURCES.values())
    manager = FasterWhisperModelManager(tmp_path / "models")
    legacy = manager.resolve_model_dir("small")
    legacy.mkdir(parents=True)
    (legacy / "model.bin").write_bytes(b"legacy")
    (legacy / "config.json").write_text("{}", encoding="utf-8")

    assert manager.is_downloaded("small") is False
    assert manager.resolve_model_source("small") == "small"


def test_model_manager_download_uses_pinned_source_and_becomes_ready(tmp_path: Path) -> None:
    downloader = FixtureDownloader()
    manager = FasterWhisperModelManager(tmp_path / "models", downloader=downloader)

    target = manager.download("small")

    assert target == tmp_path / "models" / "small"
    assert downloader.observed_revision == FASTER_WHISPER_MODEL_SOURCES["small"][1]
    assert manager.is_downloaded("small") is True
