from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .workbench_source_intake import (
    WorkbenchAudioSourceIntakeResult,
    WorkbenchBookmarkCollectionIntakeResult,
    WorkbenchFileSourceIntakeResult,
    WorkbenchImageSourceIntakeResult,
    WorkbenchLinkSourceIntakeResult,
    WorkbenchTextSourceIntakeResult,
    WorkbenchVideoSourceIntakeResult,
    serialize_workbench_audio_source_intake,
    serialize_workbench_bookmark_collection_intake,
    serialize_workbench_file_source_intake,
    serialize_workbench_image_source_intake,
    serialize_workbench_link_source_intake,
    serialize_workbench_text_source_intake,
    serialize_workbench_video_source_intake,
)
from .workbench_audio_intake_flow import (
    WorkbenchAudioIntakeFlowResult,
    serialize_workbench_audio_intake_flow,
)
from .workbench_video_intake_flow import (
    WorkbenchVideoIntakeFlowResult,
    serialize_workbench_video_intake_flow,
)


@dataclass(frozen=True, slots=True)
class WorkbenchTextSourceIntakeEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeWorkbenchTextSourceIntakeEndpoint:
    """Serve the narrow workbench text Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/text-source-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchTextSourceIntakeResult],
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench text Source intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench text Source intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        content = body.get("content", "")
        if not isinstance(title, str) or not isinstance(content, str):
            return self._json_response(400, {"detail": "title and content must be strings"})
        try:
            result = intake(title=title, content=content)
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench text Source intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_workbench_text_source_intake(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeWorkbenchLinkSourceIntakeEndpoint:
    """Serve the narrow workbench link Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/link-source-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchLinkSourceIntakeResult],
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench link Source intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench link Source intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        url = body.get("url", "")
        if not isinstance(title, str) or not isinstance(url, str):
            return self._json_response(400, {"detail": "title and url must be strings"})
        try:
            result = intake(title=title, url=url)
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench link Source intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_workbench_link_source_intake(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeWorkbenchBookmarkCollectionIntakeEndpoint:
    """Serve the narrow workbench bookmark collection Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/bookmark-collection-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchBookmarkCollectionIntakeResult],
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench bookmark collection intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench bookmark collection intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        urls = body.get("urls", [])
        if not isinstance(title, str) or not isinstance(urls, list):
            return self._json_response(400, {"detail": "title must be a string and urls must be a list"})
        if any(not isinstance(url, str) for url in urls):
            return self._json_response(400, {"detail": "urls must contain only strings"})
        try:
            result = intake(title=title, urls=tuple(urls))
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench bookmark collection intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_workbench_bookmark_collection_intake(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeWorkbenchFileSourceIntakeEndpoint:
    """Serve the narrow workbench file Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/file-source-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchFileSourceIntakeResult],
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench file Source intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench file Source intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        display_name = body.get("display_name", "")
        media_type = body.get("media_type", "")
        size_bytes = body.get("size_bytes")
        file_reference = body.get("file_reference", "")
        if (
            not isinstance(title, str)
            or not isinstance(display_name, str)
            or not isinstance(media_type, str)
            or not isinstance(size_bytes, int)
            or not isinstance(file_reference, str)
        ):
            return self._json_response(
                400,
                {
                    "detail": (
                        "title, display_name, media_type, size_bytes and file_reference "
                        "must be platform-neutral file metadata"
                    )
                },
            )
        try:
            result = intake(
                title=title,
                display_name=display_name,
                media_type=media_type,
                size_bytes=size_bytes,
                file_reference=file_reference,
            )
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench file Source intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_workbench_file_source_intake(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeWorkbenchImageSourceIntakeEndpoint:
    """Serve the narrow workbench image Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/image-source-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchImageSourceIntakeResult],
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench image Source intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench image Source intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        display_name = body.get("display_name", "")
        media_type = body.get("media_type", "")
        size_bytes = body.get("size_bytes")
        image_reference = body.get("image_reference", "")
        width_px = body.get("width_px")
        height_px = body.get("height_px")
        if (
            not isinstance(title, str)
            or not isinstance(display_name, str)
            or not isinstance(media_type, str)
            or not isinstance(size_bytes, int)
            or not isinstance(image_reference, str)
            or (width_px is not None and not isinstance(width_px, int))
            or (height_px is not None and not isinstance(height_px, int))
        ):
            return self._json_response(
                400,
                {
                    "detail": (
                        "title, display_name, media_type, size_bytes, image_reference "
                        "and optional dimensions must be platform-neutral image metadata"
                    )
                },
            )
        try:
            result = intake(
                title=title,
                display_name=display_name,
                media_type=media_type,
                size_bytes=size_bytes,
                image_reference=image_reference,
                width_px=width_px,
                height_px=height_px,
            )
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench image Source intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, serialize_workbench_image_source_intake(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeWorkbenchAudioSourceIntakeEndpoint:
    """Serve the narrow workbench audio Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/audio-source-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchAudioSourceIntakeResult] | None = None,
        intake_flow: Callable[..., WorkbenchAudioIntakeFlowResult] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench audio Source intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench audio Source intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        display_name = body.get("display_name", "")
        media_type = body.get("media_type", "")
        size_bytes = body.get("size_bytes")
        audio_reference = body.get("audio_reference", "")
        duration_ms = body.get("duration_ms")
        if (
            not isinstance(title, str)
            or not isinstance(display_name, str)
            or not isinstance(media_type, str)
            or not isinstance(size_bytes, int)
            or not isinstance(audio_reference, str)
            or (duration_ms is not None and not isinstance(duration_ms, int))
        ):
            return self._json_response(
                400,
                {
                    "detail": (
                        "title, display_name, media_type, size_bytes, audio_reference "
                        "and optional duration must be platform-neutral audio metadata"
                    )
                },
            )
        try:
            if intake_flow is not None:
                project_id = body.get("project_id")
                result = intake_flow(
                    title=title,
                    display_name=display_name,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    audio_reference=audio_reference,
                    duration_ms=duration_ms,
                    project_id=project_id if isinstance(project_id, str) else None,
                )
                payload = serialize_workbench_audio_intake_flow(result)
            elif intake is not None:
                result = intake(
                    title=title,
                    display_name=display_name,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    audio_reference=audio_reference,
                    duration_ms=duration_ms,
                )
                payload = serialize_workbench_audio_source_intake(result)
            else:
                raise ValueError("workbench audio Source intake service is unavailable")
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench audio Source intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, payload)

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )


class ServeWorkbenchVideoSourceIntakeEndpoint:
    """Serve the narrow workbench video Source intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/video-source-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        intake: Callable[..., WorkbenchVideoSourceIntakeResult] | None = None,
        intake_flow: Callable[..., WorkbenchVideoIntakeFlowResult] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench video Source intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench video Source intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        title = body.get("title", "")
        display_name = body.get("display_name", "")
        media_type = body.get("media_type", "")
        size_bytes = body.get("size_bytes")
        video_reference = body.get("video_reference", "")
        duration_ms = body.get("duration_ms")
        width_px = body.get("width_px")
        height_px = body.get("height_px")
        if (
            not isinstance(title, str)
            or not isinstance(display_name, str)
            or not isinstance(media_type, str)
            or not isinstance(size_bytes, int)
            or not isinstance(video_reference, str)
            or (duration_ms is not None and not isinstance(duration_ms, int))
            or (width_px is not None and not isinstance(width_px, int))
            or (height_px is not None and not isinstance(height_px, int))
        ):
            return self._json_response(
                400,
                {
                    "detail": (
                        "title, display_name, media_type, size_bytes, video_reference "
                        "and optional duration or dimensions must be platform-neutral video metadata"
                    )
                },
            )
        try:
            if intake_flow is not None:
                project_id = body.get("project_id")
                result = intake_flow(
                    title=title,
                    display_name=display_name,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    video_reference=video_reference,
                    duration_ms=duration_ms,
                    width_px=width_px,
                    height_px=height_px,
                    project_id=project_id if isinstance(project_id, str) else None,
                )
                payload = serialize_workbench_video_intake_flow(result)
            elif intake is not None:
                result = intake(
                    title=title,
                    display_name=display_name,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    video_reference=video_reference,
                    duration_ms=duration_ms,
                    width_px=width_px,
                    height_px=height_px,
                )
                payload = serialize_workbench_video_source_intake(result)
            else:
                raise ValueError("workbench video Source intake service is unavailable")
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench video Source intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(201, payload)

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchTextSourceIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchTextSourceIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )
