from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort

from .media_processing_queue import MediaOcrAdapterResult


class LocalOcrProviderError(ValueError):
    """Raised when a local OCR provider cannot read an authorized image reference."""


@dataclass(frozen=True, slots=True)
class LocalCommandImageOcrAdapter:
    """OCR adapter backed by an explicitly configured local command.

    The command may be a real OCR executable such as:
    ("tesseract", "{image_path}", "stdout")
    """

    object_store: ObjectStorePort
    command: Sequence[str]
    enabled: bool = False
    provider_name: str = "local-command-ocr"
    timeout_seconds: float = 30.0

    def extract_text(
        self,
        *,
        source: Mapping[str, object],
        job: Mapping[str, object],
    ) -> MediaOcrAdapterResult:
        if self.enabled is not True:
            raise LocalOcrProviderError("local OCR provider is disabled")
        if self.timeout_seconds <= 0:
            raise LocalOcrProviderError("local OCR provider timeout must be positive")
        if source.get("type") != "image":
            raise LocalOcrProviderError("local OCR provider requires image Source")
        if job.get("required_capability") != "ocr":
            raise LocalOcrProviderError("local OCR provider requires ocr job")
        image_path, authorization = self._authorized_image_path(source)
        command = _command_for_image(self.command, image_path)
        output_encoding = "utf-8" if self.provider_name == "builtin-windows-ocr" else None
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding=output_encoding,
                errors="strict",
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise LocalOcrProviderError("local OCR provider executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise LocalOcrProviderError("local OCR provider timed out") from exc
        except UnicodeDecodeError as exc:
            raise LocalOcrProviderError("local OCR provider output cannot be decoded") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "local OCR provider failed").strip()
            raise LocalOcrProviderError(
                _safe_provider_error(detail, provider_name=self.provider_name)
            )
        text = completed.stdout.strip()
        if not text:
            raise LocalOcrProviderError("local OCR provider returned empty text")
        return MediaOcrAdapterResult(
            text=text,
            provider=self.provider_name,
            confidence=None,
            metadata={
                "local_processing": True,
                "remote_processing": False,
                "image_reference": _required_str(authorization, "image_reference"),
                "authorization_id": _required_str(authorization, "id"),
                "path_stored_in_output": False,
            },
        )

    def _authorized_image_path(self, source: Mapping[str, object]) -> tuple[Path, Mapping[str, object]]:
        source_id = _required_str(source, "id")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise LocalOcrProviderError("image Source metadata is required")
        image_reference = _required_str(metadata, "image_reference")
        authorization_id = None
        image_authorization = metadata.get("image_authorization")
        if isinstance(image_authorization, Mapping):
            authorization_id = _optional_str(image_authorization.get("authorization_id"))
        authorization = (
            self.object_store.read("authorized_file_refs", authorization_id)
            if authorization_id is not None
            else None
        )
        if authorization is None:
            authorization = _find_authorization(
                self.object_store.list("authorized_file_refs"),
                source_id=source_id,
                image_reference=image_reference,
            )
        if authorization is None:
            raise LocalOcrProviderError("authorized image reference not found")
        if authorization.get("status") != "authorized":
            raise LocalOcrProviderError("authorized image reference is not authorized")
        if authorization.get("source_id") != source_id or authorization.get("image_reference") != image_reference:
            raise LocalOcrProviderError("authorized image reference does not match source")
        path = Path(_required_str(authorization, "path")).expanduser().resolve(strict=False)
        if not path.exists():
            raise LocalOcrProviderError("authorized image path does not exist")
        if not path.is_file():
            raise LocalOcrProviderError("authorized image path is not a file")
        return path, authorization


def _find_authorization(
    records: Sequence[Mapping[str, object]],
    *,
    source_id: str,
    image_reference: str,
) -> Mapping[str, object] | None:
    for record in records:
        if record.get("source_id") == source_id and record.get("image_reference") == image_reference:
            return dict(record)
    return None


def _command_for_image(command: Sequence[str], image_path: Path) -> tuple[str, ...]:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        raise LocalOcrProviderError("local OCR provider command is not configured")
    replaced: list[str] = []
    has_placeholder = False
    for part in command:
        if not isinstance(part, str) or not part:
            raise LocalOcrProviderError("local OCR provider command parts must be non-empty strings")
        if "{image_path}" in part:
            has_placeholder = True
            replaced.append(part.replace("{image_path}", str(image_path)))
        else:
            replaced.append(part)
    if not has_placeholder:
        replaced.append(str(image_path))
    return tuple(replaced)


def _preview_error(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _safe_provider_error(text: str, *, provider_name: str) -> str:
    """Keep built-in OCR diagnostics useful without persisting OS paths.

    PowerShell decorates a thrown WinRT error with the absolute script path and
    source line. That stderr is implementation detail and may expose the local
    install directory through the Job/API/UI. Built-in failures therefore use
    a small stable vocabulary; custom developer commands retain their bounded
    diagnostic for compatibility.
    """

    if provider_name == "builtin-windows-ocr":
        if "Windows OCR returned no text" in text:
            return "Windows OCR returned no text"
        if "Windows OCR language pack is unavailable" in text:
            return "Windows OCR language pack is unavailable"
        if "authorized image does not exist" in text:
            return "authorized image does not exist"
        return "Windows OCR failed to decode or recognize image"
    return _preview_error(text)


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LocalOcrProviderError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
