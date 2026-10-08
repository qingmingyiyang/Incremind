from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence


class ApplicationSkillError(ValueError):
    """Raised when an Application Skill package violates its read-only contract."""


_SKILL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_TOP_LEVEL_DIRECTORIES = frozenset({"agents", "references", "scripts", "assets"})
_TEXT_RESOURCE_GROUPS = frozenset({"agents", "references", "scripts"})
_MAX_FRONTMATTER_BYTES = 4096
_MAX_SKILL_BODY_BYTES = 16 * 1024
_MAX_TEXT_RESOURCE_BYTES = 64 * 1024
_MAX_ASSET_BYTES = 1024 * 1024
_MAX_PACKAGE_BYTES = 2 * 1024 * 1024
_MAX_PACKAGE_FILES = 128
_MAX_RELATIVE_PATH_LENGTH = 240
_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*:\s*(?:bearer|basic)\s+\S+"),
    re.compile(r"(?i)cookie\s*:\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_./+-]{16,}"),
)
_SKILL_MATURITIES = frozenset({"draft", "verified", "deprecated"})


@dataclass(frozen=True, slots=True)
class ApplicationSkillSource:
    source_id: str
    root: Path
    source_kind: str = "user"

    def __post_init__(self) -> None:
        if not _SOURCE_ID.fullmatch(self.source_id):
            raise ApplicationSkillError("invalid Application Skill source id")
        if self.source_kind not in {"bundled", "user", "plugin", "external"}:
            raise ApplicationSkillError("unsupported Application Skill source kind")


@dataclass(frozen=True, slots=True)
class ApplicationSkillResource:
    relative_path: str
    resource_kind: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ApplicationSkillVerifiedContent:
    """Immutable, canonical bytes for a verified Application Skill package.

    This value deliberately carries no directory handle.  Consumers can keep
    loading a reviewed package after its staging path has been removed or
    altered, but must revalidate these bytes before use.
    """

    files: tuple[tuple[str, bytes], ...]

    @classmethod
    def from_mapping(
        cls, files: Mapping[str, bytes],
    ) -> "ApplicationSkillVerifiedContent":
        if not isinstance(files, Mapping):
            raise ApplicationSkillError("verified Application Skill content must be a mapping")
        items: list[tuple[str, bytes]] = []
        for path, content in files.items():
            if not isinstance(path, str) or not isinstance(content, bytes):
                raise ApplicationSkillError("verified Application Skill content is invalid")
            items.append((path, content))
        return cls(tuple(sorted(items, key=lambda item: item[0])))

    def __post_init__(self) -> None:
        if len(self.files) > _MAX_PACKAGE_FILES:
            raise ApplicationSkillError("skill package file count exceeds the limit")
        if sum(len(content) for _, content in self.files) > _MAX_PACKAGE_BYTES:
            raise ApplicationSkillError("skill package size exceeds the limit")
        paths = [path for path, _ in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ApplicationSkillError("verified Application Skill paths must be unique and canonical")
        for path, content in self.files:
            _canonical_relative_path(path)
            if not isinstance(content, bytes):
                raise ApplicationSkillError("verified Application Skill bytes are invalid")

    def file_mapping(self) -> dict[str, bytes]:
        """Return a fresh mapping; contained byte strings remain immutable."""
        return dict(self.files)


@dataclass(frozen=True, slots=True)
class ApplicationSkillPackage:
    skill_id: str
    name: str
    description: str
    trigger_boundary: str
    validation: str
    maturity: str
    source_id: str
    source_kind: str
    package_root: Path
    fingerprint: str
    skill_file_size_bytes: int
    instruction_size_bytes: int
    package_size_bytes: int
    resources: tuple[ApplicationSkillResource, ...]
    verified_content: ApplicationSkillVerifiedContent | None = None


@dataclass(frozen=True, slots=True)
class ApplicationSkillCatalogIssue:
    source_id: str
    package_name: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ApplicationSkillCatalogSnapshot:
    packages: tuple[ApplicationSkillPackage, ...]
    issues: tuple[ApplicationSkillCatalogIssue, ...]
    scanned_source_count: int

    def get(self, skill_id: str) -> ApplicationSkillPackage | None:
        return next((package for package in self.packages if package.skill_id == skill_id), None)


@dataclass(frozen=True, slots=True)
class ApplicationSkillInstructions:
    skill_id: str
    fingerprint: str
    markdown: str
    size_bytes: int


class ApplicationSkillCatalog:
    """Discover package metadata without retaining instruction or resource bodies."""

    def inspect_package(
        self,
        package_root: Path,
        *,
        source_id: str = "import-preview",
    ) -> ApplicationSkillPackage:
        """Inspect exactly one user-selected package without scanning its siblings."""

        root = package_root.expanduser()
        if root.is_symlink():
            raise ApplicationSkillError("skill package cannot be a symlink")
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise ApplicationSkillError("skill package directory is missing") from error
        if not resolved.is_dir():
            raise ApplicationSkillError("skill package must be a directory")
        return _inspect_package(
            resolved,
            ApplicationSkillSource(source_id, resolved.parent, "user"),
        )

    def package_from_verified_content(
        self,
        content: ApplicationSkillVerifiedContent | Mapping[str, bytes],
        *,
        source_id: str = "external-verified",
        source_kind: str = "external",
        package_root: Path | None = None,
    ) -> ApplicationSkillPackage:
        """Build a package solely from pre-verified immutable bytes.

        ``package_root`` remains metadata for compatibility with existing
        callers.  The loader never opens it when ``verified_content`` exists.
        """
        verified = content if isinstance(content, ApplicationSkillVerifiedContent) else (
            ApplicationSkillVerifiedContent.from_mapping(content)
        )
        root = Path(".") if package_root is None else Path(package_root)
        source = ApplicationSkillSource(source_id, root.parent, source_kind)
        return _package_from_verified_content(verified, source, root)

    def snapshot_from_packages(
        self,
        packages: Sequence[ApplicationSkillPackage],
        *,
        scanned_source_count: int = 0,
    ) -> ApplicationSkillCatalogSnapshot:
        """Safely compose preselected filesystem and verified packages.

        This is the only public composition point for dynamic package sources;
        it retains the catalog's duplicate-identity fail-closed behavior.
        """
        if not isinstance(scanned_source_count, int) or scanned_source_count < 0:
            raise ApplicationSkillError("scanned source count is invalid")
        if any(not isinstance(package, ApplicationSkillPackage) for package in packages):
            raise ApplicationSkillError("Application Skill package list is invalid")
        for package in packages:
            if package.verified_content is not None:
                _revalidate_verified_package(package)
        unique, issues = _remove_duplicate_identities(tuple(packages))
        return ApplicationSkillCatalogSnapshot(
            packages=tuple(sorted(unique, key=lambda item: item.skill_id)),
            issues=tuple(sorted(issues, key=lambda item: (item.source_id, item.package_name, item.code))),
            scanned_source_count=scanned_source_count,
        )

    def discover(self, sources: Sequence[ApplicationSkillSource]) -> ApplicationSkillCatalogSnapshot:
        packages: list[ApplicationSkillPackage] = []
        issues: list[ApplicationSkillCatalogIssue] = []
        for source in sorted(sources, key=lambda item: (item.source_kind, item.source_id)):
            found, source_issues = self._discover_source(source)
            packages.extend(found)
            issues.extend(source_issues)

        packages, duplicate_issues = _remove_duplicate_identities(packages)
        issues.extend(duplicate_issues)
        return ApplicationSkillCatalogSnapshot(
            packages=tuple(sorted(packages, key=lambda item: item.skill_id)),
            issues=tuple(sorted(issues, key=lambda item: (item.source_id, item.package_name, item.code))),
            scanned_source_count=len(sources),
        )

    def discover_selected(
        self,
        sources: Sequence[ApplicationSkillSource],
        skill_ids: Sequence[str],
    ) -> ApplicationSkillCatalogSnapshot:
        """Inspect only explicitly enabled package directories without enumerating siblings."""

        requested = tuple(dict.fromkeys(skill_ids))
        if any(not isinstance(skill_id, str) or not _SKILL_NAME.fullmatch(skill_id) for skill_id in requested):
            raise ApplicationSkillError("selected Application Skill id is invalid")
        packages: list[ApplicationSkillPackage] = []
        issues: list[ApplicationSkillCatalogIssue] = []
        for source in sorted(sources, key=lambda item: (item.source_kind, item.source_id)):
            root = source.root.expanduser()
            if root.is_symlink():
                issues.append(_issue(source, root.name or source.source_id, "source_symlink", "skill source cannot be a symlink"))
                continue
            try:
                resolved_root = root.resolve(strict=True)
            except OSError:
                issues.append(_issue(source, root.name or source.source_id, "source_missing", "skill source directory is missing"))
                continue
            if not resolved_root.is_dir():
                issues.append(_issue(source, root.name or source.source_id, "source_not_directory", "skill source must be a directory"))
                continue
            for skill_id in requested:
                package_root = resolved_root / skill_id
                if not package_root.exists():
                    continue
                try:
                    packages.append(_inspect_package(package_root, source))
                except (ApplicationSkillError, OSError, UnicodeError) as error:
                    issues.append(_issue(source, skill_id, "invalid_package", str(error)))
        packages, duplicate_issues = _remove_duplicate_identities(packages)
        issues.extend(duplicate_issues)
        return ApplicationSkillCatalogSnapshot(
            packages=tuple(sorted(packages, key=lambda item: item.skill_id)),
            issues=tuple(sorted(issues, key=lambda item: (item.source_id, item.package_name, item.code))),
            scanned_source_count=len(sources),
        )

    def _discover_source(
        self,
        source: ApplicationSkillSource,
    ) -> tuple[list[ApplicationSkillPackage], list[ApplicationSkillCatalogIssue]]:
        root = source.root.expanduser()
        if root.is_symlink():
            return [], [_issue(source, root.name or source.source_id, "source_symlink", "skill source cannot be a symlink")]
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            return [], [_issue(source, root.name or source.source_id, "source_missing", "skill source directory is missing")]
        if not resolved_root.is_dir():
            return [], [_issue(source, root.name or source.source_id, "source_not_directory", "skill source must be a directory")]

        packages: list[ApplicationSkillPackage] = []
        issues: list[ApplicationSkillCatalogIssue] = []
        for entry in sorted(resolved_root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                issues.append(_issue(source, entry.name, "hidden_source_entry", "hidden entries are not skill packages"))
                continue
            if entry.is_symlink() or not entry.is_dir():
                issues.append(_issue(source, entry.name, "invalid_source_entry", "source entries must be non-symlink directories"))
                continue
            try:
                packages.append(_inspect_package(entry, source))
            except (ApplicationSkillError, OSError, UnicodeError) as error:
                issues.append(_issue(source, entry.name, "invalid_package", str(error)))
        return packages, issues


class ApplicationSkillPackageLoader:
    """Load only a selected package's SKILL.md body after fingerprint revalidation."""

    def load_instructions(self, package: ApplicationSkillPackage) -> ApplicationSkillInstructions:
        if not _SHA256.fullmatch(package.fingerprint):
            raise ApplicationSkillError("invalid Application Skill fingerprint")
        if package.verified_content is not None:
            _revalidate_verified_package(package)
            markdown = _read_skill_body_bytes(package.verified_content.file_mapping()["SKILL.md"])
            return ApplicationSkillInstructions(
                skill_id=package.skill_id,
                fingerprint=package.fingerprint,
                markdown=markdown,
                size_bytes=len(markdown.encode("utf-8")),
            )
        source = ApplicationSkillSource(
            source_id=package.source_id,
            root=package.package_root.parent,
            source_kind=package.source_kind,
        )
        current = _inspect_package(package.package_root, source)
        if current.skill_id != package.skill_id or current.fingerprint != package.fingerprint:
            raise ApplicationSkillError("Application Skill package fingerprint drifted")
        markdown = _read_skill_body(package.package_root / "SKILL.md")
        return ApplicationSkillInstructions(
            skill_id=package.skill_id,
            fingerprint=package.fingerprint,
            markdown=markdown,
            size_bytes=len(markdown.encode("utf-8")),
        )


def _inspect_package(package_root: Path, source: ApplicationSkillSource) -> ApplicationSkillPackage:
    if package_root.is_symlink() or not package_root.is_dir():
        raise ApplicationSkillError("skill package must be a non-symlink directory")
    resolved_package = package_root.resolve(strict=True)
    _require_direct_child(resolved_package, source.root.resolve(strict=True))
    if not _SKILL_NAME.fullmatch(resolved_package.name):
        raise ApplicationSkillError("skill package directory must use lowercase hyphen-case")

    entries = sorted(resolved_package.iterdir(), key=lambda item: item.name)
    skill_file = resolved_package / "SKILL.md"
    if not skill_file.exists() or skill_file.is_symlink() or not skill_file.is_file():
        raise ApplicationSkillError("SKILL.md is required and cannot be a symlink")
    for entry in entries:
        if entry.name == "SKILL.md":
            continue
        if entry.name.startswith(".") or entry.name not in _ALLOWED_TOP_LEVEL_DIRECTORIES:
            raise ApplicationSkillError(f"unsupported top-level skill entry: {entry.name}")
        if entry.is_symlink() or not entry.is_dir():
            raise ApplicationSkillError(f"skill resource group must be a non-symlink directory: {entry.name}")

    metadata, instruction_size = _parse_skill_file(skill_file)
    name = metadata["name"]
    description = metadata["description"]
    if name != resolved_package.name:
        raise ApplicationSkillError("SKILL.md name must match its package directory")

    file_records = [_file_record(skill_file, resolved_package, "instructions")]
    resources: list[ApplicationSkillResource] = []
    for group in sorted(_ALLOWED_TOP_LEVEL_DIRECTORIES):
        group_root = resolved_package / group
        if not group_root.exists():
            continue
        group_records = list(_resource_files(group_root, resolved_package, group))
        if group == "agents" and any(record.relative_path != "agents/openai.yaml" for record in group_records):
            raise ApplicationSkillError("agents may only contain openai.yaml")
        resources.extend(group_records)
        file_records.extend(group_records)

    if len(file_records) > _MAX_PACKAGE_FILES:
        raise ApplicationSkillError("skill package file count exceeds the limit")
    package_size = sum(record.size_bytes for record in file_records)
    if package_size > _MAX_PACKAGE_BYTES:
        raise ApplicationSkillError("skill package size exceeds the limit")
    return ApplicationSkillPackage(
        skill_id=name,
        name=name,
        description=description,
        trigger_boundary=metadata["trigger_boundary"],
        validation=metadata["validation"],
        maturity=metadata["maturity"],
        source_id=source.source_id,
        source_kind=source.source_kind,
        package_root=resolved_package,
        fingerprint=_package_fingerprint(file_records),
        skill_file_size_bytes=file_records[0].size_bytes,
        instruction_size_bytes=instruction_size,
        package_size_bytes=package_size,
        resources=tuple(sorted(resources, key=lambda item: item.relative_path)),
    )


_PACKAGE_FIELDS = (
    "skill_id", "name", "description", "trigger_boundary", "validation", "maturity",
    "source_id", "source_kind", "package_root", "fingerprint", "skill_file_size_bytes",
    "instruction_size_bytes", "package_size_bytes", "resources",
)


def _package_from_verified_content(
    verified: ApplicationSkillVerifiedContent,
    source: ApplicationSkillSource,
    package_root: Path,
) -> ApplicationSkillPackage:
    files = verified.file_mapping()
    if "SKILL.md" not in files:
        raise ApplicationSkillError("SKILL.md is required")
    records: list[ApplicationSkillResource] = []
    for relative, content in verified.files:
        resource_kind = _content_resource_kind(relative)
        records.append(_content_file_record(relative, content, resource_kind))
    if len(records) > _MAX_PACKAGE_FILES:
        raise ApplicationSkillError("skill package file count exceeds the limit")
    package_size = sum(record.size_bytes for record in records)
    if package_size > _MAX_PACKAGE_BYTES:
        raise ApplicationSkillError("skill package size exceeds the limit")
    agent_paths = {record.relative_path for record in records if record.resource_kind == "agents"}
    if agent_paths and agent_paths != {"agents/openai.yaml"}:
        raise ApplicationSkillError("agents may only contain openai.yaml")
    metadata, instruction_size = _parse_skill_bytes(files["SKILL.md"])
    resources = tuple(sorted(
        (record for record in records if record.resource_kind != "instructions"),
        key=lambda item: item.relative_path,
    ))
    return ApplicationSkillPackage(
        skill_id=metadata["name"], name=metadata["name"], description=metadata["description"],
        trigger_boundary=metadata["trigger_boundary"], validation=metadata["validation"],
        maturity=metadata["maturity"], source_id=source.source_id, source_kind=source.source_kind,
        package_root=package_root, fingerprint=_package_fingerprint(records),
        skill_file_size_bytes=len(files["SKILL.md"]), instruction_size_bytes=instruction_size,
        package_size_bytes=package_size, resources=resources, verified_content=verified,
    )


def _same_package_contract(
    current: ApplicationSkillPackage, expected: ApplicationSkillPackage,
) -> bool:
    return all(getattr(current, field) == getattr(expected, field) for field in _PACKAGE_FIELDS)


def _revalidate_verified_package(package: ApplicationSkillPackage) -> None:
    verified = package.verified_content
    if verified is None:
        raise ApplicationSkillError("verified Application Skill content is unavailable")
    source = ApplicationSkillSource(
        source_id=package.source_id,
        root=package.package_root.parent,
        source_kind=package.source_kind,
    )
    current = _package_from_verified_content(verified, source, package.package_root)
    if not _same_package_contract(current, package):
        raise ApplicationSkillError("verified Application Skill package contract drifted")


def _canonical_relative_path(relative: str) -> PurePosixPath:
    if not relative or len(relative) > _MAX_RELATIVE_PATH_LENGTH or "\\" in relative:
        raise ApplicationSkillError("skill resource path is invalid or too long")
    path = PurePosixPath(relative)
    if path.as_posix() != relative or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts):
        raise ApplicationSkillError("skill resource path is invalid or hidden")
    return path


def _content_resource_kind(relative: str) -> str:
    path = _canonical_relative_path(relative)
    if relative == "SKILL.md":
        return "instructions"
    if len(path.parts) < 2 or path.parts[0] not in _ALLOWED_TOP_LEVEL_DIRECTORIES:
        raise ApplicationSkillError(f"unsupported top-level skill entry: {path.parts[0]}")
    return path.parts[0]


def _content_file_record(
    relative: str, content: bytes, resource_kind: str,
) -> ApplicationSkillResource:
    limit = _MAX_ASSET_BYTES if resource_kind == "assets" else _MAX_TEXT_RESOURCE_BYTES
    if resource_kind == "instructions":
        limit = _MAX_FRONTMATTER_BYTES + _MAX_SKILL_BODY_BYTES
    if len(content) > limit:
        raise ApplicationSkillError(f"skill resource exceeds its size limit: {relative}")
    if resource_kind in _TEXT_RESOURCE_GROUPS:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ApplicationSkillError(f"text skill resource must be valid UTF-8: {relative}") from error
        _reject_secret_text(text, label=relative)
    return ApplicationSkillResource(
        relative, resource_kind, len(content), hashlib.sha256(content).hexdigest(),
    )


def _parse_skill_file(path: Path) -> tuple[dict[str, str], int]:
    raw = path.read_bytes()
    return _parse_skill_bytes(raw)


def _parse_skill_bytes(raw: bytes) -> tuple[dict[str, str], int]:
    if len(raw) > _MAX_FRONTMATTER_BYTES + _MAX_SKILL_BODY_BYTES:
        raise ApplicationSkillError("SKILL.md exceeds the size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ApplicationSkillError("SKILL.md must be valid UTF-8") from error
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ApplicationSkillError("SKILL.md must start with YAML frontmatter")
    closing_index = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing_index is None:
        raise ApplicationSkillError("SKILL.md frontmatter is not closed")
    frontmatter_text = "".join(lines[: closing_index + 1]).encode("utf-8")
    if len(frontmatter_text) > _MAX_FRONTMATTER_BYTES:
        raise ApplicationSkillError("SKILL.md frontmatter exceeds the size limit")
    fields = _parse_frontmatter(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :]).lstrip("\r\n")
    body_size = len(body.encode("utf-8"))
    if not body.strip():
        raise ApplicationSkillError("SKILL.md instructions are required")
    if body_size > _MAX_SKILL_BODY_BYTES:
        raise ApplicationSkillError("SKILL.md instructions exceed the size limit")
    _reject_secret_text(text, label="SKILL.md")
    name = fields["name"]
    description = fields["description"]
    if not _SKILL_NAME.fullmatch(name):
        raise ApplicationSkillError("SKILL.md name must use lowercase hyphen-case")
    if not description or len(description) > 2048:
        raise ApplicationSkillError("SKILL.md description is required and must be at most 2048 characters")
    trigger_boundary = fields.get("trigger_boundary", description)
    validation = fields.get("validation", "manual-review-required")
    maturity = fields.get("maturity", "draft")
    if not trigger_boundary or len(trigger_boundary) > 2048:
        raise ApplicationSkillError("SKILL.md trigger_boundary is required and must be at most 2048 characters")
    if not validation or len(validation) > 2048:
        raise ApplicationSkillError("SKILL.md validation is required and must be at most 2048 characters")
    if maturity not in _SKILL_MATURITIES:
        raise ApplicationSkillError("SKILL.md maturity must be draft, verified, or deprecated")
    return {
        "name": name,
        "description": description,
        "trigger_boundary": trigger_boundary,
        "validation": validation,
        "maturity": maturity,
    }, body_size


def _parse_frontmatter(lines: Sequence[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ApplicationSkillError("SKILL.md frontmatter must contain scalar key-value pairs")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key not in {"name", "description", "trigger_boundary", "validation", "maturity"}:
            raise ApplicationSkillError(f"unsupported SKILL.md frontmatter field: {key}")
        if key in fields:
            raise ApplicationSkillError(f"duplicate SKILL.md frontmatter field: {key}")
        fields[key] = _yaml_scalar(raw_value.strip())
    if not {"name", "description"}.issubset(fields):
        raise ApplicationSkillError("SKILL.md frontmatter requires name and description")
    return fields


def _yaml_scalar(value: str) -> str:
    if not value or value[0] in "[{&*!|>":
        raise ApplicationSkillError("SKILL.md frontmatter only supports scalar strings")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ApplicationSkillError("invalid quoted SKILL.md frontmatter value") from error
        if not isinstance(parsed, str):
            raise ApplicationSkillError("SKILL.md frontmatter values must be strings")
        return parsed.strip()
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ApplicationSkillError("invalid quoted SKILL.md frontmatter value")
        return value[1:-1].replace("''", "'").strip()
    return value.strip()


def _read_skill_body(path: Path) -> str:
    return _read_skill_body_bytes(path.read_bytes())


def _read_skill_body_bytes(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ApplicationSkillError("SKILL.md must be valid UTF-8") from error
    # Match Path.read_text()'s universal-newline behavior for existing
    # filesystem packages while keeping verified packages independent of disk.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    closing_index = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing_index is None:
        raise ApplicationSkillError("SKILL.md frontmatter is not closed")
    body = "".join(lines[closing_index + 1 :]).lstrip("\r\n")
    if len(body.encode("utf-8")) > _MAX_SKILL_BODY_BYTES:
        raise ApplicationSkillError("SKILL.md instructions exceed the size limit")
    return body


def _resource_files(group_root: Path, package_root: Path, resource_kind: str) -> Iterable[ApplicationSkillResource]:
    pending = [group_root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir(), key=lambda item: item.name, reverse=True):
            if entry.name.startswith("."):
                raise ApplicationSkillError(f"hidden skill resource is forbidden: {entry.name}")
            if entry.is_symlink():
                raise ApplicationSkillError(f"skill resource symlink is forbidden: {entry.name}")
            resolved = entry.resolve(strict=True)
            _require_contained(resolved, package_root)
            if entry.is_dir():
                pending.append(entry)
                continue
            if not entry.is_file():
                raise ApplicationSkillError(f"skill resource must be a regular file: {entry.name}")
            yield _file_record(entry, package_root, resource_kind)


def _file_record(path: Path, package_root: Path, resource_kind: str) -> ApplicationSkillResource:
    relative = path.relative_to(package_root).as_posix()
    if len(relative) > _MAX_RELATIVE_PATH_LENGTH or any(part in {"", ".", ".."} for part in Path(relative).parts):
        raise ApplicationSkillError("skill resource path is invalid or too long")
    size = path.stat().st_size
    limit = _MAX_ASSET_BYTES if resource_kind == "assets" else _MAX_TEXT_RESOURCE_BYTES
    if resource_kind == "instructions":
        limit = _MAX_FRONTMATTER_BYTES + _MAX_SKILL_BODY_BYTES
    if size > limit:
        raise ApplicationSkillError(f"skill resource exceeds its size limit: {relative}")
    digest = hashlib.sha256()
    text_probe = bytearray()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
            if resource_kind in _TEXT_RESOURCE_GROUPS and len(text_probe) <= _MAX_TEXT_RESOURCE_BYTES:
                text_probe.extend(chunk)
    if resource_kind in _TEXT_RESOURCE_GROUPS:
        try:
            text = bytes(text_probe).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ApplicationSkillError(f"text skill resource must be valid UTF-8: {relative}") from error
        _reject_secret_text(text, label=relative)
    return ApplicationSkillResource(relative, resource_kind, size, digest.hexdigest())


def _package_fingerprint(records: Sequence[ApplicationSkillResource]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item.relative_path):
        digest.update(record.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _remove_duplicate_identities(
    packages: Sequence[ApplicationSkillPackage],
) -> tuple[list[ApplicationSkillPackage], list[ApplicationSkillCatalogIssue]]:
    by_id: dict[str, list[ApplicationSkillPackage]] = {}
    for package in packages:
        by_id.setdefault(package.skill_id, []).append(package)
    unique: list[ApplicationSkillPackage] = []
    issues: list[ApplicationSkillCatalogIssue] = []
    for skill_id, candidates in by_id.items():
        if len(candidates) == 1:
            unique.append(candidates[0])
            continue
        for package in candidates:
            issues.append(
                ApplicationSkillCatalogIssue(
                    source_id=package.source_id,
                    package_name=skill_id,
                    code="duplicate_identity",
                    detail="duplicate Application Skill identity is fail-closed",
                )
            )
    return unique, issues


def _reject_secret_text(text: str, *, label: str) -> None:
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        raise ApplicationSkillError(f"secret-like material is forbidden in {label}")


def _require_direct_child(path: Path, root: Path) -> None:
    _require_contained(path, root)
    if path.parent != root:
        raise ApplicationSkillError("skill package must be a direct child of its source")


def _require_contained(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ApplicationSkillError("skill path escapes its package boundary") from error


def _issue(source: ApplicationSkillSource, package_name: str, code: str, detail: str) -> ApplicationSkillCatalogIssue:
    return ApplicationSkillCatalogIssue(source.source_id, package_name, code, detail)
