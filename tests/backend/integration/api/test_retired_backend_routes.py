"""Retired entry points stay absent without changing retained route families."""

from types import SimpleNamespace

import pytest

from backend.api.app import create_app
from backend.memory_app.app import create_app as create_memory_app
from backend.security.secrets import InMemorySecretStore


@pytest.fixture(scope="module")
def installed_routes(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("retired-routes")
    app = create_app(SimpleNamespace(root_dir=tmp_path, secret_store=InMemorySecretStore()))
    app = create_memory_app(runtime_root=tmp_path, legacy_app=app)
    return app.openapi()["paths"]


@pytest.fixture(scope="module")
def installed_paths(installed_routes):
    return set(installed_routes)


def test_retained_desktop_and_library_routes(installed_paths):
    paths = installed_paths
    assert "/api/rebuild/team-memory/profile" in paths
    assert "/api/rebuild/memory/export/file" in paths
    assert "/api/rebuild/memory/export/round-trip" in paths
    assert "/api/rebuild/memory-snapshots" in paths
    assert "/api/recognition/tasks/{task_id}" in paths


def test_retained_proposal_review_routes(installed_routes):
    assert "get" in installed_routes["/api/recognition/restructure-proposals"]
    assert "patch" in installed_routes["/api/recognition/restructure-proposals/{proposal_id}"]


@pytest.mark.parametrize("path", [
    "/api/recognition/candidates/from-experiences",
    "/api/recognition/mental-models",
    "/api/recognition/mental-models/{question_id}/refresh",
    "/api/recognition/context/preview",
    "/api/recognition/tasks",
    "/api/recognition/tasks/{task_id}/approve",
    "/api/recognition/tasks/{task_id}/cancel",
    "/api/recognition/restructure/preview",
    "/api/recognition/restructure-inputs",
    "/api/recognition/restructure-proposals",
])
def test_zero_consumer_post_operations_are_retired(installed_routes, path):
    assert "post" not in installed_routes.get(path, {})


@pytest.mark.parametrize("path", [
    "/api/videos/{series_id}/{video_id}/knowledge-cards/generate",
    "/api/recognition/migration/export-preview",
    "/api/recognition/migration/import-preview",
    "/api/recognition/migration/import-commit",
    "/api/recognition/tasks/{task_id}/feedback",
    "/api/rebuild/mvp/readiness",
    "/api/rebuild/self-use-alpha/readiness",
    "/api/rebuild/whitebox-memory/export",
    "/api/rebuild/whitebox-memory/exports/{export_id}/files",
])
def test_zero_consumer_routes_are_retired(installed_paths, path):
    assert path not in installed_paths
