"""Library index ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
import sqlite3, time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.job_runtime import build_rebuild_job_repository as _job_repository
from backend.api.library_query_runtime import load_current_recall_entries as _current_recall_entries

from core.product_core.index_rebuild_effect_admission import (
    IndexRebuildEffectAdmissionFactory,
    SQLiteIndexRebuildEffectAdmission,
)
from core.search_and_recall import (
    ObjectStoreRecallIndex,
    build_recall_authority_ledger,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
)

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


def _index_rebuild_job_projection(job: Mapping[str, object] | None) -> Mapping[str, object] | None:
    if job is None:
        return None
    error = job.get("error")
    safe_error = None
    if isinstance(error, Mapping):
        safe_error = {
            "code": error.get("code"),
            "message": error.get("message"),
            "retryable": error.get("retryable") is True,
        }
    return {
        "id": job.get("id"), "status": job.get("status"), "attempt": job.get("attempt"),
        "progress": job.get("progress"), "error": safe_error, "updated_at": job.get("updated_at"),
    }


def _index_rebuild_effect_projection(effect) -> dict[str, object]:
    """Return the bounded HTTP view of an Index Effect-v2 operation."""

    state = getattr(effect, "state", None)
    return {
        "operation_id": getattr(effect, "operation_id", None),
        "state": getattr(state, "value", state),
        "receipt_ref": getattr(effect, "result_ref", None),
    }


@router.get("/api/rebuild/index/freshness")
def index_freshness(request: Request, container: ApiContainerDep) -> JSONResponse:
    """返回当前索引新鲜度状态（验收3：新 source 入库后 index stale 可被检测）。

    前端在 source 入库后可调用此路由主动检查 staleness，提示用户重建索引。
    """
    _ = request
    store, _ = product_repositories._object_store(container.root_dir)
    recall_index = ObjectStoreRecallIndex(store)
    active_manifest = recall_index.manifest()
    recall_entries = _current_recall_entries(container.root_dir, store)
    source_ledger = build_recall_authority_ledger(recall_entries)
    try:
        freshness = evaluate_index_freshness(active_manifest, source_ledger)
    except Exception as exc:
        return product_http._json_response(
            400,
            {"detail": "index freshness check failed", "reason": "invalid_source_ledger", "error": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    rebuild_jobs = _job_repository(container.root_dir, store).list_jobs(job_type="rebuild_index")
    latest_job = max(rebuild_jobs, key=lambda item: str(item.get("updated_at") or ""), default=None)
    return product_http._json_response(
        200,
        {
            "status": freshness.status,
            "reason": freshness.reason,
            "source_count": len(source_ledger),
            "has_active_manifest": active_manifest is not None,
            "index_stale": freshness.status != "fresh",
            "can_rebuild": bool(source_ledger),
            "rebuild_job": _index_rebuild_job_projection(latest_job),
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )


@router.post("/api/rebuild/index/rebuild")
def index_rebuild(request: Request, container: ApiContainerDep) -> JSONResponse:
    """触发 SQLite FTS5 索引重建（验收3：新 source 入库后 index stale → 可手动触发 rebuild）。

    流程：
    1. 从 ObjectStore 读当前 source ledger（build_source_ledger_from_object_store）。
    2. 用 evaluate_index_freshness 对比 active manifest 与当前 source ledger。
    3. 若 fresh 直接返回无需重建；否则用 create_index_rebuild_request + create_sqlite_fts5_manifest
       构建 rebuild request + candidate manifest。
    4. 持久化 Job，同步构建与验证候选数据库，再原子激活；失败时保留旧 active 索引。

    返回安全投影后的 completed Job 与 fresh 状态；fresh/empty 请求保持幂等。
    """
    _ = request
    store, settings = product_repositories._object_store(container.root_dir)
    recall_index = ObjectStoreRecallIndex(store)
    active_manifest = recall_index.manifest()
    recall_entries = _current_recall_entries(container.root_dir, store)
    source_ledger = build_recall_authority_ledger(recall_entries)
    if not source_ledger:
        return product_http._json_response(
            200,
            {
                "status": "empty",
                "freshness": {"status": "missing", "reason": "missing_manifest", "source_count": 0},
                "job_id": None,
                "message": "资料库为空，无需重建索引。",
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    try:
        freshness = evaluate_index_freshness(active_manifest, source_ledger)
    except Exception as exc:  # source ledger 里有坏数据
        return product_http._json_response(
            400,
            {"detail": "index rebuild rejected", "reason": "invalid_source_ledger", "error": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    if freshness.status == "fresh":
        return product_http._json_response(
            200,
            {
                "status": "fresh",
                "freshness": {
                    "status": freshness.status,
                    "reason": freshness.reason,
                    "source_count": len(source_ledger),
                },
                "job_id": None,
                "message": "索引已是最新，无需重建。",
            },
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    backend_selection = select_default_recall_backend_policy()
    try:
        rebuild_request = create_index_rebuild_request(
            freshness=freshness,
            backend_selection=backend_selection,
            sources=source_ledger,
        )
        candidate_manifest = create_sqlite_fts5_manifest(
            rebuild_request=rebuild_request,
            backend_selection=backend_selection,
            manifest_id=f"sqlite_fts5_candidate_{rebuild_request.source_fingerprint[:16]}",
        )
    except Exception as exc:
        return product_http._json_response(
            400,
            {"detail": "index rebuild rejected", "reason": "rebuild_request_invalid", "error": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    from core.search_and_recall import sqlite_fts5_manifest_payload

    candidate_payload = sqlite_fts5_manifest_payload(candidate_manifest)
    from core.search_and_recall import ObjectStoreSqliteFts5ManifestRepository
    ObjectStoreSqliteFts5ManifestRepository(
        store,
        manifest_id=candidate_manifest.manifest_id,
    ).save_candidate_manifest(candidate_manifest)
    now = int(time.time())
    admission = IndexRebuildEffectAdmissionFactory(admitted_at=now).build(
        request={
            "id": f"sqlite-fts5-{rebuild_request.source_fingerprint[:32]}",
            "backend_kind": rebuild_request.backend_kind,
            "reason": rebuild_request.reason,
            "source_refs": list(rebuild_request.source_refs),
        },
        manifest={
            "id": str(candidate_payload["id"]),
            "backend_kind": str(candidate_payload["backend_kind"]),
            "source_fingerprint": str(candidate_payload["source_fingerprint"]),
        },
        ledger={
            "id": f"recall-ledger-{rebuild_request.source_fingerprint[:32]}",
            "source_fingerprint": rebuild_request.source_fingerprint,
            "entry_count": len(source_ledger),
        },
    )
    effect_runtime = request.app.state.effect_runtime
    try:
        with sqlite3.connect(effect_runtime.log.database, isolation_level=None) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            effect, _created = SQLiteIndexRebuildEffectAdmission(
                effect_runtime.log,
            ).admit_in_connection(connection, admission, now=now)
            connection.commit()
        settled = effect_runtime.dispatch_operation(effect.operation_id, now=now)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        return product_http._json_response(
            409,
            {"detail": "index rebuild rejected", "reason": "effect_dispatch_failed", "error": str(exc)},
            {"Content-Type": "application/json", "Cache-Control": "no-store"},
        )
    return product_http._json_response(
        200,
        {
            "status": "fresh",
            "freshness": {
                "status": "fresh",
                "reason": None,
                "source_count": len(source_ledger),
            },
            "previous_status": freshness.status,
            "operation_id": settled.operation_id,
            "effect": _index_rebuild_effect_projection(settled),
            "message": "索引重建、验证与激活已完成。",
        },
        {"Content-Type": "application/json", "Cache-Control": "no-store"},
    )
