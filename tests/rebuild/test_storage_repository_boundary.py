from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.memory_core import ObjectStoreMemoryStore
from core.product_core import SourceJobMemoryLoop
from core.storage_provider import JsonObjectStore, StorageConfigurationError
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _persistent_parts(
    rebuild_root: Path,
    legacy_root: Path,
) -> tuple[
    SourceJobMemoryLoop,
    ObjectStoreSourceRegistrar,
    ObjectStoreJobRepository,
    ObjectStoreMemoryStore,
]:
    object_store = JsonObjectStore(rebuild_root, legacy_root=legacy_root)
    source_registrar = ObjectStoreSourceRegistrar(object_store)
    job_repository = ObjectStoreJobRepository(object_store)
    memory_store = ObjectStoreMemoryStore(object_store)
    loop = SourceJobMemoryLoop(
        source_registrar=source_registrar,
        job_repository=job_repository,
        memory_reader=memory_store,
        memory_writer=memory_store,
    )
    return loop, source_registrar, job_repository, memory_store


def test_object_store_loop_survives_repository_reload(tmp_path: Path) -> None:
    rebuild_root = tmp_path / ".rebuild-data"
    legacy_root = tmp_path / "library"
    loop, _source_registrar, _job_repository, _memory_store = _persistent_parts(
        rebuild_root,
        legacy_root,
    )

    result = loop.run_text(
        title="Persistent Source",
        content="Persistent repository keeps Source, Job, Atom, Scenario and Series Memory.",
        series_id="series-default",
        project_id="project-alpha",
    )

    assert result.status == "completed"
    _reloaded_loop, reloaded_sources, reloaded_jobs, reloaded_memory = _persistent_parts(
        rebuild_root,
        legacy_root,
    )
    source = reloaded_sources.get(result.source_id)
    job = reloaded_jobs.get(result.job_id)
    atom = reloaded_memory.get("atom", result.atom_id or "")
    scenario = reloaded_memory.get("scenario", result.scenario_id or "")
    series_memory = reloaded_memory.get("series_memory", result.series_memory_id or "")

    assert source is not None
    assert job is not None
    assert atom is not None
    assert scenario is not None
    assert series_memory is not None
    assert job["status"] == "completed"
    assert job["checkpoint"] is None
    assert validate_contract_instance("source.schema.json", _schema("source.schema.json"), source) == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []
    assert validate_contract_instance("atom.schema.json", _schema("atom.schema.json"), atom) == []
    assert validate_contract_instance("scenario.schema.json", _schema("scenario.schema.json"), scenario) == []
    assert (
        validate_contract_instance(
            "series_memory.schema.json",
            _schema("series_memory.schema.json"),
            series_memory,
        )
        == []
    )
    assert tuple(item["id"] for item in reloaded_memory.list_by_source(result.source_id)) == (
        result.atom_id,
        result.scenario_id,
        result.series_memory_id,
    )
    assert (rebuild_root / "objects" / "default" / "jobs" / f"{result.job_id}.json").exists()
    assert not legacy_root.exists()


def test_object_store_reload_keeps_failed_checkpoint_and_staged_memory(tmp_path: Path) -> None:
    rebuild_root = tmp_path / ".rebuild-data"
    legacy_root = tmp_path / "library"
    loop, _source_registrar, _job_repository, _memory_store = _persistent_parts(
        rebuild_root,
        legacy_root,
    )

    result = loop.run_text(
        title="Persistent failure",
        content="This Source should stage an Atom but fail before publish.",
        fail_after_staging=True,
    )

    assert result.status == "failed"
    _reloaded_loop, reloaded_sources, reloaded_jobs, reloaded_memory = _persistent_parts(
        rebuild_root,
        legacy_root,
    )
    source = reloaded_sources.get(result.source_id)
    job = reloaded_jobs.get(result.job_id)

    assert source is not None
    assert job is not None
    assert job["status"] == "failed"
    assert job["checkpoint"]["resume_step"] == "publish_atom"
    assert job["published_outputs"] == []
    staged_atom_id = job["staged_outputs"][0]["object_id"]
    assert reloaded_memory.staged("atom", staged_atom_id) is not None
    assert reloaded_memory.get("atom", staged_atom_id) is None
    assert reloaded_memory.list_by_source(result.source_id) == ()
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []
    assert not legacy_root.exists()


def test_object_store_resume_publish_after_checkpoint_reload_is_idempotent(tmp_path: Path) -> None:
    rebuild_root = tmp_path / ".rebuild-data"
    legacy_root = tmp_path / "library"
    loop, _source_registrar, _job_repository, _memory_store = _persistent_parts(
        rebuild_root,
        legacy_root,
    )

    failed = loop.run_text(
        title="Persistent resume",
        content="This Source should resume from staged Atom.\nIt should publish memory once.",
        series_id="series-default",
        project_id="project-alpha",
        fail_after_staging=True,
    )

    reloaded_loop, _reloaded_sources, reloaded_jobs, reloaded_memory = _persistent_parts(
        rebuild_root,
        legacy_root,
    )
    resumed = reloaded_loop.resume_publish(
        failed.job_id,
        series_id="series-default",
        project_id="project-alpha",
    )
    repeated = reloaded_loop.resume_publish(
        failed.job_id,
        series_id="series-default",
        project_id="project-alpha",
    )
    job = reloaded_jobs.get(failed.job_id)

    assert resumed.status == "completed"
    assert repeated == resumed
    assert len(resumed.atom_ids) == 2
    assert job is not None
    assert job["status"] == "completed"
    assert job["error"] is None
    assert job["checkpoint"] is None
    assert job["staged_outputs"] == []
    assert {item["kind"] for item in job["published_outputs"]} == {"atom", "scenario", "series_memory"}
    assert [item["kind"] for item in job["published_outputs"]].count("atom") == 2
    assert [step["name"] for step in job["steps"]].count("publish_memory") == 1
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []

    assert resumed.atom_id is not None
    assert resumed.scenario_id is not None
    assert resumed.series_memory_id is not None
    for atom_id in resumed.atom_ids:
        assert reloaded_memory.staged("atom", atom_id) is None
    atoms = [reloaded_memory.get("atom", atom_id) for atom_id in resumed.atom_ids]
    scenario = reloaded_memory.get("scenario", resumed.scenario_id)
    series_memory = reloaded_memory.get("series_memory", resumed.series_memory_id)
    assert all(atom is not None for atom in atoms)
    assert scenario is not None
    assert series_memory is not None
    for atom in atoms:
        assert atom is not None
        assert validate_contract_instance("atom.schema.json", _schema("atom.schema.json"), atom) == []
    assert validate_contract_instance("scenario.schema.json", _schema("scenario.schema.json"), scenario) == []
    assert (
        validate_contract_instance(
            "series_memory.schema.json",
            _schema("series_memory.schema.json"),
            series_memory,
        )
        == []
    )
    assert len(reloaded_memory.transitions()) == 2
    assert tuple(item["id"] for item in reloaded_memory.list_by_source(failed.source_id)) == (
        *resumed.atom_ids,
        resumed.scenario_id,
        resumed.series_memory_id,
    )
    assert not legacy_root.exists()


def test_object_store_series_memory_merges_across_repository_reload(tmp_path: Path) -> None:
    rebuild_root = tmp_path / ".rebuild-data"
    legacy_root = tmp_path / "library"
    loop, _source_registrar, _job_repository, _memory_store = _persistent_parts(
        rebuild_root,
        legacy_root,
    )

    first = loop.run_text(
        title="Persistent series first",
        content="First persistent source fact.",
        series_id="series-persistent",
        project_id="project-alpha",
    )

    reloaded_loop, _reloaded_sources, _reloaded_jobs, reloaded_memory = _persistent_parts(
        rebuild_root,
        legacy_root,
    )
    second = reloaded_loop.run_text(
        title="Persistent series second",
        content="Second persistent source fact.",
        series_id="series-persistent",
        project_id="project-beta",
    )
    merged = reloaded_memory.get("series_memory", first.series_memory_id or "")

    assert second.series_memory_id == first.series_memory_id
    assert merged is not None
    assert merged["scenario_ids"] == [first.scenario_id, second.scenario_id]
    assert merged["project_ids"] == ["project-alpha", "project-beta"]
    assert merged["revision"] == 2
    assert "2 traceable Source-derived scenario" in merged["overview"]
    assert len(merged["source_refs"]) == 2
    assert validate_contract_instance("series_memory.schema.json", _schema("series_memory.schema.json"), merged) == []
    assert not legacy_root.exists()


def test_object_store_rejects_legacy_library_overlap(tmp_path: Path) -> None:
    legacy_root = tmp_path / "library"

    with pytest.raises(StorageConfigurationError):
        JsonObjectStore(legacy_root, legacy_root=legacy_root)

    with pytest.raises(StorageConfigurationError):
        JsonObjectStore(legacy_root / ".rebuild-data", legacy_root=legacy_root)

    with pytest.raises(StorageConfigurationError):
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path)
