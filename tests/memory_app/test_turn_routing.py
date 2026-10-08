import json

import pytest
from types import SimpleNamespace

from backend.api.model_routing_snapshot_authority import (TurnModelRoutingSnapshotAuthority,
    TurnModelRoutingSnapshotAuthorityError)
from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.turn_dispatch import _request
from backend.memory_app.turn_routing import RecognitionModelRoutingSnapshotAuthority, SNAPSHOT_KIND
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


class Models:
    def __init__(self):
        self.configuration = dict(purpose="generation", provider="openai", base_url="https://example.test",
            model="test-model", allow_remote=True, revision=4, configured=True, has_api_key=True,
            api_key="must-not-be-copied", secret_ref="must-not-be-copied")

    def public(self):
        return {"generation": self.configuration}


def test_routing_is_durable_metadata_only_and_rejects_changed_configuration(tmp_path):
    models = Models()
    path = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(path)
    store.claim_turn(dict(turn_id="turn-one", session_id="session-one",
                          operation_id="operation-one", idempotency_key="key-one"))
    authority = RecognitionModelRoutingSnapshotAuthority(models, store)
    identity = dict(turn_id="turn-one", project_id="project-a", context_packet_id="packet-one",
        project_profile_id="profile-one", project_profile_revision=1,
        boundary_profile_id="boundary-one", boundary_profile_revision=1,
        capability_ids=("recognition.task.execute",), agent_binding=None, allow_remote=True)
    first = authority.acquire(**identity)
    second = RecognitionModelRoutingSnapshotAuthority(models, SQLiteAITurnStore(path)).acquire(**identity)
    assert first == second and first.payload_ref.startswith("crp://session/turn-one/")
    assert len(first.revision) == 64 and len(first.payload["catalog_revision"]) == 64
    raw = json.dumps(store.get_immutable_payload("turn-one", SNAPSHOT_KIND)[1])
    assert "must-not-be-copied" not in raw and '"api_key"' not in raw and '"secret_ref"' not in raw
    assert first.generation_binding()["configuration"]["revision"] == 4
    assert first.generation_binding()["execution_location"] == "remote"
    models.configuration["revision"] = 5
    with pytest.raises(ModelConfigurationError, match="routing_changed"):
        authority.acquire(**identity)
    assert store.get_immutable_payload("turn-one", SNAPSHOT_KIND)[1] == first.payload


def test_routing_rejects_remote_without_task_consent_before_persistence(tmp_path):
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    authority = RecognitionModelRoutingSnapshotAuthority(Models(), store)
    with pytest.raises(ModelConfigurationError, match="not_consented"):
        authority.acquire(turn_id="turn-one", project_id="project-a", context_packet_id="packet-one",
            project_profile_id="profile-one", project_profile_revision=1,
            boundary_profile_id="boundary-one", boundary_profile_revision=1,
            capability_ids=("recognition.task.execute",), agent_binding=None, allow_remote=False)
    assert store.get_immutable_payload("turn-one", SNAPSHOT_KIND) is None


def test_local_capability_routes_only_with_local_privacy_and_exact_id():
    class Routing:
        def acquire(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(payload={"execution_location": "local_loopback"})

    routing = Routing()
    authority = TurnModelRoutingSnapshotAuthority(object(), object(), recognition_routing=routing)
    request = _request(task_id="task-one", project_id="project-a", packet_id="packet-one", remote=False)
    kwargs = dict(project_id="project-a", project_profile_id="project-profile", project_profile_revision=1,
                  boundary_profile_id="boundary-profile", boundary_profile_revision=1,
                  capability_ids=("recognition.task.execute.local",), skill_snapshot_revision=None)
    authority.acquire(request, **kwargs)
    assert routing.kwargs["allow_remote"] is False
    assert routing.kwargs["expected_execution_location"] == "local_loopback"
    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="exact confirmed context"):
        authority.acquire(request, **{**kwargs, "capability_ids": ("recognition.task.execute",)})
    with pytest.raises(TurnModelRoutingSnapshotAuthorityError, match="exact confirmed context"):
        authority.acquire({**request, "privacy": {**request["privacy"], "allow_remote": True,
                                                 "mode": "remote_allowed"}}, **kwargs)


def test_local_route_cannot_be_frozen_for_remote_capability(tmp_path):
    models = Models()
    models.configuration["base_url"] = "http://127.0.0.1:8001/local-model/v1"
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    store.claim_turn(dict(turn_id="turn-one", session_id="session-one",
                          operation_id="operation-one", idempotency_key="key-one"))
    authority = RecognitionModelRoutingSnapshotAuthority(models, store)
    with pytest.raises(ModelConfigurationError, match="location_changed"):
        authority.acquire(turn_id="turn-one", project_id="project-a", context_packet_id="packet-one",
            project_profile_id="profile-one", project_profile_revision=1,
            boundary_profile_id="boundary-one", boundary_profile_revision=1,
            capability_ids=("recognition.task.execute",), agent_binding=None, allow_remote=True,
            expected_execution_location="remote")
    assert store.get_immutable_payload("turn-one", SNAPSHOT_KIND) is None
