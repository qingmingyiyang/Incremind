from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.media_ingress_selection_authority import MediaIngressSelectionAuthority
from backend.api.routes.product import bilibili as product_bilibili
from core.job_runner import SQLiteJobStore
from core.storage_provider import SQLiteStructuredRecordStore


def test_hands_selection_short_circuits_every_legacy_bilibili_writer_before_effect(
    tmp_path, monkeypatch,
) -> None:
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    MediaIngressSelectionAuthority(SQLiteStructuredRecordStore(database)).publish(
        "hands", expected_revision=0, command_id="select-hands-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )
    effects: list[str] = []

    def forbidden(name):
        def construct(*_args, **_kwargs):
            effects.append(name)
            raise AssertionError(f"legacy effect owner was reached: {name}")
        return construct

    monkeypatch.setattr(product_bilibili, "BilibiliVideoLinkResolver", forbidden("plan"))
    monkeypatch.setattr(
        product_bilibili, "AuthorizedBilibiliDownloader", forbidden("download")
    )
    monkeypatch.setattr(
        product_bilibili, "RegisterDownloadedBilibiliVideoSource", forbidden("source")
    )
    monkeypatch.setattr(product_bilibili, "VideoAutoEffectRuntime", forbidden("workflow"))

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        responses = (
            client.post(
                "/api/rebuild/video-links/bilibili/download-plan",
                json={"url": "https://www.bilibili.com/video/BV1abcDEF234"},
            ),
            client.post(
                "/api/rebuild/video-links/bilibili/authorized-download",
                json={"confirm_download": True, "plan": {}, "settings": {}},
            ),
            client.post(
                "/api/rebuild/video-links/bilibili/auto-download",
                json={"url": "https://www.bilibili.com/video/BV1abcDEF234"},
            ),
        )

    assert effects == []
    assert {response.status_code for response in responses} == {409}
    for response in responses:
        payload = response.json()
        assert payload["status"] == "legacy_ingress_disabled"
        assert payload["selection"]["mode"] == "hands"
        assert payload["selection"]["revision"] == 1
    assert SQLiteJobStore(database).all() == ()


def test_rollback_selection_restores_legacy_plan_without_touching_hands_authority(
    tmp_path,
) -> None:
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    authority = MediaIngressSelectionAuthority(SQLiteStructuredRecordStore(database))
    authority.publish(
        "hands", expected_revision=0, command_id="select-hands-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )
    authority.publish(
        "legacy", expected_revision=1, command_id="rollback-legacy-0002",
        actor="local-user", created_at="2026-08-26T00:01:00Z",
    )

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.post(
            "/api/rebuild/video-links/bilibili/download-plan",
            json={"url": "https://www.bilibili.com/video/BV1abcDEF234"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "planned"
    assert SQLiteJobStore(database).all() == ()
