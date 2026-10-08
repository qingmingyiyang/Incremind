from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import CompanionRepository


def _client(tmp_path, *, mode: str = "development") -> tuple[TestClient, SimpleNamespace]:
    repository = tmp_path / "repository"
    resources = tmp_path / "resources"
    user_data = tmp_path / "user-data"
    repository.mkdir()
    (resources / "manual").mkdir(parents=True)
    user_data.mkdir()
    container = SimpleNamespace(
        root_dir=tmp_path / "vault",
        companion_mode=mode,
        companion_repository_root=str(repository.resolve()),
        companion_resources_root=str(resources.resolve()),
        companion_user_data_root=str(user_data.resolve()),
        companion_native_manual_open=True,
    )
    return TestClient(create_app(container)), container


def test_development_manual_and_unicode_notes_roundtrip_without_path_disclosure(tmp_path) -> None:
    client, container = _client(tmp_path)
    repository = tmp_path / "repository"
    repository.joinpath("readme.md").write_text("# 本地说明\n\n右键打开菜单。", encoding="utf-8")

    manual = client.get("/api/rebuild/companion/manual")
    created = client.post("/api/rebuild/companion/notes", json={"content": "灵感 🐻\n第二行"})
    listed = client.get("/api/rebuild/companion/notes")

    assert manual.status_code == 200
    assert manual.json() == {"markdown": "# 本地说明\n\n右键打开菜单。", "mode": "development", "can_open_editor": True}
    assert str(repository.resolve()) not in manual.text
    assert created.status_code == 201
    assert created.json()["note"]["content"] == "灵感 🐻\n第二行"
    assert listed.json()["items"] == [created.json()["note"]]
    notes_path = tmp_path / "user-data" / "companion" / "notes.txt"
    assert notes_path.is_file()
    assert "灵感 🐻" in notes_path.read_text(encoding="utf-8")
    database = container.root_dir / ".rebuild-data" / "companion" / "companion.sqlite3"
    assert not database.exists() or "灵感 🐻".encode("utf-8") not in database.read_bytes()


def test_packaged_manual_seeds_once_and_preserves_user_edit(tmp_path) -> None:
    client, _container = _client(tmp_path, mode="packaged")
    seed = tmp_path / "resources" / "manual" / "readme.md"
    seed.write_text("seed-one", encoding="utf-8")
    assert client.get("/api/rebuild/companion/manual").json()["markdown"] == "seed-one"
    target = tmp_path / "user-data" / "companion" / "readme.md"
    target.write_text("user-edit", encoding="utf-8")
    seed.write_text("seed-two", encoding="utf-8")
    assert client.get("/api/rebuild/companion/manual").json()["markdown"] == "user-edit"


def test_help_notes_api_rejects_missing_manual_bad_shapes_and_oversized_notes(tmp_path) -> None:
    client, _container = _client(tmp_path)
    missing = client.get("/api/rebuild/companion/manual")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "manual_unavailable"
    assert client.post("/api/rebuild/companion/notes", json={"content": "ok", "path": "C:/secret"}).status_code == 400
    assert client.post("/api/rebuild/companion/notes", json={"content": "x" * 4001}).status_code == 400
    assert client.post(
        "/api/rebuild/companion/notes",
        content="not-json",
        headers={"Content-Type": "application/json"},
    ).status_code == 400


def test_help_notes_runtime_configuration_fails_closed(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    assert client.get("/api/rebuild/companion/manual").status_code == 404
    assert client.get("/api/rebuild/companion/notes").status_code == 503


def test_desktop_main_can_backup_preflight_and_restore_without_projecting_paths(
    tmp_path, monkeypatch
) -> None:
    secret = "s" * 43
    for key, value in {
        "CHRIPTMAS_DESKTOP_SESSION_MODE": "desktop_production",
        "CHRIPTMAS_DESKTOP_SESSION_SECRET": secret,
        "CHRIPTMAS_DESKTOP_INSTANCE_ID": "instance-backup-restore",
        "CHRIPTMAS_DESKTOP_NONCE": "n" * 43,
        "CHRIPTMAS_DESKTOP_PROTOCOL_VERSION": "desktop-loopback/1",
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT": "2099-01-01T00:00:00+00:00",
        "CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN": "http://127.0.0.1:8317",
    }.items():
        monkeypatch.setenv(key, value)
    client, container = _client(tmp_path)
    client.headers["X-Chriptmas-Desktop-Session"] = secret
    repository = CompanionRepository.at_data_root(container.root_dir)
    repository.record_wallet_transaction(
        transaction_id="transaction:before",
        idempotency_key="reward:before",
        reason="before backup",
        delta=5,
        created_at="2026-07-24T00:00:01+00:00",
    )
    backup = tmp_path / "chosen" / "companion.sqlite3"

    created = _main_post(client, secret, "backup", "/data/backup", {"path": str(backup)})
    assert created.status_code == 201
    assert set(created.json()) == {
        "status", "fingerprint", "size_bytes", "database_schema_version", "created_at",
    }
    assert str(backup) not in created.text

    ready = _main_post(
        client, secret, "restore-preflight", "/data/restore/preflight", {"path": str(backup)}
    )
    assert ready.status_code == 200
    assert ready.json()["fingerprint"] == created.json()["fingerprint"]
    assert str(backup) not in ready.text

    repository.record_wallet_transaction(
        transaction_id="transaction:after",
        idempotency_key="reward:after",
        reason="after backup",
        delta=3,
        created_at="2026-07-24T00:00:02+00:00",
    )
    restored = _main_post(
        client,
        secret,
        "restore",
        "/data/restore",
        {"path": str(backup), "expected_fingerprint": ready.json()["fingerprint"]},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "restored"
    assert restored.json()["rollback_created"] is True
    assert str(backup) not in restored.text
    restarted = CompanionRepository.at_data_root(container.root_dir)
    assert restarted.wallet_integrity().snapshot_balance == 5
    assert list((container.root_dir / ".rebuild-data" / "companion" / "restore-points").glob("*.sqlite3"))


def test_companion_backup_routes_reject_renderer_and_tampered_main_signatures(tmp_path) -> None:
    client, _container = _client(tmp_path)
    target = tmp_path / "backup.sqlite3"
    assert client.post("/api/rebuild/companion/data/backup", json={"path": str(target)}).status_code == 403


def _main_post(client, secret: str, action: str, path: str, payload: dict[str, str]):
    fingerprint = payload.get("expected_fingerprint", "")
    message = f"companion-data:{action}:{payload['path']}:{fingerprint}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return client.post(
        f"/api/rebuild/companion{path}",
        json=payload,
        headers={"X-Chriptmas-Main-Signature": signature},
    )
