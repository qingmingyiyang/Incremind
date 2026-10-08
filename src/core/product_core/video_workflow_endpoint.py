from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .audio_asset_transcriber import (
    AudioAssetTranscriptionError,
    AudioAssetTranscriptionResult,
    serialize_audio_asset_transcription_result,
)
from .transcript_summary_adapter import (
    TranscriptSummaryError,
    TranscriptSummaryResult,
    serialize_transcript_summary_result,
)
from .downloaded_video_source import (
    DownloadedVideoSourceRegistrationResult,
    serialize_downloaded_video_source_registration,
)
from .video_audio_extractor import (
    VideoAudioExtractionError,
    VideoAudioExtractionResult,
    serialize_video_audio_extraction_result,
)
from .video_link_adapter import (
    AuthorizedBilibiliDownloadResult,
    BilibiliVideoLinkResolver,
    BilibiliDownloaderSettings,
    LinkedVideoDownloadPlanner,
    LinkedVideoDownloadPlan,
    VideoLinkResolution,
    serialize_authorized_bilibili_download_result,
    serialize_linked_video_download_plan,
)
from .workflow_progression import WorkflowDecisionBoundary, decide_workflow_progression


@dataclass(frozen=True, slots=True)
class VideoWorkflowEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeBilibiliVideoDownloadPlanEndpoint:
    """Serve Bilibili link resolution and dry-run download planning without downloading media."""

    endpoint_path = "/api/rebuild/video-links/bilibili/download-plan"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        resolve_link: Callable[..., VideoLinkResolution],
        create_plan: Callable[..., LinkedVideoDownloadPlan],
    ) -> VideoWorkflowEndpointResponse:
        if urlsplit(path).path != self.endpoint_path:
            return self._json_response(404, {"detail": "Bilibili video download plan endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "Bilibili video download plan endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        try:
            url = _required_str(body.get("url"), "url")
            resolution = resolve_link(url=url)
            video_id = _optional_str(body.get("video_id")) or _first_video_id(resolution)
            plan = create_plan(resolution=resolution, video_id=video_id)
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "Bilibili video download plan rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_linked_video_download_plan(plan))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> VideoWorkflowEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return VideoWorkflowEndpointResponse(status_code=status_code, body=body, headers=headers)


class ServeAuthorizedBilibiliDownloadEndpoint:
    """Serve explicit Bilibili downloader execution from an accepted dry-run plan."""

    endpoint_path = "/api/rebuild/video-links/bilibili/authorized-download"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        download_video: Callable[..., AuthorizedBilibiliDownloadResult],
        trusted_settings: BilibiliDownloaderSettings,
        register_downloaded_video: Callable[..., DownloadedVideoSourceRegistrationResult] | None = None,
    ) -> VideoWorkflowEndpointResponse:
        if urlsplit(path).path != self.endpoint_path:
            return self._json_response(404, {"detail": "Bilibili authorized download endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "Bilibili authorized download endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        if "command" in body:
            return self._json_response(
                400,
                {
                    "detail": "Bilibili authorized download rejected",
                    "reason": "Bilibili authorized download endpoint does not accept provider command",
                    "actionable": True,
                },
            )
        if body.get("confirm_download") is not True:
            progression = decide_workflow_progression(
                WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE,
            )
            return self._json_response(
                400,
                {
                    "detail": "Bilibili authorized download rejected",
                    "reason": "confirm_download=true is required",
                    "actionable": True,
                    "progression_mode": progression.mode.value,
                    "progression_reason": progression.reason.value,
                },
            )
        try:
            plan = _download_plan_from_payload(_required_mapping(body.get("plan"), "plan"))
            if trusted_settings.enabled is not True:
                raise ValueError("bilibili downloader settings must be enabled")
            result = download_video(plan=plan, settings=trusted_settings)
            registration = None
            if register_downloaded_video is not None and result.status == "completed":
                registration = register_downloaded_video(download=result, title=_optional_str(body.get("title")))
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "Bilibili authorized download rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        payload = serialize_authorized_bilibili_download_result(result)
        if registration is not None:
            payload["source_registration"] = serialize_downloaded_video_source_registration(registration)
            payload["source_id"] = registration.source_id
            payload["source_ref"] = registration.source_ref
            payload["authorization_ref"] = registration.authorization_ref
        return self._json_response(200, payload)

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> VideoWorkflowEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return VideoWorkflowEndpointResponse(status_code=status_code, body=body, headers=headers)


class ServeVideoAudioExtractionEndpoint:
    """Serve explicit video Source -> audio track extraction without accepting provider commands."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        extract_audio: Callable[..., VideoAudioExtractionResult],
    ) -> VideoWorkflowEndpointResponse:
        source_id = _id_from_wrapped_path(path, "/api/rebuild/sources/", "/audio-track")
        if source_id is None:
            return self._json_response(404, {"detail": "video audio extraction endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "video audio extraction endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        reject = _reject_body_command(body, "video audio extraction endpoint")
        if reject is not None:
            return self._json_response(400, reject)
        try:
            result = extract_audio(source_id=source_id)
        except VideoAudioExtractionError as error:
            status_code = 404 if str(error) in {"source not found", "authorized video reference not found"} else 400
            return self._json_response(status_code, _rejected("video audio extraction rejected", error))
        return self._json_response(200, serialize_video_audio_extraction_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> VideoWorkflowEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return VideoWorkflowEndpointResponse(status_code=status_code, body=body, headers=headers)


class ServeAudioAssetTranscriptionEndpoint:
    """Serve explicit generated audio asset -> transcript output without accepting provider commands."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        transcribe_audio: Callable[..., AudioAssetTranscriptionResult],
    ) -> VideoWorkflowEndpointResponse:
        audio_asset_id = _id_from_wrapped_path(path, "/api/rebuild/audio-assets/", "/transcription")
        if audio_asset_id is None:
            return self._json_response(404, {"detail": "audio asset transcription endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "audio asset transcription endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        reject = _reject_body_command(body, "audio asset transcription endpoint")
        if reject is not None:
            return self._json_response(400, reject)
        try:
            result = transcribe_audio(audio_asset_id=audio_asset_id)
        except AudioAssetTranscriptionError as error:
            status_code = 404 if str(error) in {"audio asset not found", "source not found"} else 400
            return self._json_response(status_code, _rejected("audio asset transcription rejected", error))
        return self._json_response(200, serialize_audio_asset_transcription_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> VideoWorkflowEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return VideoWorkflowEndpointResponse(status_code=status_code, body=body, headers=headers)


class ServeTranscriptSummaryEndpoint:
    """Serve explicit transcript output -> summary output without accepting provider commands."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        summarize_transcript: Callable[..., TranscriptSummaryResult],
    ) -> VideoWorkflowEndpointResponse:
        output_id = _id_from_wrapped_path(path, "/api/rebuild/media-processing-outputs/", "/summary")
        if output_id is None:
            return self._json_response(404, {"detail": "transcript summary endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "transcript summary endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        reject = _reject_body_command(body, "transcript summary endpoint")
        if reject is not None:
            return self._json_response(400, reject)
        try:
            result = summarize_transcript(transcript_output_id=output_id)
        except TranscriptSummaryError as error:
            status_code = 404 if str(error) in {"transcript output not found", "source not found"} else 400
            return self._json_response(status_code, _rejected("transcript summary rejected", error))
        return self._json_response(200, serialize_transcript_summary_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> VideoWorkflowEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return VideoWorkflowEndpointResponse(status_code=status_code, body=body, headers=headers)


def _id_from_wrapped_path(path: str, prefix: str, suffix: str) -> str | None:
    parsed = urlsplit(path)
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    value = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return value or None


def _reject_body_command(body: Mapping[str, Any] | None, endpoint_name: str) -> Mapping[str, Any] | None:
    if body is not None and not isinstance(body, Mapping):
        return {"detail": "request body must be a JSON object"}
    if isinstance(body, Mapping) and "command" in body:
        return {
            "detail": f"{endpoint_name} rejected",
            "reason": f"{endpoint_name} does not accept provider command",
            "actionable": True,
        }
    return None


def _rejected(detail: str, error: Exception) -> Mapping[str, Any]:
    return {"detail": detail, "reason": str(error), "actionable": True}


def _required_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _first_video_id(resolution: VideoLinkResolution) -> str:
    if not resolution.videos:
        raise ValueError("Bilibili link resolution contains no videos")
    return resolution.videos[0].video_id


def _required_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} is required")
    return value


def _download_plan_from_payload(payload: Mapping[str, Any]) -> LinkedVideoDownloadPlan:
    source_url = _required_str(payload.get("source_url"), "plan.source_url")
    video_id = _required_str(payload.get("video_id"), "plan.video_id")
    resolution = BilibiliVideoLinkResolver().resolve(url=source_url)
    canonical = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id=video_id,
    )
    if dict(payload) != serialize_linked_video_download_plan(canonical):
        raise ValueError("authorized download requires an unchanged canonical dry-run plan")
    return canonical


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def _list_value(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _cookie_mode(value: str) -> str:
    clean = value.strip().lower()
    if clean not in {"none", "browser", "file"}:
        raise ValueError("settings.cookie_mode must be none, browser or file")
    return clean
