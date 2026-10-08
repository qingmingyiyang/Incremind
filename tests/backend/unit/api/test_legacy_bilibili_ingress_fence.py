from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.media_ingress_selection_authority import (
    MediaIngressSelectionAuthority,
    SelectionGatedLegacyBilibiliDownloader,
)
from backend.api.routes.linked import router
from core.storage_provider import SQLiteStructuredRecordStore


def _authority(tmp_path) -> MediaIngressSelectionAuthority:
    return MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    )


def _select_hands(tmp_path) -> None:
    _authority(tmp_path).publish(
        "hands", expected_revision=0, command_id="select-hands-linked-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )


def _client(tmp_path, **attributes: object) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=tmp_path, **attributes)
    application.include_router(router)
    return TestClient(application)


def test_hands_selection_short_circuits_legacy_linked_resolve_before_workspace_effect(
    tmp_path,
) -> None:
    _select_hands(tmp_path)

    class _Resolver:
        async def run(self, **_kwargs):
            raise AssertionError("legacy linked resolver was reached")

    response = _client(tmp_path, resolve_bilibili_video=_Resolver()).post(
        "/api/linked/bilibili/resolve/video",
        json={"url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "legacy_ingress_disabled"
    assert response.json()["detail"]["selection"]["mode"] == "hands"


def test_hands_selection_short_circuits_legacy_linked_series_resolve_before_workspace_effect(
    tmp_path,
) -> None:
    _select_hands(tmp_path)

    class _Resolver:
        async def run(self, **_kwargs):
            raise AssertionError("legacy linked series resolver was reached")

    response = _client(tmp_path, resolve_bilibili_series=_Resolver()).post(
        "/api/linked/bilibili/resolve/series",
        json={"url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "legacy_ingress_disabled"
    assert response.json()["detail"]["selection"]["mode"] == "hands"


def test_hands_selection_short_circuits_linked_download_before_task_creation(tmp_path) -> None:
    _select_hands(tmp_path)

    class _Starter:
        def run(self, **_kwargs):
            raise AssertionError("legacy linked download starter was reached")

    linked = SimpleNamespace(
        videos=[SimpleNamespace(video_id="video-1", provider="bilibili")]
    )
    workspace = SimpleNamespace(get_linked_series=lambda _series_id: linked)
    response = _client(
        tmp_path,
        start_linked_video_download=_Starter(),
        linked_series_workspace=workspace,
    ).post("/api/videos/series-1/video-1/download")

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "legacy_ingress_disabled"


def test_selection_gated_downloader_blocks_late_hands_cutover_before_network_or_file_effect(
    tmp_path,
) -> None:
    calls: list[tuple[object, ...]] = []

    class _Downloader:
        def download(self, *args: object, **kwargs: object) -> Path:
            calls.append((*args, kwargs))
            return Path("legacy-output.mp4")

    authority = _authority(tmp_path)
    downloader = SelectionGatedLegacyBilibiliDownloader(_Downloader(), authority)
    assert downloader.download("BV1xx411c7mD", 1, tmp_path, object()) == Path("legacy-output.mp4")
    authority.publish(
        "hands", expected_revision=0, command_id="select-hands-late-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )

    try:
        downloader.download("BV1xx411c7mD", 1, tmp_path, object())
    except Exception as error:
        assert str(error) == "legacy_ingress_disabled"
    else:
        raise AssertionError("Hands cutover did not block legacy downloader")
    assert len(calls) == 1

    authority.publish(
        "legacy", expected_revision=1, command_id="rollback-legacy-late-0002",
        actor="local-user", created_at="2026-08-26T00:01:00Z",
    )
    assert downloader.download("BV1xx411c7mD", 1, tmp_path, object()) == Path("legacy-output.mp4")
    assert len(calls) == 2
