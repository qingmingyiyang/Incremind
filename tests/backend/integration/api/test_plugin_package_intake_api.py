from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app


def _package(tmp_path):
    package = tmp_path / ".rebuild-data" / "plugin-package-inbox" / "selected-plugin"
    metadata = package / ".codex-plugin"
    metadata.mkdir(parents=True)
    (metadata / "plugin.json").write_text(
        json.dumps(
            {
                "name": "selected-plugin",
                "version": "1.0.0",
                "description": "Selected by the local user",
            }
        ),
        encoding="utf-8",
    )
    skill = package / "skills" / "selected-method"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: selected-method\ndescription: A reviewed method\n---\nUse the reviewed method.\n",
        encoding="utf-8",
    )
    return package


def test_api_discovers_installs_disabled_and_recovers_from_sqlite(tmp_path) -> None:
    package = _package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        discovered = client.post(
            "/api/ai/governance/plugins/packages/discover",
            json={"source_path": str(package), "command_id": "discover-api-0001"},
        )
        assert discovered.status_code == 201
        assert discovered.json()["state"]["enabled"] is False
        installed = client.post(
            "/api/ai/governance/plugins/packages/selected-plugin/install-disabled",
            json={
                "expected_state_revision": discovered.json()["state_revision"],
                "command_id": "install-api-0001",
                "confirm": True,
            },
        )
        assert installed.status_code == 200
        assert installed.json()["state"]["status"] == "installed_disabled"
        reviewed = client.post(
            "/api/ai/governance/plugins/packages/selected-plugin/skills/review",
            json={
                "skill_ids": ["selected-method"],
                "expected_state_revision": installed.json()["state_revision"],
                "command_id": "review-api-0001",
                "confirm": True,
                "reason": "Reviewed locally",
            },
        )
        assert reviewed.status_code == 200
        activated = client.post(
            "/api/ai/governance/plugins/packages/selected-plugin/skills/activate",
            json={
                "expected_review_revision": reviewed.json()["review_revision"],
                "expected_activation_revision": 0,
                "command_id": "activate-api-0001",
                "confirm": True,
            },
        )
        assert activated.status_code == 200
        assert activated.json()["activation"]["status"] == "active"
        skill_status = client.get("/api/rebuild/developer-studio/application-skills")
        plugin_package = next(
            item for item in skill_status.json()["catalog"]["packages"]
            if item["skill_id"] == "selected-method"
        )
        assert plugin_package["source_kind"] == "plugin"
        preview = client.post(
            "/api/rebuild/developer-studio/application-skills/bindings/preview",
            json={
                "skill_id": "selected-method",
                "project_id": "project-plugin-api",
                "allowed_consumers": ["turn.workbench-question"],
                "priority": 700,
                "trigger_terms": ["reviewed"],
            },
        )
        assert preview.status_code == 200
        bound = client.post(
            "/api/rebuild/developer-studio/application-skills/bindings/activate",
            json={
                "skill_id": "selected-method",
                "project_id": "project-plugin-api",
                "allowed_consumers": ["turn.workbench-question"],
                "priority": 700,
                "trigger_terms": ["reviewed"],
                "expected_registry_revision": preview.json()["registry_revision"],
                "preview_token": preview.json()["preview_token"],
                "proposal_id": preview.json()["proposal_id"],
                "confirm": True,
                "reason": "Bind reviewed Plugin Skill",
            },
        )
        assert bound.status_code == 200
        enabled = client.post(
            "/api/rebuild/developer-studio/application-skills/plugins/selected-plugin/projects/project-plugin-api/enable",
            json={"skill_ids": ["selected-method"], "expected_profile_revision": 0, "confirm": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["profile"]["enabled_sources"] == ["core", "plugin"]
        assert enabled.json()["profile"]["enabled_plugin_ids"] == ["selected-plugin"]
        assert enabled.json()["profile"]["enabled_skill_ids"] == ["selected-method"]
        invalid_enable = client.post(
            "/api/rebuild/developer-studio/application-skills/plugins/selected-plugin/projects/project-plugin-api/enable",
            json={"skill_ids": ["selected-method", 1], "expected_profile_revision": 1, "confirm": True},
        )
        assert invalid_enable.status_code == 400

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        snapshot = restarted.get("/api/ai/governance/plugins/packages")
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json()["packages"][0]["state"]["enabled"] is False


def test_api_rejects_coerced_or_extra_fields(tmp_path) -> None:
    package = _package(tmp_path)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        non_string = client.post(
            "/api/ai/governance/plugins/packages/discover",
            json={"source_path": str(package), "command_id": 12345678},
        )
        extra = client.post(
            "/api/ai/governance/plugins/packages/discover",
            json={"source_path": str(package), "command_id": "discover-api-0001", "enable": True},
        )

    assert non_string.status_code == 400
    assert non_string.json()["status"] == "invalid_request"
    assert extra.status_code == 400
    assert extra.json()["status"] == "invalid_request"
