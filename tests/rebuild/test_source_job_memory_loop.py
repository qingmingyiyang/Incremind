from __future__ import annotations

import json
from pathlib import Path

from core.ingestion_core import DeterministicSourceRegistrar
from core.job_runner import InMemoryJobRepository
from core.memory_core import InMemoryMemoryStore
from core.product_core import SourceJobMemoryLoop
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _build_loop() -> tuple[
    SourceJobMemoryLoop,
    DeterministicSourceRegistrar,
    InMemoryJobRepository,
    InMemoryMemoryStore,
]:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    memory_store = InMemoryMemoryStore()
    loop = SourceJobMemoryLoop(
        source_registrar=source_registrar,
        job_repository=job_repository,
        memory_reader=memory_store,
        memory_writer=memory_store,
    )
    return loop, source_registrar, job_repository, memory_store


def test_source_job_memory_loop_publishes_traceable_memory_objects() -> None:
    loop, source_registrar, job_repository, memory_store = _build_loop()

    result = loop.run_text(
        title="Phase 2 fixture input",
        content="Phase 2 starts with Source, then Job, then traceable memory.",
        series_id="series-default",
        project_id="project-alpha",
    )

    assert result.status == "completed"
    assert result.atom_id is not None
    assert result.scenario_id is not None
    assert result.series_memory_id is not None
    source = source_registrar.get(result.source_id)
    job = job_repository.get(result.job_id)
    atom = memory_store.get("atom", result.atom_id)
    scenario = memory_store.get("scenario", result.scenario_id)
    series_memory = memory_store.get("series_memory", result.series_memory_id)

    assert source is not None
    assert job is not None
    assert atom is not None
    assert scenario is not None
    assert series_memory is not None
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
    assert atom["source_id"] == result.source_id
    assert scenario["atom_ids"] == [result.atom_id]
    assert series_memory["scenario_ids"] == [result.scenario_id]
    assert {item["kind"] for item in job["published_outputs"]} == {"atom", "scenario", "series_memory"}
    assert result.published_output_ids == (result.atom_id, result.scenario_id, result.series_memory_id)
    assert result.atom_ids == (result.atom_id,)


def test_source_job_memory_loop_splits_text_into_multiple_atoms_with_stable_locators() -> None:
    loop, _source_registrar, job_repository, memory_store = _build_loop()
    content = "First durable fact.\n\nSecond durable fact.\n  Third durable fact.  \n"

    result = loop.run_text(
        title="Multi atom fixture input",
        content=content,
        series_id="series-default",
    )

    assert result.status == "completed"
    assert result.atom_id == result.atom_ids[0]
    assert len(result.atom_ids) == 3
    job = job_repository.get(result.job_id)
    assert job is not None
    assert [output["kind"] for output in job["published_outputs"]].count("atom") == 3
    atoms = [memory_store.get("atom", atom_id) for atom_id in result.atom_ids]
    assert all(atom is not None for atom in atoms)
    locators = [atom["source_refs"][0]["locator"] for atom in atoms if atom is not None]
    quotes = [atom["source_refs"][0]["quote"] for atom in atoms if atom is not None]
    assert locators == ["char:0-19", "char:21-41", "char:44-63"]
    assert quotes == ["First durable fact.", "Second durable fact.", "Third durable fact."]
    scenario = memory_store.get("scenario", result.scenario_id or "")
    assert scenario is not None
    assert scenario["atom_ids"] == list(result.atom_ids)
    assert [ref["locator"] for ref in scenario["source_refs"]] == locators
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []
    for atom in atoms:
        assert atom is not None
        assert validate_contract_instance("atom.schema.json", _schema("atom.schema.json"), atom) == []
    assert validate_contract_instance("scenario.schema.json", _schema("scenario.schema.json"), scenario) == []


def test_source_job_memory_loop_merges_series_memory_without_overwriting_user_overview() -> None:
    loop, _source_registrar, _job_repository, memory_store = _build_loop()

    first = loop.run_text(
        title="First series input",
        content="First source fact.",
        series_id="series-shared",
        project_id="project-alpha",
    )
    series_memory = memory_store.get("series_memory", first.series_memory_id or "")
    assert series_memory is not None
    user_edited = dict(series_memory)
    user_edited["overview"] = "User edited overview must stay stable."
    user_edited["trust_status"] = "user_confirmed"
    user_edited["revision"] = 7
    memory_store.publish("series_memory", user_edited)

    second = loop.run_text(
        title="Second series input",
        content="Second source fact.\nThird source fact.",
        series_id="series-shared",
        project_id="project-beta",
    )
    merged = memory_store.get("series_memory", first.series_memory_id or "")

    assert second.series_memory_id == first.series_memory_id
    assert merged is not None
    assert merged["overview"] == "User edited overview must stay stable."
    assert merged["trust_status"] == "user_confirmed"
    assert merged["scenario_ids"] == [first.scenario_id, second.scenario_id]
    assert merged["project_ids"] == ["project-alpha", "project-beta"]
    assert merged["scope"] == "project"
    assert merged["revision"] == 8
    assert len(merged["source_refs"]) == 3
    assert validate_contract_instance("series_memory.schema.json", _schema("series_memory.schema.json"), merged) == []


def test_source_job_memory_loop_keeps_staged_atom_unpublished_on_failure() -> None:
    loop, _source_registrar, job_repository, memory_store = _build_loop()

    result = loop.run_text(
        title="Phase 2 failure input",
        content="This source should stage an atom but fail before publishing.",
        fail_after_staging=True,
    )

    assert result.status == "failed"
    assert result.atom_id is None
    job = job_repository.get(result.job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert job["published_outputs"] == []
    assert job["staged_outputs"]
    staged_atom_id = job["staged_outputs"][0]["object_id"]
    assert memory_store.staged("atom", staged_atom_id) is not None
    assert memory_store.get("atom", staged_atom_id) is None
    assert memory_store.list_by_source(result.source_id) == ()
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), job) == []


def test_source_job_memory_loop_rejects_empty_text_before_job_creation() -> None:
    loop, _source_registrar, job_repository, memory_store = _build_loop()

    try:
        loop.run_text(title="Empty", content="")
    except ValueError as error:
        assert "content" in str(error)
    else:
        raise AssertionError("empty text should be rejected")

    assert job_repository.all() == ()
    assert memory_store.list_by_source("source-text-empty") == ()
