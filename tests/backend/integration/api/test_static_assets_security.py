from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import _path_setup  # noqa: F401
from backend.api.static_assets import _resolve_dist_path, mount_frontend_dist


@pytest.fixture
def frontend_site(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    root_dir = tmp_path / "app-root"
    dist_dir = root_dir / "src" / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)

    index_path = dist_dir / "index.html"
    index_path.write_text("INDEX_CANARY", encoding="utf-8")
    (assets_dir / "app.js").write_text("ASSET_CANARY", encoding="utf-8")
    (dist_dir / "manifest.json").write_text("SAFE_FILE_CANARY", encoding="utf-8")
    (dist_dir / "nested").mkdir()

    outside_path = dist_dir.parent / "outside.txt"
    outside_path.write_text("OUTSIDE_CANARY", encoding="utf-8")
    sibling_dir = dist_dir.parent / "dist-private"
    sibling_dir.mkdir()
    (sibling_dir / "secret.txt").write_text("SIBLING_CANARY", encoding="utf-8")

    app = FastAPI()
    mount_frontend_dist(app, root_dir)
    with TestClient(app) as client:
        yield client, root_dir, dist_dir, outside_path


def test_frontend_static_files_and_spa_fallback_remain_available(
    frontend_site: tuple[TestClient, Path, Path, Path],
) -> None:
    client, _, _, _ = frontend_site

    index_response = client.get("/")
    asset_response = client.get("/assets/app.js")
    file_response = client.get("/manifest.json")
    deep_link_response = client.get("/workspace/item-1")
    api_response = client.get("/api/not-a-real-endpoint")

    assert index_response.status_code == 200
    assert index_response.text == "INDEX_CANARY"
    assert asset_response.status_code == 200
    assert asset_response.text == "ASSET_CANARY"
    assert file_response.status_code == 200
    assert file_response.text == "SAFE_FILE_CANARY"
    assert deep_link_response.status_code == 200
    assert deep_link_response.text == "INDEX_CANARY"
    assert api_response.status_code == 404


@pytest.mark.parametrize(
    "request_path",
    (
        "/%2e%2e/outside.txt",
        "/..%2Foutside.txt",
        "/%252e%252e%252Foutside.txt",
        "/..%5Coutside.txt",
        "/%2e%2e%5Coutside.txt",
        "/..\\outside.txt",
        "/%2e%2e%2Fdist-private%2Fsecret.txt",
    ),
)
def test_frontend_path_rejects_traversal_variants_without_leaking_paths(
    frontend_site: tuple[TestClient, Path, Path, Path],
    request_path: str,
) -> None:
    client, root_dir, _, _ = frontend_site

    response = client.get(request_path)

    assert response.status_code == 404
    assert "OUTSIDE_CANARY" not in response.text
    assert "SIBLING_CANARY" not in response.text
    assert str(root_dir) not in response.text


def test_frontend_path_rejects_existing_directories(
    frontend_site: tuple[TestClient, Path, Path, Path],
) -> None:
    client, root_dir, _, _ = frontend_site

    response = client.get("/nested")

    assert response.status_code == 404
    assert str(root_dir) not in response.text


def test_frontend_path_resolver_rejects_raw_parent_segments(
    frontend_site: tuple[TestClient, Path, Path, Path],
) -> None:
    _, _, dist_dir, _ = frontend_site

    assert _resolve_dist_path(dist_dir, "../dist-private/secret.txt") is None


def test_frontend_path_rejects_symlink_escape(
    frontend_site: tuple[TestClient, Path, Path, Path],
) -> None:
    client, root_dir, dist_dir, outside_path = frontend_site
    link_path = dist_dir / "escape.txt"
    try:
        link_path.symlink_to(outside_path)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable on this platform: {error}")

    response = client.get("/escape.txt")

    assert response.status_code == 404
    assert "OUTSIDE_CANARY" not in response.text
    assert str(root_dir) not in response.text


def test_assets_mount_cannot_escape_its_directory(
    frontend_site: tuple[TestClient, Path, Path, Path],
) -> None:
    client, root_dir, _, _ = frontend_site

    response = client.get("/assets/%2e%2e/index.html")

    assert response.status_code == 404
    assert "INDEX_CANARY" not in response.text
    assert str(root_dir) not in response.text
