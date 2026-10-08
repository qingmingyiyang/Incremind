from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable, Mapping
from threading import Lock
from uuid import uuid4

from backend.shared.runtime_logging import redact_log_text


class SuppressPathAccessLogFilter(logging.Filter):
    def __init__(self, *, paths: Iterable[str]) -> None:
        super().__init__()
        self._paths = frozenset(paths)

    def filter(self, record: logging.LogRecord) -> bool:
        request_path = _extract_request_path(record.getMessage())
        if request_path is None:
            return True
        return request_path not in self._paths


def install_access_log_filters() -> None:
    logger = logging.getLogger("uvicorn.access")
    for item in logger.filters:
        if isinstance(item, SuppressPathAccessLogFilter):
            return
    logger.addFilter(
        SuppressPathAccessLogFilter(
            paths={
                "/api/agent/memory/status",
                "/api/rag/models",
            },
        )
    )


def _extract_request_path(message: str) -> str | None:
    match = re.search(r'"[A-Z]+ (?P<target>\S+) HTTP/\d(?:\.\d)?"', message)
    if match is None:
        return None
    target = match.group("target")
    return target.split("?", 1)[0]


# ─── Developer Studio 诊断日志：HTTP access 内存环形缓冲 ───
#
# 设计动机：uvicorn.access 仅输出到日志 handler，不持久化。Developer Studio
# 的"3 类日志"视图需要一个可读的 HTTP access 源。为了避免磁盘膨胀和写入
# 开销，这里采用进程内 deque 环形缓冲，重启后清空——这对诊断场景足够。
class AccessLogBuffer:
    """线程安全的 HTTP access 日志内存环形缓冲。

    容量固定，超出后自动丢弃最旧条目。重启后清空（仅用于实时诊断）。
    """

    __slots__ = ("_entries", "_lock", "_capacity")

    def __init__(self, capacity: int = 200) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._entries: deque[Mapping[str, object]] = deque(maxlen=capacity)
        self._lock = Lock()

    def append(self, entry: Mapping[str, object]) -> None:
        with self._lock:
            self._entries.append(dict(entry))

    def list(self) -> list[Mapping[str, object]]:
        with self._lock:
            return [dict(item) for item in self._entries]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# 跳过采集的路径前缀/精确匹配——避免 SSE 长连接、健康检查、静态资源污染日志
_SUPPRESSED_PATH_PREFIXES: tuple[str, ...] = (
    "/api/rebuild/jobs/",  # SSE 流式端点前缀（包含 /stream）
    "/assets/",
    "/static/",
)
_SUPPRESSED_PATH_EXACT: frozenset[str] = frozenset(
    {
        "/api/health",
        "/",
        "/index.html",
        "/favicon.ico",
    }
)


def should_capture_request(path: str) -> bool:
    """判断该请求路径是否应被 access log 缓冲采集。"""
    if not path:
        return False
    if path in _SUPPRESSED_PATH_EXACT:
        return False
    for prefix in _SUPPRESSED_PATH_PREFIXES:
        if path.startswith(prefix):
            return False
    return True


def make_access_log_entry(*, method: str, path: str, status: int, duration_ms: int, timestamp: str) -> Mapping[str, object]:
    """构造单条 access log 条目（已脱敏：不采集 headers/body/query）。"""
    path = redact_log_text(path)
    return {
        "id": f"http-{uuid4().hex[:12]}",
        "component": "http",
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": duration_ms,
        "timestamp": timestamp,
        "stage": f"{method} {path}",
        # error 字段在端点聚合时根据 status 推断
    }
