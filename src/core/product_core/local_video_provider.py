from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort

from .media_processing_queue import MediaFrameExtractionAdapterResult


class LocalVideoProviderError(ValueError):
    """Raised when a local video provider cannot read an authorized video reference."""


@dataclass(frozen=True, slots=True)
class LocalCommandVideoFrameExtractionAdapter:
    """Frame extraction adapter backed by an explicitly configured local command.

    The command should print JSON to stdout:
    {"frame_refs": ["crp-ref://default/assets/video/frame-0001.jpg"], "preview": "..."}
    """

    object_store: ObjectStorePort
    command: Sequence[str]
    enabled: bool = False
    provider_name: str = "local-command-video"
    timeout_seconds: float = 180.0

    def extract_frames(
        self,
        *,
        source: Mapping[str, object],
        job: Mapping[str, object],
    ) -> MediaFrameExtractionAdapterResult:
        if self.enabled is not True:
            raise LocalVideoProviderError("local video provider is disabled")
        if self.timeout_seconds <= 0:
            raise LocalVideoProviderError("local video provider timeout must be positive")
        if source.get("type") != "video":
            raise LocalVideoProviderError("local video provider requires video Source")
        if job.get("required_capability") != "video_frame_extraction":
            raise LocalVideoProviderError("local video provider requires video_frame_extraction job")
        video_path, authorization = self._authorized_video_path(source)
        command = _command_for_video(self.command, video_path)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise LocalVideoProviderError("local video provider executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise LocalVideoProviderError("local video provider timed out") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "local video provider failed").strip()
            raise LocalVideoProviderError(_preview_error(detail))
        payload = _parse_stdout(completed.stdout)
        frame_refs = _required_frame_refs(payload.get("frame_refs"))
        preview = _optional_str(payload.get("preview")) or f"提取 {len(frame_refs)} 个关键帧。"
        return MediaFrameExtractionAdapterResult(
            frame_refs=frame_refs,
            provider=self.provider_name,
            preview=preview,
            metadata={
                "local_processing": True,
                "remote_processing": False,
                "video_reference": _required_str(authorization, "video_reference"),
                "authorization_id": _required_str(authorization, "id"),
                "path_stored_in_output": False,
            },
        )

    def _authorized_video_path(self, source: Mapping[str, object]) -> tuple[Path, Mapping[str, object]]:
        source_id = _required_str(source, "id")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise LocalVideoProviderError("video Source metadata is required")
        video_reference = _required_str(metadata, "video_reference")
        authorization_id = None
        video_authorization = metadata.get("video_authorization")
        if isinstance(video_authorization, Mapping):
            authorization_id = _optional_str(video_authorization.get("authorization_id"))
        authorization = (
            self.object_store.read("authorized_file_refs", authorization_id)
            if authorization_id is not None
            else None
        )
        if authorization is None:
            authorization = _find_authorization(
                self.object_store.list("authorized_file_refs"),
                source_id=source_id,
                video_reference=video_reference,
            )
        if authorization is None:
            raise LocalVideoProviderError("authorized video reference not found")
        if authorization.get("status") != "authorized":
            raise LocalVideoProviderError("authorized video reference is not authorized")
        if authorization.get("source_id") != source_id or authorization.get("video_reference") != video_reference:
            raise LocalVideoProviderError("authorized video reference does not match source")
        path = Path(_required_str(authorization, "path")).expanduser().resolve(strict=False)
        if not path.exists():
            raise LocalVideoProviderError("authorized video path does not exist")
        if not path.is_file():
            raise LocalVideoProviderError("authorized video path is not a file")
        return path, authorization


def _find_authorization(
    records: Sequence[Mapping[str, object]],
    *,
    source_id: str,
    video_reference: str,
) -> Mapping[str, object] | None:
    for record in records:
        if record.get("source_id") == source_id and record.get("video_reference") == video_reference:
            return dict(record)
    return None


def _command_for_video(command: Sequence[str], video_path: Path) -> tuple[str, ...]:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        raise LocalVideoProviderError("local video provider command is not configured")
    replaced: list[str] = []
    has_placeholder = False
    for part in command:
        if not isinstance(part, str) or not part:
            raise LocalVideoProviderError("local video provider command parts must be non-empty strings")
        if "{video_path}" in part:
            has_placeholder = True
            replaced.append(part.replace("{video_path}", str(video_path)))
        else:
            replaced.append(part)
    if not has_placeholder:
        replaced.append(str(video_path))
    return tuple(replaced)


def _parse_stdout(stdout: str) -> Mapping[str, object]:
    text = stdout.strip()
    if not text:
        raise LocalVideoProviderError("local video provider returned empty frame index")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        refs = tuple(line.strip() for line in text.splitlines() if line.strip())
        if not refs:
            raise LocalVideoProviderError("local video provider returned no frame refs")
        return {"frame_refs": refs}
    if not isinstance(payload, Mapping):
        raise LocalVideoProviderError("local video provider JSON output must be an object")
    return payload


def _required_frame_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LocalVideoProviderError("local video provider requires frame_refs")
    refs = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(refs) != len(value) or not refs:
        raise LocalVideoProviderError("local video provider frame_refs must contain non-empty strings")
    return refs


def _preview_error(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LocalVideoProviderError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
