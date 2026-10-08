from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import create_app
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope


class GuardedFakeModels:
    """Remote-shaped generation fake that exercises the callback boundary."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.before_response = None
        self.generation_base_url = "https://example.test/v1"

    def public(self):
        return {
            purpose: {
                "purpose": purpose,
                "provider": "openai",
                "base_url": self.generation_base_url if purpose == "generation" else "",
                "model": "test-model" if purpose == "generation" else "",
                "allow_remote": purpose == "generation",
                "revision": 1,
                "has_api_key": purpose == "generation",
                "configured": purpose == "generation",
            }
            for purpose in ("generation", "embedding", "rerank")
        }

    def complete(self, messages, *, max_tokens=1800, validate_current=None):
        if validate_current is not None:
            validate_current()
        self.calls.append(messages)
        if self.before_response is not None:
            callback, self.before_response = self.before_response, None
            callback()
        if validate_current is not None:
            validate_current()
        return "模型生成的可编辑认识", {"model": "test-model", "configuration_revision": 1, "usage": {}}


def _client(tmp_path):
    models = GuardedFakeModels()
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    return TestClient(app), models


def _experience(client, *, project_id="project-a", content="验证工作台先于桌面壳"):
    response = client.post("/api/recognition/experiences", json={"project_id": project_id, "content": content})
    assert response.status_code == 200, response.text
    return response.json()


def _policy(client, source_type, source, *, purposes, expected_policy_revision=0, project_id="project-a"):
    response = client.put(f"/api/recognition/source-policies/{source_type}/{source['id']}", json={
        "project_id": project_id,
        "expected_source_revision": source["revision"],
        "expected_policy_revision": expected_policy_revision,
        "allowed_purposes": purposes,
    })
    assert response.status_code == 200, response.text
    return response.json()




def test_policy_api_uses_source_version_cas_scope_and_body_free_snapshot(tmp_path):
    client, _models = _client(tmp_path)
    try:
        source = _experience(client)
        stored = _policy(client, "experience", source, purposes=["generation", "embedding", "rerank"])
        assert stored == {
            "source_type": "experience", "source_id": source["id"], "source_revision": source["revision"],
            "policy_revision": 1, "allowed_purposes": ["embedding", "generation", "rerank"],
        }

        snapshot = client.get(f"/api/recognition/source-policies/experience/{source['id']}", params={
            "project_id": "project-a", "revision": source["revision"],
        })
        assert snapshot.status_code == 200, snapshot.text
        payload = snapshot.json()
        assert payload["roots"] == [{"type": "experience", "id": source["id"], "revision": source["revision"]}]
        assert "content" not in str(payload)
        assert payload["nodes"] == [{
            "type": "experience", "id": source["id"], "source_revision": source["revision"],
            "policy_revision": 1, "effective_purposes": ["embedding", "generation", "rerank"],
        }]

        stale = client.put(f"/api/recognition/source-policies/experience/{source['id']}", json={
            "project_id": "project-a", "expected_source_revision": source["revision"],
            "expected_policy_revision": 0, "allowed_purposes": [],
        })
        assert stale.status_code == 409 and "policy revision conflicted" in stale.json()["detail"]
        stale_source = client.put(f"/api/recognition/source-policies/experience/{source['id']}", json={
            "project_id": "project-a", "expected_source_revision": source["revision"] + 1,
            "expected_policy_revision": 1, "allowed_purposes": [],
        })
        assert stale_source.status_code == 409 and "source revision conflicted" in stale_source.json()["detail"]
        foreign_write = client.put(f"/api/recognition/source-policies/experience/{source['id']}", json={
            "project_id": "project-b", "expected_source_revision": source["revision"],
            "expected_policy_revision": 1, "allowed_purposes": [],
        })
        assert foreign_write.status_code == 409
        foreign = client.get(f"/api/recognition/source-policies/experience/{source['id']}", params={
            "project_id": "project-b", "revision": source["revision"],
        })
        assert foreign.status_code == 409
    finally:
        client.close()
