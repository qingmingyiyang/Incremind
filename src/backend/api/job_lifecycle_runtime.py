from __future__ import annotations

import time
from threading import Lock
from typing import Protocol

from core.job_runner import (
    RoutedJobRepository,
    SQLiteJobRuntimeLifecycle,
    job_execution_operation_id,
)


class JobLifecycleConfigurationConflict(RuntimeError):
    """An app already owns a lifecycle with a different handler set."""


class ApplicationPort(Protocol):
    state: object


_LIFECYCLE_LOCK = Lock()
_STATE_LIFECYCLE = "rebuild_job_lifecycle"
_STATE_AUTHORITY = "rebuild_job_lifecycle_authority"


def get_or_create_rebuild_job_lifecycle(
    application: ApplicationPort,
    repository: RoutedJobRepository,
    object_store: object,
    *,
    namespace_id: str,
) -> SQLiteJobRuntimeLifecycle:
    """Return the sole app Core Effect dispatch queue."""

    requested_authority = (repository.sqlite.database_path, namespace_id)
    with _LIFECYCLE_LOCK:
        existing = getattr(application.state, _STATE_LIFECYCLE, None)
        if existing is None:
            effect_runtime = getattr(application.state, "effect_runtime", None)
            if effect_runtime is None:
                raise JobLifecycleConfigurationConflict(
                    "Core Effect Runtime is required for Job execution"
                )

            def dispatch_job(job_id: str) -> object:
                if effect_runtime is None:
                    raise RuntimeError("Core Effect Runtime is unavailable")
                record = repository.sqlite.read(job_id)
                if record is None:
                    raise RuntimeError("Job projection disappeared before dispatch")
                attempt = record.payload.get("attempt", 0)
                if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
                    raise RuntimeError("Job attempt is invalid")
                return effect_runtime.dispatch_operation(
                    job_execution_operation_id(job_id, attempt), now=int(time.time())
                )

            lifecycle = SQLiteJobRuntimeLifecycle(
                repository.sqlite,
                execution_dispatcher=dispatch_job,
            )
            setattr(application.state, _STATE_LIFECYCLE, lifecycle)
            setattr(application.state, _STATE_AUTHORITY, requested_authority)
            return lifecycle
        if not isinstance(existing, SQLiteJobRuntimeLifecycle):
            raise JobLifecycleConfigurationConflict("app job lifecycle has an unsupported type")
        if getattr(application.state, _STATE_AUTHORITY, None) != requested_authority:
            raise JobLifecycleConfigurationConflict("app job lifecycle authority conflicts with requested configuration")
        return existing
