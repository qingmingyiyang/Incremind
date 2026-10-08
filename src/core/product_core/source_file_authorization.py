from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort


class SourceFileAuthorizationError(ValueError):
    """Raised when a local file cannot be authorized for a Source."""


@dataclass(frozen=True, slots=True)
class SourceFileAuthorizationResult:
    status: str
    source_id: str
    authorization_id: str
    authorization_ref: str
    file_reference: str
    media_type: str
    size_bytes: int


class AuthorizeLocalTextFileForSource:
    """Create a local file authorization record without storing OS paths in Source."""

    _ALLOWED_MEDIA_TYPES = frozenset({"text/plain", "text/markdown", "application/json"})

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T17:00:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str, file_path: str) -> SourceFileAuthorizationResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceFileAuthorizationError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceFileAuthorizationError("source not found")
        if source.get("type") != "file":
            raise SourceFileAuthorizationError("local file authorization requires file Source")

        media_type = _required_str(source, "media_type")
        if media_type not in self._ALLOWED_MEDIA_TYPES:
            raise SourceFileAuthorizationError(f"unsupported media_type for text file read: {media_type}")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SourceFileAuthorizationError("source metadata is required")
        file_reference = _required_str(metadata, "file_reference")
        path = Path(file_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceFileAuthorizationError("authorized file does not exist")
        if not path.is_file():
            raise SourceFileAuthorizationError("authorized path is not a file")

        authorization_id = f"authorized-file-{clean_source_id}"
        authorization_ref = self._authorization_uri(authorization_id)
        size_bytes = path.stat().st_size
        record = {
            "schema_version": "1.0.0",
            "id": authorization_id,
            "source_id": clean_source_id,
            "file_reference": file_reference,
            "path": str(path),
            "media_type": media_type,
            "size_bytes": size_bytes,
            "status": "authorized",
            "authorized_at": self._now,
            "path_scope": "local_user_authorized_file",
        }
        self._object_store.write("authorized_file_refs", authorization_id, record, expected_revision=None)
        self._update_source_authorization(
            source,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=file_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )
        return SourceFileAuthorizationResult(
            status="authorized",
            source_id=clean_source_id,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=file_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )

    def _update_source_authorization(
        self,
        source: Mapping[str, object],
        *,
        authorization_id: str,
        authorization_ref: str,
        file_reference: str,
        media_type: str,
        size_bytes: int,
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["file_authorization"] = {
            "status": "authorized",
            "authorization_id": authorization_id,
            "authorization_ref": authorization_ref,
            "file_reference": file_reference,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "authorized_at": self._now,
            "path_stored_in_source": False,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _authorization_uri(self, authorization_id: str) -> str:
        return f"crp://{self._namespace_id}/authorized-files/{authorization_id}.json"


class AuthorizeLocalDocumentFileForSource:
    """Create a local PDF / Word authorization record without storing OS paths in Source."""

    _ALLOWED_MEDIA_TYPES = frozenset(
        {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T23:10:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str, file_path: str) -> SourceFileAuthorizationResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceFileAuthorizationError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceFileAuthorizationError("source not found")
        if source.get("type") != "file":
            raise SourceFileAuthorizationError("local document authorization requires file Source")

        media_type = _required_str(source, "media_type")
        if media_type not in self._ALLOWED_MEDIA_TYPES:
            raise SourceFileAuthorizationError(f"unsupported media_type for document text extraction: {media_type}")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SourceFileAuthorizationError("source metadata is required")
        file_reference = _required_str(metadata, "file_reference")
        path = Path(file_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceFileAuthorizationError("authorized document does not exist")
        if not path.is_file():
            raise SourceFileAuthorizationError("authorized document path is not a file")

        authorization_id = f"authorized-document-{clean_source_id}"
        authorization_ref = self._authorization_uri(authorization_id)
        size_bytes = path.stat().st_size
        record = {
            "schema_version": "1.0.0",
            "id": authorization_id,
            "source_id": clean_source_id,
            "file_reference": file_reference,
            "path": str(path),
            "media_type": media_type,
            "size_bytes": size_bytes,
            "status": "authorized",
            "authorized_at": self._now,
            "path_scope": "local_user_authorized_document",
            "content_extraction": "requires_document_text_extractor",
        }
        self._object_store.write("authorized_file_refs", authorization_id, record, expected_revision=None)
        self._update_source_authorization(
            source,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=file_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )
        return SourceFileAuthorizationResult(
            status="authorized",
            source_id=clean_source_id,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=file_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )

    def _update_source_authorization(
        self,
        source: Mapping[str, object],
        *,
        authorization_id: str,
        authorization_ref: str,
        file_reference: str,
        media_type: str,
        size_bytes: int,
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["document_authorization"] = {
            "status": "authorized",
            "authorization_id": authorization_id,
            "authorization_ref": authorization_ref,
            "file_reference": file_reference,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "authorized_at": self._now,
            "path_stored_in_source": False,
            "content_extraction": "requires_document_text_extractor",
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _authorization_uri(self, authorization_id: str) -> str:
        return f"crp://{self._namespace_id}/authorized-documents/{authorization_id}.json"


class AuthorizeLocalImageFileForSource:
    """Create a local image authorization record without storing OS paths in Source."""

    _ALLOWED_MEDIA_TYPES = frozenset(
        {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/bmp",
            "image/tiff",
        }
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T20:35:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str, file_path: str) -> SourceFileAuthorizationResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceFileAuthorizationError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceFileAuthorizationError("source not found")
        if source.get("type") != "image":
            raise SourceFileAuthorizationError("local image authorization requires image Source")

        media_type = _required_str(source, "media_type")
        if media_type not in self._ALLOWED_MEDIA_TYPES:
            raise SourceFileAuthorizationError(f"unsupported media_type for image OCR: {media_type}")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SourceFileAuthorizationError("source metadata is required")
        image_reference = _required_str(metadata, "image_reference")
        path = Path(file_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceFileAuthorizationError("authorized image does not exist")
        if not path.is_file():
            raise SourceFileAuthorizationError("authorized image path is not a file")

        authorization_id = f"authorized-image-{clean_source_id}"
        authorization_ref = self._authorization_uri(authorization_id)
        size_bytes = path.stat().st_size
        record = {
            "schema_version": "1.0.0",
            "id": authorization_id,
            "source_id": clean_source_id,
            "image_reference": image_reference,
            "path": str(path),
            "media_type": media_type,
            "size_bytes": size_bytes,
            "status": "authorized",
            "authorized_at": self._now,
            "path_scope": "local_user_authorized_image",
        }
        self._object_store.write("authorized_file_refs", authorization_id, record, expected_revision=None)
        self._update_source_authorization(
            source,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            image_reference=image_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )
        return SourceFileAuthorizationResult(
            status="authorized",
            source_id=clean_source_id,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=image_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )

    def _update_source_authorization(
        self,
        source: Mapping[str, object],
        *,
        authorization_id: str,
        authorization_ref: str,
        image_reference: str,
        media_type: str,
        size_bytes: int,
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["image_authorization"] = {
            "status": "authorized",
            "authorization_id": authorization_id,
            "authorization_ref": authorization_ref,
            "image_reference": image_reference,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "authorized_at": self._now,
            "path_stored_in_source": False,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _authorization_uri(self, authorization_id: str) -> str:
        return f"crp://{self._namespace_id}/authorized-images/{authorization_id}.json"


class AuthorizeLocalAudioFileForSource:
    """Create a local audio authorization record without storing OS paths in Source."""

    _ALLOWED_MEDIA_TYPES = frozenset(
        {
            "audio/aac",
            "audio/flac",
            "audio/m4a",
            "audio/mp4",
            "audio/mpeg",
            "audio/ogg",
            "audio/wav",
            "audio/webm",
            "audio/x-m4a",
        }
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T21:45:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str, file_path: str) -> SourceFileAuthorizationResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceFileAuthorizationError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceFileAuthorizationError("source not found")
        if source.get("type") != "audio":
            raise SourceFileAuthorizationError("local audio authorization requires audio Source")

        media_type = _required_str(source, "media_type")
        if media_type not in self._ALLOWED_MEDIA_TYPES and not media_type.startswith("audio/"):
            raise SourceFileAuthorizationError(f"unsupported media_type for audio transcription: {media_type}")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SourceFileAuthorizationError("source metadata is required")
        audio_reference = _required_str(metadata, "audio_reference")
        path = Path(file_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceFileAuthorizationError("authorized audio does not exist")
        if not path.is_file():
            raise SourceFileAuthorizationError("authorized audio path is not a file")

        authorization_id = f"authorized-audio-{clean_source_id}"
        authorization_ref = self._authorization_uri(authorization_id)
        size_bytes = path.stat().st_size
        record = {
            "schema_version": "1.0.0",
            "id": authorization_id,
            "source_id": clean_source_id,
            "audio_reference": audio_reference,
            "path": str(path),
            "media_type": media_type,
            "size_bytes": size_bytes,
            "status": "authorized",
            "authorized_at": self._now,
            "path_scope": "local_user_authorized_audio",
        }
        self._object_store.write("authorized_file_refs", authorization_id, record, expected_revision=None)
        self._update_source_authorization(
            source,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            audio_reference=audio_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )
        return SourceFileAuthorizationResult(
            status="authorized",
            source_id=clean_source_id,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=audio_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )

    def _update_source_authorization(
        self,
        source: Mapping[str, object],
        *,
        authorization_id: str,
        authorization_ref: str,
        audio_reference: str,
        media_type: str,
        size_bytes: int,
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["audio_authorization"] = {
            "status": "authorized",
            "authorization_id": authorization_id,
            "authorization_ref": authorization_ref,
            "audio_reference": audio_reference,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "authorized_at": self._now,
            "path_stored_in_source": False,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _authorization_uri(self, authorization_id: str) -> str:
        return f"crp://{self._namespace_id}/authorized-audio/{authorization_id}.json"


class AuthorizeLocalVideoFileForSource:
    """Create a local video authorization record without storing OS paths in Source."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T22:20:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str, file_path: str) -> SourceFileAuthorizationResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceFileAuthorizationError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceFileAuthorizationError("source not found")
        if source.get("type") != "video":
            raise SourceFileAuthorizationError("local video authorization requires video Source")

        media_type = _required_str(source, "media_type")
        if not media_type.startswith("video/"):
            raise SourceFileAuthorizationError(f"unsupported media_type for video frame extraction: {media_type}")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise SourceFileAuthorizationError("source metadata is required")
        video_reference = _required_str(metadata, "video_reference")
        path = Path(file_path).expanduser().resolve(strict=False)
        if not path.exists():
            raise SourceFileAuthorizationError("authorized video does not exist")
        if not path.is_file():
            raise SourceFileAuthorizationError("authorized video path is not a file")

        authorization_id = f"authorized-video-{clean_source_id}"
        authorization_ref = self._authorization_uri(authorization_id)
        size_bytes = path.stat().st_size
        record = {
            "schema_version": "1.0.0",
            "id": authorization_id,
            "source_id": clean_source_id,
            "video_reference": video_reference,
            "path": str(path),
            "media_type": media_type,
            "size_bytes": size_bytes,
            "status": "authorized",
            "authorized_at": self._now,
            "path_scope": "local_user_authorized_video",
        }
        self._object_store.write("authorized_file_refs", authorization_id, record, expected_revision=None)
        self._update_source_authorization(
            source,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            video_reference=video_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )
        return SourceFileAuthorizationResult(
            status="authorized",
            source_id=clean_source_id,
            authorization_id=authorization_id,
            authorization_ref=authorization_ref,
            file_reference=video_reference,
            media_type=media_type,
            size_bytes=size_bytes,
        )

    def _update_source_authorization(
        self,
        source: Mapping[str, object],
        *,
        authorization_id: str,
        authorization_ref: str,
        video_reference: str,
        media_type: str,
        size_bytes: int,
    ) -> None:
        source_id = _required_str(source, "id")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        metadata["video_authorization"] = {
            "status": "authorized",
            "authorization_id": authorization_id,
            "authorization_ref": authorization_ref,
            "video_reference": video_reference,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "authorized_at": self._now,
            "path_stored_in_source": False,
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", source_id, updated, expected_revision=None)

    def _authorization_uri(self, authorization_id: str) -> str:
        return f"crp://{self._namespace_id}/authorized-video/{authorization_id}.json"


def serialize_source_file_authorization_result(
    result: SourceFileAuthorizationResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "authorization_id": result.authorization_id,
        "authorization_ref": result.authorization_ref,
        "file_reference": result.file_reference,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
    }


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SourceFileAuthorizationError(f"{key} is required")
    return value
