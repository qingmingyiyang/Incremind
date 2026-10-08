from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes.product.repositories import _object_store


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _prompt(prompt_id: str, content: str, version: int = 1) -> dict[str, object]:
    return {
        "id": prompt_id,
        "stageId": prompt_id.removeprefix("pt-"),
        "name": prompt_id,
        "description": f"{prompt_id} description",
        "content": content,
        "variables": [],
        "outputSchema": "{}",
        "modelProfileId": "mp-test",
        "version": version,
        "isProtected": False,
        "updatedAt": f"2026-07-17T00:00:0{version}+08:00",
    }


def _prompts(prefix: str, version: int = 1) -> list[dict[str, object]]:
    return [
        _prompt("pt-input-understanding", f"{prefix} intake", version),
        _prompt("pt-title", f"{prefix} title", version),
        _prompt("pt-detail-summary", f"{prefix} detail", version),
        _prompt("pt-longterm-organize", f"{prefix} organize", version),
        _prompt("pt-output-validate", f"{prefix} validate", version),
    ]


def _save(client: TestClient, revision: int, prompts: list[dict[str, object]]):
    return client.put(
        "/api/rebuild/developer-studio/config",
        json={
            "expected_revision": revision,
            "model_profiles": [],
            "prompts": prompts,
            "skills": [],
            "workflow_steps": [],
            "snapshots": [],
        },
    )


def test_prompt_activation_endpoint_draft_activate_restart_and_rollback(tmp_path) -> None:
    first_client = _client(tmp_path)
    first = _save(first_client, 0, _prompts("baseline", 1))
    assert first.status_code == 200
    assert first.json()["prompt_activation_status"]["activation_revision"] == 0
    assert all(unit["active_prompt_ids"] == [] for unit in first.json()["prompt_activation_status"]["units"])

    draft = _save(first_client, 1, _prompts("draft", 2))
    assert draft.status_code == 200
    status = draft.json()["prompt_activation_status"]
    assert status["config_revision"] == 2
    units = {unit["unit_id"]: unit for unit in status["units"]}
    assert units["intake.classification"]["dirty"] is True

    preview = first_client.post(
        "/api/rebuild/developer-studio/prompt-activation/preview",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 2,
            "expected_activation_revision": 0,
        },
    )
    assert preview.status_code == 200
    assert preview.json()["status"] == "validated"
    assert preview.json()["consumer_manifest"] == [
        "workbench.input-classifier",
        "workbench.auto-intake",
        "model-route:intake.classification",
    ]

    activated = first_client.post(
        "/api/rebuild/developer-studio/prompt-activation/activate",
        json={
            "unit_id": "intake.classification",
            "expected_config_revision": 2,
            "expected_activation_revision": 0,
            "preview_token": preview.json()["preview_token"],
            "confirm": True,
            "reason": "endpoint validation",
        },
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "activated"
    assert activated.json()["prompt_activation_status"]["config_revision"] == 3
    activated_units = {
        unit["unit_id"]: unit
        for unit in activated.json()["prompt_activation_status"]["units"]
    }
    assert activated_units["intake.classification"]["dirty"] is False
    assert activated_units["intake.classification"]["can_rollback"] is True

    with _client(tmp_path) as restarted:
        after_restart = restarted.get("/api/rebuild/developer-studio/prompt-activation")
        assert after_restart.status_code == 200
        assert after_restart.json()["activation_revision"] == 1
        restarted_units = {
            unit["unit_id"]: unit
            for unit in after_restart.json()["units"]
        }
        assert restarted_units["intake.classification"]["active_prompt_versions"] == {
            "pt-input-understanding": 2,
        }
        rollback = restarted.post(
            "/api/rebuild/developer-studio/prompt-activation/rollback",
            json={
                "unit_id": "intake.classification",
                "expected_config_revision": 3,
                "expected_activation_revision": 1,
                "confirm": True,
                "reason": "restore product fallback",
            },
        )
        assert rollback.status_code == 200
        assert rollback.json()["status"] == "rolled_back"
        rollback_units = {
            unit["unit_id"]: unit
            for unit in rollback.json()["prompt_activation_status"]["units"]
        }
        assert rollback_units["intake.classification"]["active_prompt_ids"] == []
        assert rollback_units["intake.classification"]["can_rollback"] is False
        assert restarted.get("/api/rebuild/developer-studio/config").json()["prompts"][0]["content"] == "draft intake"


def test_prompt_activation_endpoint_rejects_stale_preview_and_missing_confirmation(tmp_path) -> None:
    client = _client(tmp_path)
    assert _save(client, 0, _prompts("draft", 1)).status_code == 200
    preview = client.post(
        "/api/rebuild/developer-studio/prompt-activation/preview",
        json={
            "unit_id": "source.template-document",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
        },
    )
    assert preview.status_code == 200

    missing_confirmation = client.post(
        "/api/rebuild/developer-studio/prompt-activation/activate",
        json={
            "unit_id": "source.template-document",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
            "preview_token": preview.json()["preview_token"],
            "confirm": False,
            "reason": "not confirmed",
        },
    )
    assert missing_confirmation.status_code == 400
    assert "explicit confirmation" in missing_confirmation.json()["detail"]

    assert _save(client, 1, _prompts("changed again", 2)).status_code == 200
    stale = client.post(
        "/api/rebuild/developer-studio/prompt-activation/activate",
        json={
            "unit_id": "source.template-document",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
            "preview_token": preview.json()["preview_token"],
            "confirm": True,
            "reason": "stale preview",
        },
    )
    assert stale.status_code == 409
    assert "config revision conflict" in stale.json()["detail"]


def test_prompt_activation_status_fails_closed_for_incomplete_template_unit(tmp_path) -> None:
    client = _client(tmp_path)
    incomplete = _prompts("draft", 1)[:-1]
    assert _save(client, 0, incomplete).status_code == 200

    preview = client.post(
        "/api/rebuild/developer-studio/prompt-activation/preview",
        json={
            "unit_id": "source.template-document",
            "expected_config_revision": 1,
            "expected_activation_revision": 0,
        },
    )

    assert preview.status_code == 400
    assert "pt-output-validate" in preview.json()["detail"]


def test_developer_config_read_and_write_fail_closed_when_activation_authority_drifted(tmp_path) -> None:
    client = _client(tmp_path)
    assert _save(client, 0, _prompts("draft", 1)).status_code == 200
    store, _settings = _object_store(tmp_path)
    record = dict(store.read("developer_studio_configs", "default"))
    projection = dict(record["prompt_activation"])
    projection["schema_version"] = "tampered"
    record["prompt_activation"] = projection
    store.write("developer_studio_configs", "default", record, expected_revision=None)

    read = client.get("/api/rebuild/developer-studio/config")
    write = _save(client, 1, _prompts("new draft", 2))

    assert read.status_code == 409
    assert read.json()["detail"] == "prompt activation authority drifted"
    assert write.status_code == 409
    assert write.json()["detail"] == "prompt activation authority drifted"
