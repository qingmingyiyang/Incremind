from __future__ import annotations

from pathlib import Path

from core.composition import ObjectStoreLibraryOverviewReader
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    AuthorizeLocalAudioFileForSource,
    AuthorizeLocalImageFileForSource,
    AuthorizeLocalVideoFileForSource,
    GetLibraryOverview,
    ServeSourceFileAuthorizationEndpoint,
    serialize_library_overview,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_source_file_authorization_endpoint_accepts_image_audio_and_video(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    registrar = ObjectStoreSourceRegistrar(object_store)
    endpoint = ServeSourceFileAuthorizationEndpoint()
    image_file = tmp_path / "whiteboard.png"
    audio_file = tmp_path / "meeting.wav"
    video_file = tmp_path / "walkthrough.mp4"
    image_file.write_bytes(b"image")
    audio_file.write_bytes(b"audio")
    video_file.write_bytes(b"video")
    image = registrar.register(
        SourceSubmission(
            kind="image",
            title="Image",
            display_name="whiteboard.png",
            media_type="image/png",
            size_bytes=image_file.stat().st_size,
            image_reference="platform-image-ref",
            width_px=640,
            height_px=480,
        )
    )
    audio = registrar.register(
        SourceSubmission(
            kind="audio",
            title="Audio",
            display_name="meeting.wav",
            media_type="audio/wav",
            size_bytes=audio_file.stat().st_size,
            audio_reference="platform-audio-ref",
            duration_ms=1200,
        )
    )
    video = registrar.register(
        SourceSubmission(
            kind="video",
            title="Video",
            display_name="walkthrough.mp4",
            media_type="video/mp4",
            size_bytes=video_file.stat().st_size,
            video_reference="platform-video-ref",
            duration_ms=2300,
            width_px=1280,
            height_px=720,
        )
    )

    image_response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{image['id']}/file-authorization",
        body={"file_path": str(image_file)},
        authorize_file=AuthorizeLocalImageFileForSource(object_store).execute,
    )
    audio_response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{audio['id']}/file-authorization",
        body={"file_path": str(audio_file)},
        authorize_file=AuthorizeLocalAudioFileForSource(object_store).execute,
    )
    video_response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{video['id']}/file-authorization",
        body={"file_path": str(video_file)},
        authorize_file=AuthorizeLocalVideoFileForSource(object_store).execute,
    )

    assert image_response.status_code == 200
    assert image_response.body["authorization_ref"] == (
        f"crp://default/authorized-images/authorized-image-{image['id']}.json"
    )
    assert audio_response.status_code == 200
    assert audio_response.body["authorization_ref"] == (
        f"crp://default/authorized-audio/authorized-audio-{audio['id']}.json"
    )
    assert video_response.status_code == 200
    assert video_response.body["authorization_ref"] == (
        f"crp://default/authorized-video/authorized-video-{video['id']}.json"
    )


def test_library_overview_includes_media_authorization_refs(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    image_file = tmp_path / "whiteboard.png"
    image_file.write_bytes(b"image")
    image = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="image",
            title="Authorized image",
            display_name="whiteboard.png",
            media_type="image/png",
            size_bytes=image_file.stat().st_size,
            image_reference="platform-image-ref",
            width_px=640,
            height_px=480,
        )
    )
    authorization = AuthorizeLocalImageFileForSource(object_store).execute(
        source_id=str(image["id"]),
        file_path=str(image_file),
    )

    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == image["id"])

    assert authorization.authorization_ref in source_item["trace_refs"]
