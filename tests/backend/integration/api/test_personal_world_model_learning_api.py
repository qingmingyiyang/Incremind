from __future__ import annotations

from pathlib import Path

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.memory_core import ObjectStoreMemoryCandidateRepository

# The API fixture remains owned by the World Model route integration module.
# Importing it keeps this test on the same real receipt-bound setup.
from tests.backend.integration.api.test_personal_world_model_api import (
    PROJECT,
    TURN,
    _TurnAuthority,
    _client,
    _feedback,
    _seed_plan,
)


def _proposal(content: str = "Reuse the verified artifact revision check") -> dict[str, object]:
    return {
        "turn_id": TURN,
        "memory_proposal": {
            "proposed_content": content,
            "reason": "The user explicitly accepted this verified method",
        },
        "skill_proposal": None,
    }


def test_learning_api_creates_existing_pending_candidate_and_replays(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, _TurnAuthority()) as client:
        _seed_plan(client)
        feedback = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback",
            json=_feedback(),
        )
        assert feedback.status_code == 201

        created = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback/feedback-api/learning-proposals",
            json=_proposal(),
        )
        replayed = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback/feedback-api/learning-proposals",
            json=_proposal(),
        )
        drifted = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback/feedback-api/learning-proposals",
            json=_proposal("Use a different method for the same feedback"),
        )

    store, _settings = build_rebuild_object_store(tmp_path)
    candidate = ObjectStoreMemoryCandidateRepository(store).get(
        "world-feedback-feedback-api-event"
    )
    assert created.status_code == 201
    assert created.json()["memory"]["status"] == "pending_review"
    assert created.json()["authority_effects"] == {
        "memory_publication": "not_performed",
        "skill_file_write": "not_performed",
    }
    assert replayed.status_code == 200 and replayed.json()["replayed"] is True
    assert drifted.status_code == 409
    assert candidate is not None and candidate["status"] == "pending_review"


def test_learning_api_rejects_missing_proposal_and_cross_project_scope(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, _TurnAuthority()) as client:
        _seed_plan(client)
        assert client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback",
            json=_feedback(),
        ).status_code == 201

        missing = client.post(
            f"/api/rebuild/projects/{PROJECT}/world-model/feedback/feedback-api/learning-proposals",
            json={
                "turn_id": TURN,
                "memory_proposal": None,
                "skill_proposal": None,
            },
        )
        crossed = client.post(
            "/api/rebuild/projects/project-other/world-model/feedback/feedback-api/learning-proposals",
            json=_proposal(),
        )

    assert missing.status_code == 400
    assert crossed.status_code == 409
