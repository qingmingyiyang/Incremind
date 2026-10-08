from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from backend.api.routes import workbench_auto_intake


@dataclass(frozen=True)
class _Response:
    status_code: int
    body: dict[str, object]
    headers: dict[str, str]


class _Repository:
    def __init__(self, job: object) -> None:
        self._job = job
        self.calls = 0

    def get(self, job_id: str) -> object:
        self.calls += 1
        return self._job


class _Store:
    def __init__(self, sources: dict[str, object]) -> None:
        self._sources = sources
        self.calls = 0

    def read(self, collection: str, source_id: str) -> object:
        assert collection == "sources"
        self.calls += 1
        return self._sources.get(source_id)


def _response(job_id: str = "job-transform-1") -> _Response:
    return _Response(202, {"job_id": job_id, "items": []}, {"Cache-Control": "no-store"})


def _transform_job(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": "job-transform-1",
        "job_type": "workbench_content_transform",
        "execution_version": "effect-v2",
        "transform_items": items,
    }


def test_task_reference_helper_rejects_mixed_project_sources() -> None:
    response = workbench_auto_intake._with_workbench_transform_task_reference(
        _response(),
        repository=_Repository(_transform_job([{"source_id": "source-a"}, {"source_id": "source-b"}])),
        object_store=_Store({
            "source-a": {"id": "source-a", "project_id": "project-a"},
            "source-b": {"id": "source-b", "project_id": "project-b"},
        }),
    )
    assert "task_ref" not in response.body
    assert "project_id" not in response.body


def test_task_reference_helper_rejects_missing_source() -> None:
    response = workbench_auto_intake._with_workbench_transform_task_reference(
        _response(),
        repository=_Repository(_transform_job([{"source_id": "source-missing"}])),
        object_store=_Store({}),
    )
    assert "task_ref" not in response.body
    assert "project_id" not in response.body


def test_task_reference_helper_rejects_ordinary_auto_intake_job() -> None:
    response = workbench_auto_intake._with_workbench_transform_task_reference(
        _response(),
        repository=_Repository({
            "id": "job-transform-1", "job_type": "workbench_auto_intake",
            "execution_version": "effect-v2", "transform_items": [{"source_id": "source-a"}],
        }),
        object_store=_Store({"source-a": {"id": "source-a", "project_id": "project-a"}}),
    )
    assert "task_ref" not in response.body
    assert "project_id" not in response.body


def test_execute_does_not_build_job_repository_without_a_successful_job_response(monkeypatch, tmp_path) -> None:
    response = _Response(400, {"detail": "rejected"}, {})
    monkeypatch.setattr(workbench_auto_intake, "build_rebuild_object_store", lambda _root: (object(), SimpleNamespace(namespace_id="test")))
    monkeypatch.setattr(
        workbench_auto_intake,
        "build_workbench_auto_intake_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(execute=lambda **_execute_kwargs: response),
    )
    monkeypatch.setattr(
        workbench_auto_intake,
        "build_rebuild_job_repository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("repository must not be composed")),
    )
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/api/rebuild/workbench/auto-intake", query=""), app=object())
    container = SimpleNamespace(root_dir=tmp_path)
    assert workbench_auto_intake._execute_auto_intake(request, container, {"content": "x"}) is response
