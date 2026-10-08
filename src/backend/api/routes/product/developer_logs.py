"""Developer logs ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.job_runtime import build_rebuild_job_repository as _job_repository

from core.storage_provider import JsonObjectStore

from . import http as product_http
from . import repositories as product_repositories

router = APIRouter(tags=["rebuild-product-core"])


_DEV_LOG_COMPONENTS = frozenset({"all", "http", "job", "llm"})


_DEV_LOG_STATUS_FILTERS = frozenset({"all", "ok", "fail"})


_DEV_LOG_SENSITIVE_PATTERNS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "bearer",
    "secret",
    "token",
)


_DEV_LOG_SENSITIVE_PATH_HINTS = (
    "\\",
    "C:\\",
    "/home/",
    "/Users/",
    ".env",
)


def _sanitize_dev_log_text(text: str | None) -> str:
    """遮蔽日志文本中可能出现的敏感片段。

    覆盖三类：
    1. api_key/cookie/authorization/bearer/secret/token 关键字后的值
    2. 形如 sk-[A-Za-z0-9]{12,} 的 OpenAI/DeepSeek 风格 API key
    3. 文件路径痕迹（C:\\、/Users/、/home/、.env）
    """
    if not isinstance(text, str) or not text:
        return ""

    # 先用正则把明显的 API key 模式遮蔽（无论是否有关键字前缀）
    sanitized = re.sub(r"sk-[A-Za-z0-9]{12,}", "***REDACTED***", text)

    # 再处理关键字后的值
    lowered = sanitized.lower()
    if any(keyword in lowered for keyword in _DEV_LOG_SENSITIVE_PATTERNS):
        for keyword in _DEV_LOG_SENSITIVE_PATTERNS:
            # 把 "api_key=xxx" / "Authorization: Bearer xxx" 之类替换为标签
            idx = sanitized.lower().find(keyword)
            while idx >= 0:
                end = idx + len(keyword)
                # 保留关键字本身，把后续值替换为 ***REDACTED***
                # 找分隔符 =/:/空格 之后的连续非空白字符
                while end < len(sanitized) and sanitized[end] in "=: \t":
                    end += 1
                value_start = end
                while end < len(sanitized) and sanitized[end] not in " \t\r\n,;\"'":
                    end += 1
                if end > value_start:
                    sanitized = sanitized[:value_start] + "***REDACTED***" + sanitized[end:]
                next_search = sanitized.lower().find(keyword, idx + len(keyword) + len("***REDACTED***"))
                idx = next_search

    # 最后遮蔽文件路径痕迹
    for hint in _DEV_LOG_SENSITIVE_PATH_HINTS:
        if hint in sanitized:
            sanitized = sanitized.replace(hint, "***REDACTED***")

    return sanitized


def _format_llm_dev_log(result: Mapping[str, object]) -> Mapping[str, object]:
    """将 model_result 转换为统一的 dev log 条目。"""
    result_id = str(result.get("id") or "model-result-unknown")
    status_raw = str(result.get("status") or "")
    # completed → ok；其余（privacy_blocked/safety_blocked/error）→ fail
    status = "ok" if status_raw == "completed" else "fail"

    latency = result.get("latency")
    elapsed_ms = 0
    completed_at = ""
    if isinstance(latency, Mapping):
        elapsed_ms = int(latency.get("elapsed_ms") or 0)
        completed_at = str(latency.get("completed_at") or "")

    model_block = result.get("model")
    model_name = ""
    if isinstance(model_block, Mapping):
        model_name = str(model_block.get("name") or "")

    error_block = result.get("error")
    error_text = ""
    if isinstance(error_block, Mapping):
        error_text = _sanitize_dev_log_text(str(error_block.get("message") or ""))

    # stage 优先用 request 的 capability，否则用 status
    stage = f"LLM · {status_raw}" if status_raw else "LLM"

    return {
        "id": f"llm-{result_id}",
        "component": "llm",
        "stage": stage,
        "status": status,
        "duration": f"{elapsed_ms / 1000:.1f}s" if elapsed_ms else "—",
        "duration_ms": elapsed_ms,
        "model": model_name,
        "error": error_text,
        "timestamp": completed_at or str(result.get("created_at") or ""),
        "request_id": str(result.get("request_id") or ""),
        "raw_status": status_raw,
    }


def _format_job_dev_log(job: Mapping[str, object]) -> Mapping[str, object]:
    """将 job 记录转换为统一的 dev log 条目。"""
    job_id = str(job.get("id") or job.get("job_id") or "job-unknown")
    status_raw = str(job.get("status") or "")
    # completed → ok；failed/cancelled → fail；running/pending/waiting_user → ok（in-progress 视为非错误）
    if status_raw == "completed":
        status = "ok"
    elif status_raw in {"failed", "cancelled"}:
        status = "fail"
    else:
        status = "ok"

    job_type = str(job.get("job_type") or "job")
    stage = f"Job · {job_type}"

    error_text = _sanitize_dev_log_text(str(job.get("error") or ""))

    timestamp = str(job.get("updated_at") or job.get("created_at") or "")

    return {
        "id": f"job-{job_id}",
        "component": "job",
        "stage": stage,
        "status": status,
        "duration": "—",
        "duration_ms": 0,
        "model": "",
        "error": error_text,
        "timestamp": timestamp,
        "raw_status": status_raw,
    }


def _format_http_dev_log(entry: Mapping[str, object]) -> Mapping[str, object]:
    """将 access log buffer 条目转换为统一的 dev log 条目。"""
    status_code = int(entry.get("status") or 0)
    status = "ok" if 200 <= status_code < 400 else "fail"
    duration_ms = int(entry.get("duration_ms") or 0)
    method = str(entry.get("method") or "")
    path = str(entry.get("path") or "")
    error_text = "" if status == "ok" else f"HTTP {status_code}"
    return {
        "id": str(entry.get("id") or ""),
        "component": "http",
        "stage": f"{method} {path}".strip(),
        "status": status,
        "duration": f"{duration_ms / 1000:.2f}s" if duration_ms else "—",
        "duration_ms": duration_ms,
        "model": "",
        "error": error_text,
        "timestamp": str(entry.get("timestamp") or ""),
        "raw_status": str(status_code),
    }


def _aggregate_developer_studio_logs(
    *,
    component: str,
    status_filter: str,
    limit: int,
    offset: int,
    store: JsonObjectStore,
    access_log_buffer: object | None,
) -> Mapping[str, object]:
    """聚合 3 类日志，应用过滤/排序/分页。"""
    entries: list[Mapping[str, object]] = []

    if component in ("all", "http") and access_log_buffer is not None:
        for entry in access_log_buffer.list():
            entries.append(_format_http_dev_log(entry))

    if component in ("all", "job"):
        repository = _job_repository(store.root.parent, store)
        for job in repository.all():
            entries.append(_format_job_dev_log(job))

    if component in ("all", "llm"):
        for result in store.list("model_results"):
            if isinstance(result, Mapping):
                entries.append(_format_llm_dev_log(result))

    # 状态过滤
    if status_filter != "all":
        entries = [entry for entry in entries if entry.get("status") == status_filter]

    # 按 timestamp 降序排序（空字符串排到最后）
    entries.sort(key=lambda entry: entry.get("timestamp") or "", reverse=True)

    total = len(entries)
    page = entries[offset : offset + limit] if offset >= 0 else entries[:limit]

    return {
        "logs": page,
        "total": total,
        "filters": {
            "component": component,
            "status": status_filter,
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/api/rebuild/developer-studio/logs")
def developer_studio_logs(request: Request, container: ApiContainerDep) -> JSONResponse:
    """Developer Studio 诊断日志聚合端点。

    Query params:
    - component: all|http|job|llm（默认 all）
    - status:    all|ok|fail（默认 all）
    - limit:     1..100（默认 50）
    - offset:    >= 0（默认 0）
    """
    component = (product_http._optional_query_str(request, "component") or "all").strip().lower()
    if component not in _DEV_LOG_COMPONENTS:
        return product_http._json_response(
            400,
            {"detail": f"invalid component: {component}; expected one of {sorted(_DEV_LOG_COMPONENTS)}"},
            product_http._no_store_headers(),
        )
    status_filter = (product_http._optional_query_str(request, "status") or "all").strip().lower()
    if status_filter not in _DEV_LOG_STATUS_FILTERS:
        return product_http._json_response(
            400,
            {"detail": f"invalid status: {status_filter}; expected one of {sorted(_DEV_LOG_STATUS_FILTERS)}"},
            product_http._no_store_headers(),
        )

    limit_str = request.query_params.get("limit", "50").strip()
    try:
        limit = int(limit_str)
    except ValueError:
        return product_http._json_response(
            400,
            {"detail": f"invalid limit: {limit_str}; expected integer"},
            product_http._no_store_headers(),
        )
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    offset_str = request.query_params.get("offset", "0").strip()
    try:
        offset = int(offset_str)
    except ValueError:
        return product_http._json_response(
            400,
            {"detail": f"invalid offset: {offset_str}; expected integer"},
            product_http._no_store_headers(),
        )
    if offset < 0:
        offset = 0

    store, _settings = product_repositories._object_store(container.root_dir)
    access_log_buffer = getattr(request.app.state, "access_log_buffer", None)
    payload = _aggregate_developer_studio_logs(
        component=component,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
        store=store,
        access_log_buffer=access_log_buffer,
    )
    return product_http._json_response(200, payload, product_http._no_store_headers())
