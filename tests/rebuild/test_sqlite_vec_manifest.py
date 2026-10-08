"""3.14 sqlite-vec manifest 测试：只定义 interface 和 manifest，不默认启用。

spec 3.14 验收：sqlite-vec 只定义 interface 和 manifest，不默认启用。
"""

from __future__ import annotations

import pytest

from core.search_and_recall import (
    SqliteVecManifest,
    SqliteVecManifestError,
    create_sqlite_vec_manifest,
    sqlite_vec_manifest_payload,
)


def _valid_source_refs() -> tuple[str, ...]:
    return ("source-alpha#rev:1", "source-beta#rev:2")


def _valid_source_fingerprint() -> str:
    return "abc123def456"


def test_create_sqlite_vec_manifest_returns_disabled_manifest() -> None:
    manifest = create_sqlite_vec_manifest(
        source_fingerprint=_valid_source_fingerprint(),
        source_count=2,
        source_refs=_valid_source_refs(),
    )

    assert manifest.manifest_id == "sqlite_vec_deferred"
    assert manifest.status == "deferred"
    assert manifest.backend_kind == "sqlite_vec"
    assert manifest.enabled is False  # 强制 disabled
    assert manifest.provider is None
    assert manifest.dimension is None
    assert manifest.source_count == 2


def test_sqlite_vec_manifest_payload_validates_disabled() -> None:
    manifest = create_sqlite_vec_manifest(
        source_fingerprint=_valid_source_fingerprint(),
        source_count=2,
        source_refs=_valid_source_refs(),
    )

    payload = sqlite_vec_manifest_payload(manifest)

    assert payload["backend_kind"] == "sqlite_vec"
    assert payload["index_role"] == "deferred_manifest"
    assert payload["vector"]["enabled"] is False


def test_sqlite_vec_manifest_payload_rejects_enabled_true() -> None:
    """spec 3.14：sqlite-vec 不默认启用。任何 enabled=True 都被拒绝。"""
    bad_payload = {
        "schema_version": "1.0.0",
        "id": "sqlite_vec_deferred",
        "status": "deferred",
        "backend_kind": "sqlite_vec",
        "source": "sqlite_vec_deferred_manifest",
        "source_fingerprint": _valid_source_fingerprint(),
        "source_count": 2,
        "source_refs": list(_valid_source_refs()),
        "index_role": "deferred_manifest",
        "vector": {
            "enabled": True,  # 不允许
            "provider": None,
            "dimension": None,
        },
        "created_at": "2026-07-05T00:00:00+00:00",
    }

    with pytest.raises(SqliteVecManifestError, match="disabled"):
        sqlite_vec_manifest_payload(bad_payload)


def test_sqlite_vec_manifest_rejects_active_id() -> None:
    with pytest.raises(SqliteVecManifestError, match="active"):
        create_sqlite_vec_manifest(
            source_fingerprint=_valid_source_fingerprint(),
            source_count=2,
            source_refs=_valid_source_refs(),
            manifest_id="active",
        )


def test_sqlite_vec_manifest_rejects_source_count_mismatch() -> None:
    with pytest.raises(SqliteVecManifestError, match="source_count"):
        create_sqlite_vec_manifest(
            source_fingerprint=_valid_source_fingerprint(),
            source_count=3,  # 不匹配 source_refs 长度
            source_refs=_valid_source_refs(),  # 2 个
        )


def test_sqlite_vec_manifest_rejects_empty_source_fingerprint() -> None:
    with pytest.raises(SqliteVecManifestError, match="source_fingerprint"):
        create_sqlite_vec_manifest(
            source_fingerprint="",
            source_count=2,
            source_refs=_valid_source_refs(),
        )


def test_sqlite_vec_manifest_rejects_untraceable_source_refs() -> None:
    with pytest.raises(SqliteVecManifestError, match="source_id#rev"):
        create_sqlite_vec_manifest(
            source_fingerprint=_valid_source_fingerprint(),
            source_count=1,
            source_refs=("source-alpha",),  # 缺 #rev:
        )


def test_sqlite_vec_manifest_payload_rejects_wrong_backend_kind() -> None:
    bad_payload = {
        "schema_version": "1.0.0",
        "id": "sqlite_vec_deferred",
        "status": "deferred",
        "backend_kind": "sqlite_fts5",  # 错误
        "source": "sqlite_vec_deferred_manifest",
        "source_fingerprint": _valid_source_fingerprint(),
        "source_count": 2,
        "source_refs": list(_valid_source_refs()),
        "index_role": "deferred_manifest",
        "vector": {"enabled": False, "provider": None, "dimension": None},
        "created_at": "2026-07-05T00:00:00+00:00",
    }

    with pytest.raises(SqliteVecManifestError, match="sqlite_vec"):
        sqlite_vec_manifest_payload(bad_payload)


def test_sqlite_vec_manifest_payload_rejects_wrong_index_role() -> None:
    bad_payload = {
        "schema_version": "1.0.0",
        "id": "sqlite_vec_deferred",
        "status": "deferred",
        "backend_kind": "sqlite_vec",
        "source": "sqlite_vec_deferred_manifest",
        "source_fingerprint": _valid_source_fingerprint(),
        "source_count": 2,
        "source_refs": list(_valid_source_refs()),
        "index_role": "active_manifest",  # 错误
        "vector": {"enabled": False, "provider": None, "dimension": None},
        "created_at": "2026-07-05T00:00:00+00:00",
    }

    with pytest.raises(SqliteVecManifestError, match="deferred manifest"):
        sqlite_vec_manifest_payload(bad_payload)


def test_sqlite_vec_manifest_does_not_leak_sensitive_fields() -> None:
    manifest = create_sqlite_vec_manifest(
        source_fingerprint=_valid_source_fingerprint(),
        source_count=2,
        source_refs=_valid_source_refs(),
    )

    payload = sqlite_vec_manifest_payload(manifest)
    payload_text = str(payload).lower()

    for forbidden in ("sk-", "api_key", "cookie", "authorization", "bearer", "password", "token"):
        assert forbidden not in payload_text, f"sqlite_vec manifest leaked: {forbidden}"


def test_sqlite_vec_interface_is_protocol() -> None:
    """SqliteVecInterface 是 Protocol 占位，不提供具体实现。"""
    from core.search_and_recall import SqliteVecInterface

    # Protocol 不能被 isinstance 检查（除非 runtime_checkable）
    # 但能被引用为类型注解
    assert SqliteVecInterface is not None
    # 确认它有 build_index 和 query 方法签名
    assert hasattr(SqliteVecInterface, "build_index")
    assert hasattr(SqliteVecInterface, "query")
