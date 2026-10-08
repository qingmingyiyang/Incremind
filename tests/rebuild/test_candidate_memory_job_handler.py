from pathlib import Path

import pytest

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    CandidateMemoryJobHandler,
    CandidateMemoryJobInput,
    CreateMemoryCandidateFromSourceOutput,
    ReadSourceTextContent,
    build_candidate_memory_job,
)
from core.storage_provider import JsonObjectStore


def _fixture(tmp_path: Path):
    objects = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    source = ObjectStoreSourceRegistrar(objects).register(SourceSubmission(
        kind="text", title="Candidate child", content="Create one pending candidate and never publish formal memory."
    ))
    read = ReadSourceTextContent(objects).execute(source_id=str(source["id"]))
    evidence_id = str(read.read_ref).removesuffix(".json").rsplit("/", 1)[-1]
    job = build_candidate_memory_job(CandidateMemoryJobInput(
        parent_job_id="job-parent", source_id=str(source["id"]), project_id="default",
        evidence_kind="source_content_read", evidence_id=evidence_id,
    ), now="2026-07-11T12:00:00Z")
    return objects, source, evidence_id, job


def test_legacy_candidate_job_handler_is_retired_without_domain_write(tmp_path: Path):
    objects, _source, _evidence_id, job = _fixture(tmp_path)
    handler = CandidateMemoryJobHandler(CreateMemoryCandidateFromSourceOutput(objects))

    with pytest.raises(RuntimeError, match="Core effect-v2 handler"):
        handler.run_step("create_candidate", job)
    assert objects.list("memory_candidates") == ()
