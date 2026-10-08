from __future__ import annotations

import pytest

from core.product_core.external_persona_candidate_staging import (
    ExternalPersonaCandidateStagingError,
    StageExternalPersonaCandidate,
)
from core.product_core.persona import ObjectStorePersonaRepository
from core.storage_provider import JsonObjectStore


def _repository(tmp_path) -> ObjectStorePersonaRepository:
    return ObjectStorePersonaRepository(
        JsonObjectStore(
            tmp_path / ".rebuild-data",
            legacy_root=tmp_path / "library",
        )
    )


def _candidate(**overrides):
    return {
        "id": "memory-external-persona-1",
        "layer": "L4",
        "target_layer": "persona",
        "type": "persona",
        "content": "请保持简洁、直接的回答风格。",
        "confidence": 0.85,
        "source_role": "user",
        "source_id": "source-external-1",
        "source_refs": [
            {
                "source_id": "source-external-1",
                "locator": "generic://zip/custom_instructions/0",
            }
        ],
        "status": "pending_review",
        **overrides,
    }


def test_external_persona_candidate_creates_pending_l4_draft_only(tmp_path) -> None:
    repository = _repository(tmp_path)

    draft = StageExternalPersonaCandidate(
        repository,
        now="2026-07-27T10:00:00+08:00",
    ).execute(
        _candidate(),
        scope="project",
        expected_draft_revision=0,
        expected_current_revision=0,
    )

    assert repository.get("project") is None
    assert repository.get_draft("project") == draft
    assert draft["confirmation"]["status"] == "pending"
    assert draft["trust_status"] == "system_generated"
    assert draft["statements"] == [
        {
            "id": draft["statements"][0]["id"],
            "content": "请保持简洁、直接的回答风格。",
            "category": "style",
            "confidence": 0.85,
        }
    ]
    assert draft["evidence_refs"][0]["object_type"] == "source"
    assert draft["evidence_refs"][0]["object_id"] == "source-external-1"


@pytest.mark.parametrize(
    "candidate",
    [
        _candidate(layer="L2"),
        _candidate(type="fact"),
        _candidate(status="confirmed"),
        _candidate(source_id=""),
    ],
)
def test_external_persona_candidate_rejects_invalid_or_terminal_input(
    tmp_path,
    candidate,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ExternalPersonaCandidateStagingError):
        StageExternalPersonaCandidate(
            repository,
            now="2026-07-27T10:00:00+08:00",
        ).execute(
            candidate,
            scope="project",
            expected_draft_revision=0,
            expected_current_revision=0,
        )

    assert repository.get("project") is None
    assert repository.get_draft("project") is None


def test_external_persona_candidate_merges_distinct_evidence_without_duplicates(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    service = StageExternalPersonaCandidate(
        repository,
        now="2026-07-27T10:00:00+08:00",
    )
    first = service.execute(
        _candidate(),
        scope="global",
        expected_draft_revision=0,
        expected_current_revision=0,
    )
    second = service.execute(
        _candidate(
            id="memory-external-persona-2",
            content="不要在回答里隐藏证据来源。",
            source_id="source-external-2",
            source_refs=[
                {
                    "source_id": "source-external-2",
                    "locator": "generic://zip/custom_instructions/1",
                }
            ],
        ),
        scope="global",
        expected_draft_revision=1,
        expected_current_revision=0,
    )

    assert len(first["statements"]) == 1
    assert len(second["statements"]) == 2
    assert {item["category"] for item in second["statements"]} == {
        "style",
        "constraint",
    }
    assert {
        item["object_id"] for item in second["evidence_refs"]
    } == {"source-external-1", "source-external-2"}
    assert repository.get("global") is None


def test_legacy_l3_persona_candidate_is_staged_as_l4_without_auto_confirmation(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)

    draft = StageExternalPersonaCandidate(
        repository,
        now="2026-07-27T10:00:00+08:00",
    ).execute(
        _candidate(
            layer="L3",
            target_layer=None,
            source_id=None,
            source_refs=None,
            source_ref="crp://default/sources/source-external-legacy",
        ),
        scope="global",
        expected_draft_revision=0,
        expected_current_revision=0,
    )

    assert draft["confirmation"]["status"] == "pending"
    assert draft["evidence_refs"][0]["object_id"] == "source-external-legacy"
    assert repository.get("global") is None
