from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from core.storage_provider import ObjectStorePort

from .ports import SourceSubmission


@dataclass(slots=True)
class DeterministicSourceRegistrar:
    """Registers inline text Source objects without touching the real library."""

    namespace_id: str = "default"
    project_id: str = "default"
    created_at: str = "2026-06-29T17:00:00+08:00"
    _sources: dict[str, dict[str, object]] = field(default_factory=dict)

    def register(self, submission: SourceSubmission) -> Mapping[str, object]:
        source = _build_inline_source(
            submission,
            namespace_id=self.namespace_id,
            project_id=self.project_id,
            created_at=self.created_at,
        )
        source_id = _required_id(source)
        self._sources[source_id] = source
        return dict(source)

    def get(self, source_id: str) -> Mapping[str, object] | None:
        source = self._sources.get(source_id)
        if source is None:
            return None
        return dict(source)


@dataclass(slots=True)
class ObjectStoreSourceRegistrar:
    """Registers text Source objects into the rebuild ObjectStore."""

    object_store: ObjectStorePort
    namespace_id: str = "default"
    project_id: str = "default"
    created_at: str = "2026-06-29T17:00:00+08:00"

    def register(self, submission: SourceSubmission) -> Mapping[str, object]:
        source = _build_inline_source(
            submission,
            namespace_id=self.namespace_id,
            project_id=self.project_id,
            created_at=self.created_at,
        )
        source_id = _required_id(source)
        self.object_store.write("sources", source_id, source, expected_revision=None)
        return dict(source)

    def get(self, source_id: str) -> Mapping[str, object] | None:
        return self.object_store.read("sources", source_id)


def _build_inline_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    project_id: str = "default",
    created_at: str,
) -> dict[str, object]:
    source = _build_source_payload(submission, namespace_id=namespace_id, created_at=created_at)
    if project_id != "default":
        # Preserve every established default Source id while giving identical
        # material in two projects separate durable identities.
        source_id = f"{source['id']}-p{hashlib.sha256(project_id.encode('utf-8')).hexdigest()[:8]}"
        source["id"] = source_id
        source["storage_uri"] = f"crp://{namespace_id}/sources/{source_id}"
    source["project_id"] = project_id
    return source


def _build_source_payload(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    if submission.kind == "link":
        return _build_link_source(
            submission,
            namespace_id=namespace_id,
            created_at=created_at,
        )
    if submission.kind == "collection":
        return _build_collection_source(
            submission,
            namespace_id=namespace_id,
            created_at=created_at,
        )
    if submission.kind == "file":
        return _build_file_source(
            submission,
            namespace_id=namespace_id,
            created_at=created_at,
        )
    if submission.kind == "image":
        return _build_image_source(
            submission,
            namespace_id=namespace_id,
            created_at=created_at,
        )
    if submission.kind == "audio":
        return _build_audio_source(
            submission,
            namespace_id=namespace_id,
            created_at=created_at,
        )
    if submission.kind == "video":
        return _build_video_source(
            submission,
            namespace_id=namespace_id,
            created_at=created_at,
        )
    if submission.kind not in {"text", "question"}:
        raise ValueError("minimum runtime loop only supports text, question, link, collection, file, image, audio and video sources")
    if not submission.content:
        raise ValueError("text source requires content")
    encoded = submission.content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-{submission.kind}-{digest[:12]}"
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": submission.kind,
        "title": submission.title,
        "capture_mode": "inline",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": None,
        "content_hash": digest,
        "media_type": "text/plain",
        "size_bytes": len(encoded),
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "encoding": "utf-8",
            "content": submission.content,
        },
    }


def _build_link_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    original_url = _normalized_http_url(submission.original_url)
    encoded = original_url.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-link-{digest[:12]}"
    title = submission.title.strip() or original_url
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": "link",
        "title": title,
        "capture_mode": "reference",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": original_url,
        "content_hash": digest,
        "media_type": "text/uri-list",
        "size_bytes": len(encoded),
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "url": original_url,
            "remote_fetch": "not_performed",
            "content_snapshot": None,
        },
    }


def _build_collection_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    urls = _normalized_collection_urls(submission.collection_urls)
    encoded = "\n".join(urls).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-collection-{digest[:12]}"
    title = submission.title.strip() or "收藏夹"
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": "collection",
        "title": title,
        "capture_mode": "reference_batch",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": None,
        "content_hash": digest,
        "media_type": "application/vnd.chriptmas.bookmark-collection+json",
        "size_bytes": len(encoded),
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "collection_type": "bookmark_collection",
            "item_count": len(urls),
            "urls": list(urls),
            "content_hash_basis": "normalized_url_list_not_remote_content",
            "remote_fetch": "not_performed",
            "content_snapshot": None,
        },
    }


def _build_file_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    display_name = _required_display_name(submission.display_name)
    media_type = _required_media_type(submission.media_type)
    size_bytes = _required_size_bytes(submission.size_bytes)
    file_reference = _required_file_reference(submission.file_reference)
    encoded = f"{display_name}\n{media_type}\n{size_bytes}\n{file_reference}".encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-file-{digest[:12]}"
    title = submission.title.strip() or display_name
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": "file",
        "title": title,
        "capture_mode": "reference",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": None,
        "content_hash": digest,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "display_name": display_name,
            "file_reference": file_reference,
            "content_hash_basis": "file_reference_metadata_not_content",
            "content_snapshot": None,
            "parser": "not_started",
            "file_content_read": False,
        },
    }


def _build_image_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    display_name = _required_display_name(submission.display_name)
    media_type = _required_image_media_type(submission.media_type)
    size_bytes = _required_size_bytes(submission.size_bytes)
    image_reference = _required_image_reference(submission.image_reference)
    width_px = _optional_positive_int(submission.width_px, field_name="width_px")
    height_px = _optional_positive_int(submission.height_px, field_name="height_px")
    encoded = (
        f"{display_name}\n{media_type}\n{size_bytes}\n{image_reference}\n"
        f"{width_px or ''}\n{height_px or ''}"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-image-{digest[:12]}"
    title = submission.title.strip() or display_name
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": "image",
        "title": title,
        "capture_mode": "reference",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": None,
        "content_hash": digest,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "display_name": display_name,
            "image_reference": image_reference,
            "content_hash_basis": "image_reference_metadata_not_binary",
            "content_snapshot": None,
            "image_bytes_read": False,
            "thumbnail_generated": False,
            "ocr": "disabled",
            "extractor": "disabled",
            "width_px": width_px,
            "height_px": height_px,
        },
    }


def _build_audio_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    display_name = _required_display_name(submission.display_name)
    media_type = _required_audio_media_type(submission.media_type)
    size_bytes = _required_size_bytes(submission.size_bytes)
    audio_reference = _required_audio_reference(submission.audio_reference)
    duration_ms = _optional_positive_int(submission.duration_ms, field_name="duration_ms")
    encoded = (
        f"{display_name}\n{media_type}\n{size_bytes}\n{audio_reference}\n{duration_ms or ''}"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-audio-{digest[:12]}"
    title = submission.title.strip() or display_name
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": "audio",
        "title": title,
        "capture_mode": "reference",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": None,
        "content_hash": digest,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "display_name": display_name,
            "audio_reference": audio_reference,
            "content_hash_basis": "audio_reference_metadata_not_binary",
            "content_snapshot": None,
            "audio_bytes_read": False,
            "transcription": "disabled",
            "waveform_generated": False,
            "remote_processing": "not_performed",
            "duration_ms": duration_ms,
        },
    }


def _build_video_source(
    submission: SourceSubmission,
    *,
    namespace_id: str,
    created_at: str,
) -> dict[str, object]:
    display_name = _required_display_name(submission.display_name)
    media_type = _required_video_media_type(submission.media_type)
    size_bytes = _required_size_bytes(submission.size_bytes)
    video_reference = _required_video_reference(submission.video_reference)
    duration_ms = _optional_positive_int(submission.duration_ms, field_name="duration_ms")
    width_px = _optional_positive_int(submission.width_px, field_name="width_px")
    height_px = _optional_positive_int(submission.height_px, field_name="height_px")
    encoded = (
        f"{display_name}\n{media_type}\n{size_bytes}\n{video_reference}\n"
        f"{duration_ms or ''}\n{width_px or ''}\n{height_px or ''}"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    source_id = f"source-video-{digest[:12]}"
    title = submission.title.strip() or display_name
    return {
        "schema_version": "1.1.0",
        "id": source_id,
        "type": "video",
        "title": title,
        "capture_mode": "reference",
        "storage_uri": f"crp://{namespace_id}/sources/{source_id}",
        "original_url": None,
        "content_hash": digest,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "parser_version": None,
        "processing_state": "captured",
        "created_at": created_at,
        **_local_selection_temporal_fields(created_at),
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {
            "display_name": display_name,
            "video_reference": video_reference,
            "content_hash_basis": "video_reference_metadata_not_binary",
            "content_snapshot": None,
            "video_bytes_read": False,
            "frame_extraction": "disabled",
            "audio_track_extraction": "disabled",
            "thumbnail_generated": False,
            "remote_processing": "not_performed",
            "duration_ms": duration_ms,
            "width_px": width_px,
            "height_px": height_px,
        },
    }


def _normalized_http_url(raw_url: str | None) -> str:
    if raw_url is None:
        raise ValueError("link source requires url")
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("link source requires http or https url")
    return url


def _local_selection_temporal_fields(recorded_at: str) -> dict[str, str]:
    """Local selection has no separate remote event clock.

    The user selected the material at the moment the local system recorded it,
    so the two timestamps intentionally have the same value.  Validation here
    prevents this convenience path from emitting an unparseable contract.
    """
    datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    return {"occurred_at": recorded_at, "recorded_at": recorded_at}


def _normalized_collection_urls(raw_urls: tuple[str, ...] | None) -> tuple[str, ...]:
    if not raw_urls:
        raise ValueError("collection source requires at least one url")
    urls: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        url = _normalized_http_url(raw_url)
        if url not in seen:
            urls.append(url)
            seen.add(url)
    if not urls:
        raise ValueError("collection source requires at least one url")
    return tuple(urls)


def _required_display_name(display_name: str | None) -> str:
    if display_name is None:
        raise ValueError("file source requires display name")
    clean_display_name = display_name.strip()
    if not clean_display_name:
        raise ValueError("file source requires display name")
    if "\\" in clean_display_name or "/" in clean_display_name or ":" in clean_display_name:
        raise ValueError("file source display name must not include an OS path")
    return clean_display_name


def _required_media_type(media_type: str | None) -> str:
    if media_type is None:
        raise ValueError("file source requires media type")
    clean_media_type = media_type.strip().lower()
    if "/" not in clean_media_type or "\\" in clean_media_type:
        raise ValueError("file source requires media type")
    return clean_media_type


def _required_image_media_type(media_type: str | None) -> str:
    clean_media_type = _required_media_type(media_type)
    if not clean_media_type.startswith("image/"):
        raise ValueError("image source requires image media type")
    return clean_media_type


def _required_size_bytes(size_bytes: int | None) -> int:
    if not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("file source requires non-negative size bytes")
    return size_bytes


def _required_file_reference(file_reference: str | None) -> str:
    if file_reference is None:
        raise ValueError("file source requires platform file reference")
    clean_reference = file_reference.strip()
    if not clean_reference:
        raise ValueError("file source requires platform file reference")
    if ":" in clean_reference or "\\" in clean_reference or clean_reference.startswith("/"):
        raise ValueError("file source reference must be platform-neutral")
    return clean_reference


def _required_image_reference(image_reference: str | None) -> str:
    if image_reference is None:
        raise ValueError("image source requires platform image reference")
    clean_reference = image_reference.strip()
    if not clean_reference:
        raise ValueError("image source requires platform image reference")
    if ":" in clean_reference or "\\" in clean_reference or clean_reference.startswith("/"):
        raise ValueError("image source reference must be platform-neutral")
    return clean_reference


def _required_audio_reference(audio_reference: str | None) -> str:
    if audio_reference is None:
        raise ValueError("audio source requires platform audio reference")
    clean_reference = audio_reference.strip()
    if not clean_reference:
        raise ValueError("audio source requires platform audio reference")
    if ":" in clean_reference or "\\" in clean_reference or clean_reference.startswith("/"):
        raise ValueError("audio source reference must be platform-neutral")
    return clean_reference


def _required_video_reference(video_reference: str | None) -> str:
    if video_reference is None:
        raise ValueError("video source requires platform video reference")
    clean_reference = video_reference.strip()
    if not clean_reference:
        raise ValueError("video source requires platform video reference")
    if ":" in clean_reference or "\\" in clean_reference or clean_reference.startswith("/"):
        raise ValueError("video source reference must be platform-neutral")
    return clean_reference


def _required_audio_media_type(media_type: str | None) -> str:
    clean_type = _required_media_type(media_type)
    if not clean_type.startswith("audio/"):
        raise ValueError("audio source requires audio media type")
    return clean_type


def _required_video_media_type(media_type: str | None) -> str:
    clean_type = _required_media_type(media_type)
    if not clean_type.startswith("video/"):
        raise ValueError("video source requires video media type")
    return clean_type


def _optional_positive_int(value: int | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"image source requires positive {field_name}")
    return value


def _required_id(source: Mapping[str, object]) -> str:
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source requires id")
    return source_id
