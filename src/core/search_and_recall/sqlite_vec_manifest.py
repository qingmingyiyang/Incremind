"""sqlite-vec manifest 与 interface 占位（spec 3.14：只定义 interface 和 manifest，不默认启用）。

spec 要求：
- sqlite-vec 只定义 interface 和 manifest，不默认启用。
- 不阻塞 MVP。

本模块只提供：
- ``SqliteVecManifest`` dataclass：manifest 结构。
- ``SqliteVecInterface`` Protocol：未来实现的接口契约（暂无具体实现）。
- ``create_sqlite_vec_manifest``：构建 manifest（强制 enabled=False）。
- ``sqlite_vec_manifest_payload``：序列化 + 校验（拒绝 enabled=True）。

不提供：
- 实际的 vector 索引构建 / 查询 / 删除逻辑。
- 激活（activation）逻辑。
- 任何写入 ObjectStore 的 repository。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol


SqliteVecManifestStatus = Literal["planned", "deferred"]


class SqliteVecManifestError(ValueError):
    """Raised when a sqlite-vec manifest would weaken Recall index guarantees."""


@dataclass(frozen=True, slots=True)
class SqliteVecManifest:
    """sqlite-vec manifest 占位结构（始终 disabled）。

    spec 3.14：sqlite-vec 只定义 interface 和 manifest，不默认启用。
    本 dataclass 强制 ``enabled=False``，任何尝试启用都会被 ``_validate_payload`` 拒绝。
    """

    manifest_id: str
    status: SqliteVecManifestStatus
    backend_kind: str
    enabled: bool  # 始终为 False
    provider: str | None
    dimension: int | None
    source_fingerprint: str
    source_count: int
    source_refs: tuple[str, ...]
    created_at: str


class SqliteVecInterface(Protocol):
    """sqlite-vec 接口契约占位（spec 3.14：只定义 interface，不默认启用）。

    未来实现 sqlite-vec 时需满足此契约。当前无任何具体实现。
    所有方法都应在不启用时返回空结果或抛 ``SqliteVecManifestError``。
    """

    def build_index(self, *, entries: Sequence[Mapping[str, object]], manifest: Mapping[str, object]) -> Mapping[str, object]:
        """构建 vector 索引（未来实现）。"""
        ...

    def query(self, *, query_vector: Sequence[float], manifest: Mapping[str, object], limit: int = 12) -> tuple[Mapping[str, object], ...]:
        """查询 vector 索引（未来实现）。"""
        ...


def create_sqlite_vec_manifest(
    *,
    source_fingerprint: str,
    source_count: int,
    source_refs: Sequence[str],
    manifest_id: str = "sqlite_vec_deferred",
    status: SqliteVecManifestStatus = "deferred",
    provider: str | None = None,
    dimension: int | None = None,
    created_at: str | None = None,
) -> SqliteVecManifest:
    """构建 sqlite-vec manifest（强制 enabled=False）。

    spec 3.14：sqlite-vec 只定义 interface 和 manifest，不默认启用。
    任何尝试传入 enabled=True 都不被支持（本函数不接收 enabled 参数，始终为 False）。
    """
    _validate_source_refs(source_refs)
    if source_count != len(source_refs):
        raise SqliteVecManifestError("sqlite_vec manifest source_count must match source_refs")
    if not source_fingerprint:
        raise SqliteVecManifestError("sqlite_vec manifest requires source_fingerprint")
    if manifest_id == "active":
        raise SqliteVecManifestError("sqlite_vec manifest must not replace active index")
    return SqliteVecManifest(
        manifest_id=manifest_id,
        status=status,
        backend_kind="sqlite_vec",
        enabled=False,  # 强制 disabled
        provider=provider,
        dimension=dimension,
        source_fingerprint=source_fingerprint,
        source_count=source_count,
        source_refs=tuple(source_refs),
        created_at=created_at or _utc_now(),
    )


def sqlite_vec_manifest_payload(manifest: SqliteVecManifest | Mapping[str, object]) -> dict[str, object]:
    """序列化 sqlite-vec manifest + 校验（拒绝 enabled=True）。

    spec 3.14：sqlite-vec 不默认启用。任何尝试构建 enabled=True 的 manifest 都会被拒绝。
    """
    if isinstance(manifest, SqliteVecManifest):
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "id": manifest.manifest_id,
            "status": manifest.status,
            "backend_kind": manifest.backend_kind,
            "source": "sqlite_vec_deferred_manifest",
            "source_fingerprint": manifest.source_fingerprint,
            "source_count": manifest.source_count,
            "source_refs": list(manifest.source_refs),
            "index_role": "deferred_manifest",
            "vector": {
                "enabled": manifest.enabled,  # 始终为 False
                "provider": manifest.provider,
                "dimension": manifest.dimension,
            },
            "created_at": manifest.created_at,
        }
    else:
        payload = dict(manifest)
    _validate_payload(payload)
    return payload


def _validate_payload(payload: Mapping[str, object]) -> None:
    """校验 sqlite-vec manifest payload（强制 enabled=False）。"""
    if payload.get("schema_version") != "1.0.0":
        raise SqliteVecManifestError("sqlite_vec manifest requires schema_version 1.0.0")
    if _required_string(payload, "id") == "active":
        raise SqliteVecManifestError("sqlite_vec manifest must not replace active index")
    if payload.get("status") not in {"planned", "deferred"}:
        raise SqliteVecManifestError("sqlite_vec manifest requires planned or deferred status")
    if payload.get("backend_kind") != "sqlite_vec":
        raise SqliteVecManifestError("sqlite_vec manifest requires backend_kind sqlite_vec")
    if payload.get("index_role") != "deferred_manifest":
        raise SqliteVecManifestError("sqlite_vec manifest must be a deferred manifest")
    source_count = _required_int(payload, "source_count")
    source_refs = _required_string_sequence(payload, "source_refs")
    if source_count != len(source_refs):
        raise SqliteVecManifestError("sqlite_vec manifest source_count must match source_refs")
    _validate_source_refs(source_refs)
    _required_string(payload, "source_fingerprint")
    _required_string(payload, "created_at")
    vector = payload.get("vector")
    if not isinstance(vector, Mapping):
        raise SqliteVecManifestError("sqlite_vec manifest requires vector settings")
    # spec 3.14：sqlite-vec 不默认启用。强制 enabled=False。
    if vector.get("enabled") is not False:
        raise SqliteVecManifestError("sqlite_vec manifest must keep vector disabled (spec 3.14: not enabled by default)")


def _validate_source_refs(source_refs: Sequence[str]) -> None:
    if not source_refs:
        raise SqliteVecManifestError("sqlite_vec manifest requires source refs")
    for ref in source_refs:
        if not isinstance(ref, str) or "#rev:" not in ref:
            raise SqliteVecManifestError("sqlite_vec manifest source refs must use source_id#rev:<revision>")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SqliteVecManifestError(f"sqlite_vec manifest requires {key}")
    return value


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SqliteVecManifestError(f"sqlite_vec manifest requires non-negative {key}")
    return value


def _required_string_sequence(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SqliteVecManifestError(f"sqlite_vec manifest requires {key}")
    return tuple(item for item in value if isinstance(item, str))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "SqliteVecInterface",
    "SqliteVecManifest",
    "SqliteVecManifestError",
    "SqliteVecManifestStatus",
    "create_sqlite_vec_manifest",
    "sqlite_vec_manifest_payload",
]
