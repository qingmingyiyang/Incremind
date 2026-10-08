from __future__ import annotations

import hashlib

import pytest

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository, RoutedJobRepository, SQLiteJobStore
from core.memory_core import ObjectStoreMemoryStore
from core.product_core import SourceJobMemoryLoop
from core.storage_provider import JsonObjectStore


class _FailFirstJobSave:
    def save(self, _job):
        raise RuntimeError("injected job create failure")


class _FailMemoryPublish:
    def __init__(self, delegate: ObjectStoreMemoryStore) -> None:
        self._delegate = delegate

    def get(self, layer: str, object_id: str):
        return self._delegate.get(layer, object_id)

    def list_by_source(self, source_id: str):
        return self._delegate.list_by_source(source_id)

    def save_candidate(self, layer: str, payload):
        return self._delegate.save_candidate(layer, payload)

    def publish(self, _layer: str, _payload):
        raise RuntimeError("injected memory publish failure")

    def set_trust_status(self, object_id: str, trust_status: str, reason: str) -> None:
        self._delegate.set_trust_status(object_id, trust_status, reason)

    def staged(self, layer: str, object_id: str):
        return self._delegate.staged(layer, object_id)


def _parts(tmp_path):
    rebuild_root = tmp_path / ".rebuild-data"
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    jobs = RoutedJobRepository(
        legacy=ObjectStoreJobRepository(store),
        sqlite=SQLiteJobStore(rebuild_root / "jobs.sqlite3"),
        sqlite_job_types=frozenset({"extract_memory"}),
    )
    memory = ObjectStoreMemoryStore(store)
    return rebuild_root, store, ObjectStoreSourceRegistrar(store), jobs, memory


def _loop(source_registrar, jobs, memory_reader, memory_writer) -> SourceJobMemoryLoop:
    return SourceJobMemoryLoop(
        source_registrar=source_registrar,
        job_repository=jobs,
        memory_reader=memory_reader,
        memory_writer=memory_writer,
    )


def test_normal_source_job_memory_loop_uses_json_and_jobs_sqlite_authorities(tmp_path) -> None:
    rebuild_root, store, sources, jobs, memory = _parts(tmp_path)

    result = _loop(sources, jobs, memory, memory).run_text(
        title="transaction characterization",
        content="Source, SQLite Job and Memory currently have separate authorities.",
    )

    assert store.read("sources", result.source_id) is not None
    assert jobs.sqlite.read(result.job_id) is not None
    assert ObjectStoreJobRepository(store).get(result.job_id) is None
    assert memory.get("atom", result.atom_id or "") is not None
    assert (rebuild_root / "jobs.sqlite3").exists()
    assert not (rebuild_root / "structured-records.sqlite3").exists()


def test_job_create_failure_leaves_json_source_without_job_or_memory(tmp_path) -> None:
    _rebuild_root, store, sources, _jobs, memory = _parts(tmp_path)
    loop = _loop(sources, _FailFirstJobSave(), memory, memory)

    with pytest.raises(RuntimeError, match="job create failure"):
        loop.run_text(title="partial source", content="The Source write happens before Job creation.")

    source_id = "source-text-" + hashlib.sha256(
        b"The Source write happens before Job creation."
    ).hexdigest()[:12]
    assert store.read("sources", source_id) is not None
    assert ObjectStoreJobRepository(store).all() == ()
    assert memory.list_by_source(source_id) == ()


def test_memory_publish_failure_leaves_sqlite_job_and_json_staged_memory(tmp_path) -> None:
    _rebuild_root, store, sources, jobs, memory = _parts(tmp_path)
    failing_memory = _FailMemoryPublish(memory)
    loop = _loop(sources, jobs, failing_memory, failing_memory)

    with pytest.raises(RuntimeError, match="memory publish failure"):
        loop.run_text(title="partial memory", content="The Job checkpoint persists before Memory publication.")

    source_id = "source-text-" + hashlib.sha256(
        b"The Job checkpoint persists before Memory publication."
    ).hexdigest()[:12]
    job_id = f"job-extract-{source_id}"
    job = jobs.get(job_id)
    assert store.read("sources", source_id) is not None
    assert job is not None
    assert job["status"] == "running"
    assert job["checkpoint"]["resume_step"] == "publish_atom"
    staged_atom_id = job["staged_outputs"][0]["object_id"]
    assert memory.staged("atom", staged_atom_id) is not None
    assert memory.get("atom", staged_atom_id) is None
