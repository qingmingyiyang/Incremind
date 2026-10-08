"""Job lifecycle ownership for the product API."""
from __future__ import annotations

from pathlib import Path
import time

from fastapi import FastAPI, Request

from backend.api.media_hands_composition import compose_media_hands_lifecycle

from core.effect_log import EffectState
from core.job_runner import RoutedJobRepository, SQLiteJobRuntimeLifecycle
from core.storage_provider import JsonObjectStore

from . import repositories as product_repositories


def recover_rebuild_job_lifecycle(
    application: FastAPI,
    runtime_root: Path,
) -> tuple[str, ...]:
    store, _settings = product_repositories._object_store(runtime_root)
    effect_runtime = getattr(application.state, "effect_runtime", None)
    if effect_runtime is None:
        raise RuntimeError("Core Effect Runtime is unavailable")
    recovered_jobs: list[str] = []
    for planned in effect_runtime.log.planned_for_kinds(("job_execution",), limit=100):
        settled = effect_runtime.dispatch_operation(
            planned.operation_id, now=int(time.time())
        )
        if settled.state is EffectState.SETTLED_OK:
            recovered_jobs.append(settled.root_id)
    recovered = tuple(recovered_jobs)
    return recovered


def shutdown_rebuild_job_lifecycle(application: FastAPI, *, timeout_seconds: float = 5.0) -> tuple[str, ...]:
    lifecycle = getattr(application.state, "rebuild_job_lifecycle", None)
    if lifecycle is None:
        return ()
    return lifecycle.shutdown(timeout_seconds=timeout_seconds)


def _job_lifecycle(request: Request, store: JsonObjectStore, repository: RoutedJobRepository) -> SQLiteJobRuntimeLifecycle:
    return _job_lifecycle_for_application(request.app, store, repository)


def _job_lifecycle_for_application(
    application: FastAPI,
    store: JsonObjectStore,
    repository: RoutedJobRepository,
) -> SQLiteJobRuntimeLifecycle:
    return compose_media_hands_lifecycle(
        application,
        runtime_root=getattr(application.state.container, "root_dir"),
        object_store=store,
        repository=repository,
        namespace_id=store.namespace_id,
    ).lifecycle
