from __future__ import annotations

from inspect import signature
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.api.job_lifecycle_runtime import JobLifecycleConfigurationConflict, get_or_create_rebuild_job_lifecycle
from core.job_runner import InMemoryJobRepository, RoutedJobRepository, SQLiteJobStore


class _App:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            effect_runtime=SimpleNamespace(
                dispatch_operation=lambda *_args, **_kwargs: None,
            )
        )


def _repository(tmp_path) -> RoutedJobRepository:
    return RoutedJobRepository(
        legacy=InMemoryJobRepository(),
        sqlite=SQLiteJobStore(tmp_path / "jobs.sqlite3"),
        sqlite_job_types=frozenset(),
    )


def test_factory_returns_one_app_scoped_lifecycle_for_the_same_handler_set(tmp_path) -> None:
    app = _App()
    repository = _repository(tmp_path)
    first = get_or_create_rebuild_job_lifecycle(app, repository, object(), namespace_id="default")
    second = get_or_create_rebuild_job_lifecycle(app, repository, object(), namespace_id="default")
    assert first is second
    assert first.job_types == frozenset({"extract_memory"})


def test_factory_serializes_concurrent_first_creation(tmp_path) -> None:
    app = _App()
    repository = _repository(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        lifecycles = tuple(
            pool.map(
                lambda _: get_or_create_rebuild_job_lifecycle(
                    app, repository, object(), namespace_id="default"
                ),
                range(24),
            )
        )
    assert len({id(lifecycle) for lifecycle in lifecycles}) == 1


def test_factory_has_no_media_handler_registration_seam(tmp_path) -> None:
    assert "media_handler" not in signature(
        get_or_create_rebuild_job_lifecycle
    ).parameters
    lifecycle = get_or_create_rebuild_job_lifecycle(
        _App(), _repository(tmp_path), object(), namespace_id="default"
    )
    assert "media_hands" not in lifecycle.job_types


def test_factory_rejects_preexisting_unverifiable_lifecycle_configuration(tmp_path) -> None:
    app = _App()
    repository = _repository(tmp_path)
    app.state.rebuild_job_lifecycle = object()
    with pytest.raises(JobLifecycleConfigurationConflict, match="unsupported"):
        get_or_create_rebuild_job_lifecycle(
            app, repository, object(), namespace_id="default"
        )


def test_factory_rejects_database_or_namespace_authority_drift(tmp_path) -> None:
    app = _App()
    get_or_create_rebuild_job_lifecycle(app, _repository(tmp_path), object(), namespace_id="default")
    with pytest.raises(JobLifecycleConfigurationConflict, match="authority"):
        get_or_create_rebuild_job_lifecycle(
            app,
            _repository(tmp_path / "other"),
            object(),
            namespace_id="default",
        )
