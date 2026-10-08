from __future__ import annotations

import base64
import hashlib

import pytest

from core.storage_provider import (
    GeneratedAssetAuthority,
    GeneratedAssetAuthorityError,
    GeneratedAssetDimensions,
    JsonObjectStore,
    SQLiteStructuredRecordStore,
)
from core.storage_provider.runtime import ObjectStoreRevisionError


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL6qQAAAABJRU5ErkJggg=="
)


def _arguments(**overrides):
    values = {
        "content": _PNG_1X1,
        "sha256": hashlib.sha256(_PNG_1X1).hexdigest(),
        "media_type": "image/png",
        "dimensions": GeneratedAssetDimensions(width=1, height=1),
        "project_id": "project-1",
        "turn_id": "turn-1",
        "operation_id": "operation-image-generate",
        "invocation_id": "invocation-1",
        "provider_id": "provider-openai",
        "provider_revision": "provider-rev-1",
        "model_id": "model-image",
        "model_revision": "model-rev-1",
        "receipt_refs": ("receipt-provider-1", "receipt-boundary-1"),
        "provenance": {"generation_kind": "image", "privacy_mode": "remote_allowed"},
    }
    values.update(overrides)
    return values


def test_generated_asset_authority_persists_verified_content_addressed_image_without_sensitive_metadata(tmp_path) -> None:
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    authority = GeneratedAssetAuthority(
        object_store=objects,
        vault_root=tmp_path / "vault",
    )

    asset = authority.store(**_arguments())

    assert asset.asset_id.startswith("generated-")
    assert asset.asset_ref == f"crp-ref-default-assets-generated-{asset.asset_id}"
    assert asset.vault_ref == f"assets/generated/{asset.sha256[:2]}/{asset.sha256}"
    assert asset.status == "stored"
    assert asset.revision == 2
    assert (tmp_path / "vault" / asset.vault_ref).read_bytes() == _PNG_1X1
    stored = objects.read("generated_assets", asset.asset_id)
    assert stored is not None
    assert stored["kind"] == "generated_asset"
    assert stored["dimensions"] == {"width": 1, "height": 1}
    assert stored["receipt_refs"] == ["receipt-provider-1", "receipt-boundary-1"]
    assert set(stored) == {
        "schema_version", "id", "kind", "status", "asset_ref", "vault_ref", "sha256",
        "byte_count", "media_type", "dimensions", "project_id", "turn_id", "operation_id",
        "invocation_id", "provider_id", "provider_revision", "model_id", "model_revision",
        "receipt_refs", "provenance",
    }
    assert "prompt" not in str(stored).lower()
    assert "endpoint" not in str(stored).lower()


def test_generated_asset_authority_replay_is_cas_idempotent_and_conflicting_evidence_is_rejected(tmp_path) -> None:
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    authority = GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault")

    first = authority.store(**_arguments())
    replay = authority.store(**_arguments())

    assert replay == first
    assert objects.revision("generated_assets", first.asset_id) == 2
    with pytest.raises(GeneratedAssetAuthorityError, match="identity conflicts"):
        authority.store(**_arguments(provenance={"generation_kind": "image", "safety_mode": "strict"}))
    assert objects.revision("generated_assets", first.asset_id) == 2


def test_generated_asset_authority_uses_existing_sqlite_structured_store_without_a_second_database(tmp_path) -> None:
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    structured = SQLiteStructuredRecordStore(tmp_path / "existing-vault.sqlite3")
    authority = GeneratedAssetAuthority(
        object_store=objects,
        vault_root=tmp_path / "vault",
        sqlite_records=structured,
    )

    asset = authority.store(**_arguments())

    row = structured.read("generated_assets", asset.asset_id)
    assert row is not None
    assert row.revision == 2
    assert objects.read("generated_assets", asset.asset_id) is None
    assert authority.get(asset.asset_id) == asset


class _FailingStore(JsonObjectStore):
    def write(self, collection, object_id, payload, expected_revision):
        raise ObjectStoreRevisionError("fixture metadata write failed")


class _CrashAfterBlobAuthority(GeneratedAssetAuthority):
    def _mark_stored(self, prepared):
        raise RuntimeError("fixture crashed after blob write")


class _BlobWriteFailureAuthority(GeneratedAssetAuthority):
    def _write_blob(self, prepared, content):
        raise OSError("fixture blob write failed")


def test_generated_asset_metadata_failure_writes_zero_blobs(tmp_path) -> None:
    objects = _FailingStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    authority = GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault")

    with pytest.raises(ObjectStoreRevisionError, match="metadata write failed"):
        authority.store(**_arguments())

    assert not (tmp_path / "vault" / "assets" / "generated").exists()


def test_generated_asset_crash_after_blob_before_commit_reconciles_without_new_generation(tmp_path) -> None:
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    crashed = _CrashAfterBlobAuthority(object_store=objects, vault_root=tmp_path / "vault")

    with pytest.raises(RuntimeError, match="after blob write"):
        crashed.store(**_arguments())

    preparing = crashed.find_by_operation(
        turn_id="turn-1", operation_id="operation-image-generate"
    )
    assert len(preparing) == 1
    assert preparing[0].status == "preparing"
    assert (tmp_path / "vault" / preparing[0].vault_ref).is_file()
    with pytest.raises(GeneratedAssetAuthorityError, match="identity conflicts"):
        GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault").store(
            **_arguments(provenance={"generation_kind": "image", "safety_mode": "strict"})
        )
    recovered = GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault").reconcile(
        preparing[0].asset_id
    )
    assert recovered.status == "stored"
    assert recovered.revision == 2
    assert GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault").store(
        **_arguments()
    ) == recovered


def test_generated_asset_without_blob_remains_preparing_for_reconciliation(tmp_path) -> None:
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    authority = _BlobWriteFailureAuthority(object_store=objects, vault_root=tmp_path / "vault")

    with pytest.raises(OSError, match="blob write failed"):
        authority.store(**_arguments())

    prepared = authority.find_by_operation(turn_id="turn-1", operation_id="operation-image-generate")
    assert len(prepared) == 1
    assert prepared[0].status == "preparing"
    assert authority.reconcile(prepared[0].asset_id) == prepared[0]
    assert not (tmp_path / "vault" / "assets" / "generated").exists()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"sha256": "0" * 64}, "sha256 does not match"),
        (
            {"content": b"not-a-png", "sha256": hashlib.sha256(b"not-a-png").hexdigest()},
            "PNG bytes are invalid",
        ),
        ({"media_type": "application/octet-stream"}, "media_type is unsupported"),
        ({"dimensions": GeneratedAssetDimensions(width=2, height=1)}, "dimensions do not match"),
        ({"provenance": {"prompt": "private input"}}, "contains sensitive data"),
        ({"provenance": {"source": "https://provider.example/images/1"}}, "value is unsafe"),
        ({"provenance": {"temporary_file": "C:\\private\\image.png"}}, "value is unsafe"),
    ],
)
def test_generated_asset_authority_rejects_unverified_or_sensitive_contract_fields(tmp_path, overrides, message) -> None:
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "legacy")
    authority = GeneratedAssetAuthority(object_store=objects, vault_root=tmp_path / "vault")

    with pytest.raises(GeneratedAssetAuthorityError, match=message):
        authority.store(**_arguments(**overrides))

    assert objects.list("generated_assets") == ()
    assert not (tmp_path / "vault" / "assets" / "generated").exists()
