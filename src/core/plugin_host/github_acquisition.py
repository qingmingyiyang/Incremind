"""Governed GitHub acquisition for the one reviewed PPT Master source.

This module deliberately stops at a disabled staging artifact.  It has no
activation, import, subprocess, or runtime-registration capability.
"""
from __future__ import annotations

import re
import shutil
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

from core.effect_log import Effect, EffectClass, EffectIntent, EffectPurpose, EffectState


SOURCE_ID = "ppt-master"
REPOSITORY = "hugohe3/ppt-master"
EFFECT_KIND = "plugin_github_acquisition_stage"
GATE_PURPOSE = "plugin_skill_acquisition"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
MAX_FILES = 20_000
_REQUIRED_SKILL_FILES = frozenset({
    "LICENSE",
    "SKILL.md",
    "SPONSORS.md",
    "SPONSORS_CN.md",
    "scripts/attribution_guard.py",
    "scripts/console_encoding.py",
    "scripts/project_manager.py",
    "scripts/project_management/cli.py",
    "scripts/svg_quality_checker.py",
    "scripts/svg_quality/cli.py",
    "scripts/svg_to_pptx.py",
    "scripts/svg_to_pptx/pptx_package/cli.py",
    "scripts/register_template.py",
    "scripts/template_preview_pptx.py",
    "scripts/pptx_delivery_check.py",
})
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class GitHubAcquisitionError(ValueError):
    """Raised when a source or staged artifact crosses the acquisition boundary."""


class GitHubAcquisitionConflict(GitHubAcquisitionError):
    """Raised when a durable receipt or frozen descriptor drifts."""


class RevisionResolver(Protocol):
    def __call__(self, repository: str) -> str: ...


class ArchiveDownloader(Protocol):
    def download(self, url: str, destination: Path, *, max_bytes: int) -> Path: ...


class ArchiveExtractor(Protocol):
    def extract(
        self, archive: Path, destination: Path, *, max_files: int, max_bytes: int,
    ) -> Path: ...


class ArtifactStore(Protocol):
    def store(self, operation_id: str, descriptor: Mapping[str, object], tree: Path) -> str: ...

    def probe(self, operation_id: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class GitHubAcquisitionPreview:
    source_id: str
    repository: str
    revision: str
    archive_url: str

    @property
    def descriptor(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "repository": self.repository,
            "revision": self.revision,
            "archive_url": self.archive_url,
        }


def preview_github_source(user_url: str, *, resolver: RevisionResolver) -> GitHubAcquisitionPreview:
    """Map an allowed display URL to the one controlled immutable source."""

    _require_official_repository_url(user_url)
    revision = resolver(REPOSITORY)
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise GitHubAcquisitionError("GitHub resolver did not return a locked immutable revision")
    return GitHubAcquisitionPreview(
        source_id=SOURCE_ID,
        repository=REPOSITORY,
        revision=revision,
        archive_url=f"https://github.com/{REPOSITORY}/archive/{revision}.zip",
    )


def build_acquisition_intent(
    preview: GitHubAcquisitionPreview,
    *,
    session_id: str,
    root_id: str,
    step_key: str,
    gate_decision_id: str,
    intent_ref: str,
    turn_id: str | None = None,
    parent_id: str | None = None,
    idem_key: str | None = None,
) -> EffectIntent:
    """Build the fixed descriptor that confirmation may submit to Core.

    Caller input cannot introduce a URL, filesystem path, kind, class, or
    purpose.  The frozen revision is deliberately present in both payload and
    rev_set so any replay identity drift is rejected by EffectLog.
    """

    _validate_preview(preview)
    payload = {
        "source_id": SOURCE_ID,
        "repository": REPOSITORY,
        "revision": preview.revision,
        "archive_url": preview.archive_url,
        "mode": "disabled_staging_only",
    }
    return EffectIntent(
        session_id=session_id,
        root_id=root_id,
        step_key=step_key,
        kind=EFFECT_KIND,
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=intent_ref,
        gate_decision_id=gate_decision_id,
        rev_set={
            "github_source_id": SOURCE_ID,
            "github_repository": REPOSITORY,
            "github_revision": preview.revision,
            "acquisition_contract_revision": "1",
        },
        payload=payload,
        turn_id=turn_id,
        parent_id=parent_id,
        idem_key=idem_key,
    )


class GitHubAcquisitionStagingHandler:
    """Effect handler that downloads and stores disabled bytes, never executes them."""

    def __init__(
        self,
        *,
        staging_root: Path,
        downloader: ArchiveDownloader,
        extractor: ArchiveExtractor,
        artifact_store: ArtifactStore,
    ) -> None:
        requested_root = Path(staging_root).expanduser().absolute()
        _reject_links(requested_root)
        self._root = requested_root.resolve(strict=False)
        self._downloader = downloader
        self._extractor = extractor
        self._artifact_store = artifact_store

    def __call__(self, effect: Effect) -> str:
        return self.stage(effect)

    def stage(self, effect: Effect) -> str:
        descriptor = _descriptor_from_effect(effect)
        existing = self._artifact_store.probe(effect.operation_id)
        if existing is not None:
            if not isinstance(existing, str) or not existing:
                raise GitHubAcquisitionConflict("artifact receipt is invalid")
            return existing
        operation_root = _operation_path(self._root, effect.operation_id)
        archive = operation_root / "source.zip"
        extracted = operation_root / "extracted"
        try:
            operation_root.mkdir(parents=True, exist_ok=True)
            _reject_links(operation_root)
            downloaded = self._downloader.download(
                str(descriptor["archive_url"]), archive, max_bytes=MAX_ARCHIVE_BYTES,
            )
            downloaded = Path(downloaded)
            if downloaded.resolve(strict=False) != archive.resolve(strict=False):
                raise GitHubAcquisitionError("downloader returned an uncontrolled archive path")
            if not archive.is_file() or archive.is_symlink() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise GitHubAcquisitionError("downloaded archive is unavailable or exceeds byte limit")
            tree = self._extractor.extract(
                archive, extracted, max_files=MAX_FILES, max_bytes=MAX_EXTRACTED_BYTES,
            )
            tree = Path(tree)
            if not tree.is_dir() or tree.is_symlink() or not tree.resolve(strict=False).is_relative_to(extracted.resolve(strict=False)):
                raise GitHubAcquisitionError("archive extractor returned an uncontrolled staging tree")
            _validate_tree(tree, max_files=MAX_FILES, max_bytes=MAX_EXTRACTED_BYTES)
            receipt = self._artifact_store.store(effect.operation_id, descriptor, tree)
            if not isinstance(receipt, str) or not receipt:
                raise GitHubAcquisitionError("artifact store did not return a receipt")
            return receipt
        finally:
            # Durable artifacts are owned by artifact_store.  Local transport
            # staging is disposable and contains no active runtime surface.
            if operation_root.exists() and operation_root.is_dir() and operation_root.parent == self._root:
                shutil.rmtree(operation_root, ignore_errors=True)

    def probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        """Resolve recovery from the artifact store without rerunning a handler."""

        _descriptor_from_effect(effect)
        receipt = self._artifact_store.probe(effect.operation_id)
        if receipt is None:
            return EffectState.PLANNED, None
        if not isinstance(receipt, str) or not receipt:
            raise GitHubAcquisitionConflict("artifact receipt is invalid")
        return EffectState.SETTLED_OK, receipt


class SafeZipExtractor:
    """Reference extractor with zip-slip, link, file-count, and byte fences."""

    def extract(self, archive: Path, destination: Path, *, max_files: int, max_bytes: int) -> Path:
        destination.mkdir(parents=True, exist_ok=False)
        total = 0
        count = 0
        roots: set[str] = set()
        with zipfile.ZipFile(archive) as source:
            members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            skill_candidates: set[PurePosixPath] = set()
            for info in source.infolist():
                relative = _safe_zip_member(info.filename)
                if info.is_dir():
                    continue
                if _zip_is_link(info):
                    raise GitHubAcquisitionError("archive links are forbidden")
                members.append((info, relative))
                roots.add(relative.parts[0])
                if relative.name == "SKILL.md":
                    skill_candidates.add(relative.parent)
            if len(roots) != 1:
                raise GitHubAcquisitionError("archive must contain one top-level skill directory")
            wrapper = PurePosixPath(next(iter(roots)))
            direct, upstream = wrapper, wrapper / "skills" / "ppt-master"
            if skill_candidates not in ({direct}, {upstream}):
                raise GitHubAcquisitionError("archive must contain one unambiguous skill root")
            selected = next(iter(skill_candidates))
            skill_root = destination / "skill"
            for info, relative in members:
                if relative != selected and selected not in relative.parents:
                    continue
                count += 1
                if count > max_files:
                    raise GitHubAcquisitionError("archive extraction exceeds resource limits")
                tail = relative.relative_to(selected)
                output = skill_root.joinpath(*tail.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info, "r") as reader, output.open("xb") as writer:
                    while chunk := reader.read(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise GitHubAcquisitionError("archive extraction exceeds resource limits")
                        writer.write(chunk)
        if not skill_root.is_dir() or skill_root.is_symlink():
            raise GitHubAcquisitionError("archive skill root is invalid")
        missing = sorted(path for path in _REQUIRED_SKILL_FILES if not _safe_required_file(skill_root, path))
        if missing:
            raise GitHubAcquisitionError("archive skill entrypoints are missing")
        return skill_root


def _select_skill_root(archive_root: Path) -> Path:
    """Select one fixed skill layout without guessing among archive contents.

    A standalone skill archive may expose ``SKILL.md`` at its only top-level
    directory.  The reviewed upstream repository archive instead has one
    repository wrapper and precisely ``skills/ppt-master`` below it.  Any
    other Skill document would make the artifact ambiguous, so reject it
    before an artifact store can freeze the wrong execution surface.
    """

    candidates = [path.parent for path in archive_root.rglob("SKILL.md") if path.is_file() and not path.is_symlink()]
    direct = archive_root
    upstream = archive_root / "skills" / "ppt-master"
    allowed = {direct.resolve(strict=False), upstream.resolve(strict=False)}
    if any(candidate.resolve(strict=False) not in allowed for candidate in candidates):
        raise GitHubAcquisitionError("archive skill layout drifted")
    unique_candidates = {candidate.resolve(strict=False) for candidate in candidates}
    if len(unique_candidates) != 1:
        raise GitHubAcquisitionError("archive must contain one unambiguous skill root")
    selected = next(iter(unique_candidates))
    if selected == upstream.resolve(strict=False):
        # Do not allow a repository archive to move the reviewed skill.
        return upstream
    if selected == direct.resolve(strict=False):
        return direct
    raise GitHubAcquisitionError("archive skill layout drifted")


def _safe_required_file(skill_root: Path, relative: str) -> bool:
    path = skill_root.joinpath(*PurePosixPath(relative).parts)
    return path.is_file() and not path.is_symlink() and path.resolve(strict=False).is_relative_to(skill_root.resolve(strict=False))


def _require_official_repository_url(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise GitHubAcquisitionError("GitHub source URL is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as error:
        raise GitHubAcquisitionError("GitHub source URL is invalid") from error
    if parsed.scheme != "https" or parsed.hostname != "github.com" or port not in {None, 443}:
        raise GitHubAcquisitionError("GitHub source must use official HTTPS repository URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GitHubAcquisitionError("GitHub source URL is invalid")
    path = parsed.path.rstrip("/")
    if path != f"/{REPOSITORY}":
        raise GitHubAcquisitionError("GitHub repository is outside the acquisition allowlist")


def _validate_preview(preview: GitHubAcquisitionPreview) -> None:
    if not isinstance(preview, GitHubAcquisitionPreview):
        raise TypeError("preview must be a GitHubAcquisitionPreview")
    expected = f"https://github.com/{REPOSITORY}/archive/{preview.revision}.zip"
    if (preview.source_id, preview.repository, preview.archive_url) != (SOURCE_ID, REPOSITORY, expected):
        raise GitHubAcquisitionConflict("GitHub acquisition preview drifted")
    if not _REVISION.fullmatch(preview.revision):
        raise GitHubAcquisitionError("GitHub revision is not immutable")


def _descriptor_from_effect(effect: Effect) -> dict[str, object]:
    if effect.kind != EFFECT_KIND or effect.effect_class is not EffectClass.QUERYABLE or effect.purpose is not EffectPurpose.PRIMARY:
        raise GitHubAcquisitionConflict("Effect is not a governed GitHub acquisition")
    # EffectLog intentionally persists the canonical payload digest rather than
    # the payload itself.  The durable rev_set therefore supplies the handler's
    # complete authority; URL and path data are reconstructed from constants.
    # The builder above is the only API that ever accepts a preview descriptor.
    rev_set = effect.rev_set
    preview = GitHubAcquisitionPreview(
        source_id=rev_set.get("github_source_id"), repository=rev_set.get("github_repository"),
        revision=rev_set.get("github_revision"),
        archive_url=f"https://github.com/{REPOSITORY}/archive/{rev_set.get('github_revision')}.zip",
    )
    _validate_preview(preview)
    if rev_set.get("github_source_id") != SOURCE_ID or rev_set.get("github_repository") != REPOSITORY or rev_set.get("github_revision") != preview.revision or rev_set.get("acquisition_contract_revision") != "1":
        raise GitHubAcquisitionConflict("GitHub acquisition revision facts drifted")
    return preview.descriptor


def _operation_path(root: Path, operation_id: str) -> Path:
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", operation_id):
        raise GitHubAcquisitionError("operation id is invalid")
    candidate = (root / operation_id).resolve(strict=False)
    if candidate == root or not candidate.is_relative_to(root):
        raise GitHubAcquisitionError("operation staging path escaped root")
    return candidate


def _safe_zip_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise GitHubAcquisitionError("archive member escapes staging root")
    if any(part in {"", ".", ".."} or part.rstrip(" .") != part for part in path.parts):
        raise GitHubAcquisitionError("archive member path is unsafe")
    return path


def _zip_is_link(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _reject_links(root: Path) -> None:
    for item in (root, *root.parents):
        if item.exists() and item.is_symlink():
            raise GitHubAcquisitionError("staging path cannot cross a link")


def _validate_tree(root: Path, *, max_files: int, max_bytes: int) -> None:
    count = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GitHubAcquisitionError("extracted tree cannot contain links")
        if path.is_file():
            count += 1
            total += path.stat().st_size
            if count > max_files or total > max_bytes:
                raise GitHubAcquisitionError("extracted tree exceeds resource limits")
