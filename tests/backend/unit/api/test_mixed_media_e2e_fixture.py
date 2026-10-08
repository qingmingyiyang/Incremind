from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.storage_provider import JsonObjectStore

from backend.api.mixed_media_e2e_fixture import (
    DESKTOP_NONCE_ENV,
    FIXTURE_NONCE_ENV,
    FIXTURE_URL,
    install_mixed_media_e2e_fixture,
)


def test_fixture_requires_matching_nonempty_desktop_nonce(tmp_path: Path, monkeypatch) -> None:
    for fixture, desktop, expected in (
        (None, None, False),
        ("only-fixture", None, False),
        ("fixture", "desktop", False),
        ("same-random-nonce", "same-random-nonce", True),
    ):
        if fixture is None:
            monkeypatch.delenv(FIXTURE_NONCE_ENV, raising=False)
        else:
            monkeypatch.setenv(FIXTURE_NONCE_ENV, fixture)
        if desktop is None:
            monkeypatch.delenv(DESKTOP_NONCE_ENV, raising=False)
        else:
            monkeypatch.setenv(DESKTOP_NONCE_ENV, desktop)
        container = SimpleNamespace(root_dir=tmp_path)
        store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
        assert install_mixed_media_e2e_fixture(
            container, tmp_path, object_store=store, namespace_id="default"
        ) is expected
        assert hasattr(container, "platform_manifest_providers") is expected


def test_fixture_manifest_is_fixed_mixed_and_contains_no_private_locator(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(FIXTURE_NONCE_ENV, "same-random-nonce")
    monkeypatch.setenv(DESKTOP_NONCE_ENV, "same-random-nonce")
    container = SimpleNamespace(root_dir=tmp_path)
    store = JsonObjectStore(tmp_path / "objects", namespace_id="default")
    assert install_mixed_media_e2e_fixture(
        container, tmp_path, object_store=store, namespace_id="default"
    )
    assert set(container.platform_manifest_providers) == {"bilibili", "xiaohongshu"}
    provider = container.platform_manifest_providers["xiaohongshu"]
    manifest = provider.provide(FIXTURE_URL, project_id="default")
    assert manifest.content_kind == "mixed"
    assert [item.kind for item in manifest.assets] == ["image", "video", "image", "text"]
    assert all(item.locator is None for item in manifest.assets)
    assert len(manifest.permission.evidence_refs) == 1
    assert "/source-resolution-evidence/projects/default/" in manifest.permission.evidence_refs[0]
    assert container._media_hands_policy_snapshot_for_test["enabled"] is True
