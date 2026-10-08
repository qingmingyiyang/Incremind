"""GET /api/rebuild/settings/bilibili-downloader 端点测试。

验证端点已挂载、返回默认配置、以及已保存配置能被正确读取。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sys
import os
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backend.api.app import create_app  # noqa: E402
from core.storage_provider import JsonObjectStore  # noqa: E402


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_bilibili_downloader_settings_returns_defaults_when_unset(tmp_path) -> None:
    """未配置时应返回默认值（disabled、cookie_mode=none）。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/settings/bilibili-downloader")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["status"] == "disabled"
    assert payload["cookie_mode"] == "none"
    assert payload["provider_name"] == "yt-dlp-bilibili"
    assert payload["output_root"] == "work/video-downloads"
    assert payload["memory_publication"] == "not_started"


def test_bilibili_downloader_settings_returns_saved_config(tmp_path) -> None:
    """已保存的配置应能被正确读取。"""
    store = _store(tmp_path)
    store.write("bilibili_downloader_settings", "default", {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": True,
        "provider_name": "yt-dlp-bilibili",
        "output_root": "work/video-downloads",
        "cookie_mode": "file",
        "cookies_from_browser": "",
        "cookies_file": None,
        "allow_restricted_content": False,
        "remote_processing": False,
        "memory_publication": "not_started",
        "updated_at": "2026-07-04T00:00:00Z",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.get("/api/rebuild/settings/bilibili-downloader")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["status"] == "ready"
    assert payload["cookie_mode"] == "file"


def test_bilibili_downloader_settings_no_cache(tmp_path) -> None:
    """响应应携带 no-store 防缓存头。"""
    client = _client(tmp_path)
    response = client.get("/api/rebuild/settings/bilibili-downloader")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store"
