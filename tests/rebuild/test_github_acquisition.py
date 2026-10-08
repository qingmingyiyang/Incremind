from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from core.effect_log import EffectClass, EffectLog, EffectState
from core.plugin_host.github_acquisition import (
    EFFECT_KIND,
    GitHubAcquisitionError,
    GitHubAcquisitionStagingHandler,
    SafeZipExtractor,
    build_acquisition_intent,
    preview_github_source,
)


REVISION = "a" * 40


class _Downloader:
    def __init__(self, archive: Path) -> None:
        self.archive = archive
        self.calls: list[str] = []

    def download(self, url: str, destination: Path, *, max_bytes: int) -> Path:
        self.calls.append(url)
        shutil.copyfile(self.archive, destination)
        return destination


class _Store:
    def __init__(self) -> None:
        self.receipts: dict[str, str] = {}
        self.stored: list[tuple[str, dict[str, object], Path]] = []

    def probe(self, operation_id: str) -> str | None:
        return self.receipts.get(operation_id)

    def store(self, operation_id: str, descriptor: dict[str, object], tree: Path) -> str:
        self.stored.append((operation_id, dict(descriptor), tree))
        receipt = f"crp://plugin-github-receipts/{operation_id}"
        self.receipts[operation_id] = receipt
        return receipt


def _preview():
    return preview_github_source("https://github.com/hugohe3/ppt-master", resolver=lambda _: REVISION)


def _effect(tmp_path: Path):
    intent = build_acquisition_intent(
        _preview(), session_id="session", root_id="root", step_key="stage", gate_decision_id="gate:1",
        intent_ref="crp://plugin-intents/ppt-master", idem_key="ppt-master-stage",
    )
    effect, _ = EffectLog(tmp_path / "effects.sqlite3").plan(intent, now=1)
    return effect


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    return path


def _skill_entries(root: str = "ppt-master") -> dict[str, bytes]:
    return {
        f"{root}/LICENSE": b"MIT License\nCopyright (c) 2025-2026 Hugo He",
        f"{root}/SKILL.md": b"# PPT Master",
        f"{root}/SPONSORS.md": b"Sponsor",
        f"{root}/SPONSORS_CN.md": b"\xe8\xb5\x9e\xe5\x8a\xa9",
        f"{root}/scripts/attribution_guard.py": b"# guard",
        f"{root}/scripts/console_encoding.py": b"# encoding",
        f"{root}/scripts/project_manager.py": b"# manager",
        f"{root}/scripts/project_management/cli.py": b"# manager cli",
        f"{root}/scripts/svg_quality_checker.py": b"# quality checker",
        f"{root}/scripts/svg_quality/cli.py": b"# quality cli",
        f"{root}/scripts/svg_to_pptx.py": b"# converter",
        f"{root}/scripts/svg_to_pptx/pptx_package/cli.py": b"# converter cli",
        f"{root}/scripts/register_template.py": b"# register",
        f"{root}/scripts/template_preview_pptx.py": b"# preview",
        f"{root}/scripts/pptx_delivery_check.py": b"# checker",
    }


def test_preview_only_accepts_allowlisted_official_https_and_locks_revision() -> None:
    preview = _preview()

    assert preview.source_id == "ppt-master"
    assert preview.repository == "hugohe3/ppt-master"
    assert preview.archive_url.endswith(f"/{REVISION}.zip")
    for url in ("http://github.com/hugohe3/ppt-master", "https://github.com/other/repo", "https://evil.example/hugohe3/ppt-master"):
        with pytest.raises(GitHubAcquisitionError):
            preview_github_source(url, resolver=lambda _: REVISION)
    with pytest.raises(GitHubAcquisitionError):
        preview_github_source("https://github.com/hugohe3/ppt-master", resolver=lambda _: "main")


def test_confirmed_intent_is_fixed_and_has_no_user_url_or_path() -> None:
    intent = build_acquisition_intent(
        _preview(), session_id="session", root_id="root", step_key="stage", gate_decision_id="gate:1",
        intent_ref="crp://plugin-intents/ppt-master",
    )

    assert intent.kind == EFFECT_KIND
    assert intent.effect_class is EffectClass.QUERYABLE
    assert intent.payload == {
        "source_id": "ppt-master", "repository": "hugohe3/ppt-master", "revision": REVISION,
        "archive_url": f"https://github.com/hugohe3/ppt-master/archive/{REVISION}.zip",
        "mode": "disabled_staging_only",
    }
    assert "path" not in intent.payload


def test_staging_is_receipted_idempotent_and_never_activates(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "source.zip", _skill_entries())
    downloader, store = _Downloader(archive), _Store()
    handler = GitHubAcquisitionStagingHandler(
        staging_root=tmp_path / "staging", downloader=downloader, extractor=SafeZipExtractor(), artifact_store=store,
    )
    effect = _effect(tmp_path)

    first = handler.stage(effect)
    replay = handler.stage(effect)

    assert first == replay
    assert len(downloader.calls) == len(store.stored) == 1
    assert store.stored[0][1]["repository"] == "hugohe3/ppt-master"
    assert not (tmp_path / "staging" / effect.operation_id).exists()
    assert handler.probe(effect) == (EffectState.SETTLED_OK, first)


def test_zip_slip_is_rejected_before_artifact_store_write(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "malicious.zip", {"../outside.txt": b"no"})
    downloader, store = _Downloader(archive), _Store()
    handler = GitHubAcquisitionStagingHandler(
        staging_root=tmp_path / "staging", downloader=downloader, extractor=SafeZipExtractor(), artifact_store=store,
    )

    with pytest.raises(GitHubAcquisitionError, match="escapes staging root"):
        handler.stage(_effect(tmp_path))
    assert store.stored == []


@pytest.mark.parametrize(
    "entries",
    [
        _skill_entries() | {"other/README.md": b"second root"},
        {"ppt-master/SKILL.md": b"incomplete"},
    ],
)
def test_extractor_rejects_multiple_roots_or_missing_skill_entrypoints(tmp_path: Path, entries: dict[str, bytes]) -> None:
    archive = _zip(tmp_path / "invalid.zip", entries)

    with pytest.raises(GitHubAcquisitionError, match="top-level skill directory|entrypoints"):
        SafeZipExtractor().extract(archive, tmp_path / "out", max_files=20_000, max_bytes=128 * 1024 * 1024)


def test_extractor_rejects_missing_svg_quality_checker(tmp_path: Path) -> None:
    entries = _skill_entries()
    del entries["ppt-master/scripts/svg_quality_checker.py"]
    archive = _zip(tmp_path / "missing-quality-checker.zip", entries)

    with pytest.raises(GitHubAcquisitionError, match="entrypoints"):
        SafeZipExtractor().extract(archive, tmp_path / "out", max_files=20_000, max_bytes=128 * 1024 * 1024)


def test_extractor_rejects_missing_license(tmp_path: Path) -> None:
    entries = _skill_entries()
    del entries["ppt-master/LICENSE"]
    archive = _zip(tmp_path / "missing-license.zip", entries)

    with pytest.raises(GitHubAcquisitionError, match="entrypoints"):
        SafeZipExtractor().extract(archive, tmp_path / "out", max_files=20_000, max_bytes=128 * 1024 * 1024)


def test_extractor_accepts_reviewed_upstream_repository_layout(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "upstream.zip", _skill_entries("ppt-master-a" * 8 + "/skills/ppt-master"))

    extracted = SafeZipExtractor().extract(
        archive, tmp_path / "out", max_files=20_000, max_bytes=128 * 1024 * 1024,
    )

    assert extracted == tmp_path / "out" / "skill"


@pytest.mark.parametrize(
    "entries",
    [
        _skill_entries("ppt-master-commit/skills/renamed-ppt-master"),
        _skill_entries("ppt-master-commit/skills/ppt-master") | {"ppt-master-commit/other/SKILL.md": b"other skill"},
        _skill_entries("ppt-master-commit/skills/ppt-master") | _skill_entries("ppt-master-commit"),
    ],
)
def test_extractor_rejects_drifted_or_ambiguous_skill_layout(tmp_path: Path, entries: dict[str, bytes]) -> None:
    archive = _zip(tmp_path / "drifted.zip", entries)

    with pytest.raises(GitHubAcquisitionError, match="layout drifted|unambiguous"):
        SafeZipExtractor().extract(archive, tmp_path / "out", max_files=20_000, max_bytes=128 * 1024 * 1024)


@pytest.mark.parametrize("missing", ["scripts/attribution_guard.py", "scripts/project_management/cli.py", "SPONSORS.md"])
def test_extractor_fails_before_install_when_attribution_or_gate_entry_is_missing(tmp_path: Path, missing: str) -> None:
    entries = _skill_entries()
    del entries[f"ppt-master/{missing}"]
    archive = _zip(tmp_path / "missing-governance-entry.zip", entries)

    with pytest.raises(GitHubAcquisitionError, match="entrypoints"):
        SafeZipExtractor().extract(archive, tmp_path / "out", max_files=20_000, max_bytes=128 * 1024 * 1024)
