from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping


class ExtensionDetectionError(ValueError):
    """Raised when a frozen artifact cannot be identified exactly once."""


_MAX_FILES = 512
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_PATH_BYTES = 320
_FORMATS = frozenset(
    {
        "openai_codex_plugin",
        "openai_agent_skill",
        "deepseek_harness_skill",
        "deepseek_harness_plugin",
        "codex_hook_config",
        "mcp_config",
        "codex_marketplace",
    }
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)), *(f"LPT{number}" for number in range(1, 10))}
)


@dataclass(frozen=True, slots=True)
class ArtifactInventory:
    """An immutable, bounded view of files already captured in quarantine."""

    _files: Mapping[str, bytes]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_files", MappingProxyType(_capture_files(self._files)))

    @classmethod
    def capture(cls, files: Mapping[str, bytes]) -> "ArtifactInventory":
        return cls(files)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self._files)

    @property
    def total_bytes(self) -> int:
        return sum(len(value) for value in self._files.values())

    def contains(self, path: str) -> bool:
        return path in self._files

    def read_bytes(self, path: str) -> bytes:
        try:
            return bytes(self._files[path])
        except KeyError as error:
            raise ExtensionDetectionError(f"artifact file is missing: {path}") from error

    def read_text(self, path: str, *, maximum: int = 256 * 1024) -> str:
        raw = self.read_bytes(path)
        if len(raw) > maximum:
            raise ExtensionDetectionError(f"artifact text file exceeds its parser budget: {path}")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeError as error:
            raise ExtensionDetectionError(f"artifact text file is not UTF-8: {path}") from error
        if "\x00" in text:
            raise ExtensionDetectionError(f"artifact text file contains NUL: {path}")
        return text


@dataclass(frozen=True, slots=True)
class Detection:
    source_format: str
    marker_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "marker_paths", tuple(self.marker_paths))
        if self.source_format not in _FORMATS:
            raise ExtensionDetectionError("detected source format is invalid")
        if not self.marker_paths:
            raise ExtensionDetectionError("detection marker paths are required")


class StaticExtensionDetectorRegistry:
    """Core-owned marker registry with explicit container precedence."""

    def detect_exactly_one(self, inventory: ArtifactInventory) -> Detection:
        container_matches = _container_matches(inventory)
        if len(container_matches) > 1:
            raise ExtensionDetectionError(
                "ambiguous extension container formats: " + ", ".join(sorted(container_matches))
            )
        if container_matches:
            source_format, markers = next(iter(container_matches.items()))
            return Detection(source_format, markers)
        leaf_matches = _leaf_matches(inventory)
        if not leaf_matches:
            raise ExtensionDetectionError("unknown extension format")
        if len(leaf_matches) > 1:
            raise ExtensionDetectionError(
                "ambiguous extension formats: " + ", ".join(sorted(leaf_matches))
            )
        source_format, markers = next(iter(leaf_matches.items()))
        return Detection(source_format, markers)


def _container_matches(inventory: ArtifactInventory) -> dict[str, tuple[str, ...]]:
    matches: dict[str, tuple[str, ...]] = {}
    if inventory.contains(".codex-plugin/plugin.json"):
        matches["openai_codex_plugin"] = (".codex-plugin/plugin.json",)
    if inventory.contains("package.json") and _looks_like_dsh_plugin(inventory):
        matches["deepseek_harness_plugin"] = ("package.json",)
    if inventory.contains(".agents/plugins/marketplace.json"):
        matches["codex_marketplace"] = (".agents/plugins/marketplace.json",)
    return matches


def _leaf_matches(inventory: ArtifactInventory) -> dict[str, tuple[str, ...]]:
    matches: dict[str, tuple[str, ...]] = {}
    codex_skill_paths = tuple(
        path
        for path in inventory.paths
        if path == "SKILL.md"
        or re.fullmatch(
            r"(?:\.agents/skills|\.codex/skills|skills)/[a-z0-9][a-z0-9-]{0,62}/SKILL\.md",
            path,
        )
    )
    dsh_skill_paths = tuple(
        path
        for path in inventory.paths
        if re.fullmatch(r"\.dsh/skills/[a-z0-9][a-z0-9-]{0,62}(?:/SKILL\.md|\.md)", path)
    )
    if codex_skill_paths:
        matches["openai_agent_skill"] = codex_skill_paths
    if dsh_skill_paths:
        matches["deepseek_harness_skill"] = dsh_skill_paths
    hook_paths = tuple(path for path in (".codex/hooks.json", "hooks/hooks.json", "hooks.json") if inventory.contains(path))
    if hook_paths:
        matches["codex_hook_config"] = hook_paths
    mcp_paths = tuple(path for path in (".mcp.json", "mcp.json", ".codex/config.toml") if inventory.contains(path))
    if mcp_paths:
        matches["mcp_config"] = mcp_paths
    return matches


def _looks_like_dsh_plugin(inventory: ArtifactInventory) -> bool:
    text = inventory.read_text("package.json", maximum=256 * 1024)
    return bool(re.search(r'"dsh"\s*:', text) and re.search(r'"bundle"\s*:', text))


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ExtensionDetectionError("artifact path is invalid")
    if "\\" in value or "\x00" in value or ":" in value:
        raise ExtensionDetectionError("artifact path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."}
        or part.rstrip(" .") != part
        or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        for part in path.parts
    ):
        raise ExtensionDetectionError("artifact path is unsafe")
    return path.as_posix()


def _capture_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(files, Mapping) or not files or len(files) > _MAX_FILES:
        raise ExtensionDetectionError("artifact file count is invalid")
    captured: dict[str, bytes] = {}
    total = 0
    for raw_path, raw_content in sorted(files.items()):
        path = _safe_path(raw_path)
        if path in captured:
            raise ExtensionDetectionError("artifact paths must be unique")
        if not isinstance(raw_content, bytes) or len(raw_content) > _MAX_FILE_BYTES:
            raise ExtensionDetectionError("artifact file bytes are invalid")
        total += len(raw_content)
        if total > _MAX_TOTAL_BYTES:
            raise ExtensionDetectionError("artifact exceeds the total byte limit")
        captured[path] = bytes(raw_content)
    return captured
