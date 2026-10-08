from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace

from backend.api.app import create_app
from backend.api.bootstrap import build_api_container
from fastapi.testclient import TestClient

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
)
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.storage_provider import (
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
)
from tests.backend.integration.api.test_rebuild_project_skill_direct_question_e2e import (
    _candidate,
)


MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)

ROOT = Path(__file__).resolve().parents[4]


def test_sidecar_startup_bootstraps_empty_vault_and_enables_manual_project_skill_publication(
    tmp_path: Path,
) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        report = app.state.fresh_vault_shared_trust_audit_bootstrap
        store = JsonObjectStore(
            tmp_path / ".rebuild-data",
            legacy_root=tmp_path / "library",
        )
        ObjectStoreMemoryCandidateRepository(store).save(_candidate())
        reviewed = client.post(
            "/api/rebuild/memory-candidates/candidate-default-project-skill-update/review",
            json={
                "action": "promote_to_project_skill",
                "reason": "用户确认fresh Vault项目Skill候选。",
            },
        )
        draft_id = reviewed.json().get("promoted_object_id")
        published = client.post(
            f"/api/rebuild/staging-project-skills/{draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认fresh Vault项目Skill。"},
        )
        rollback = client.post(
            f"/api/rebuild/memory-publications/{published.json().get('publication_id')}/rollback",
            json={
                "confirm": True,
                "reason": "用户撤回fresh Vault项目Skill。",
                "expected_publication_revision": published.json().get("publication_revision"),
                "expected_project_skill_revision": published.json().get("project_skill_revision"),
            },
        )

    assert report.outcome == "activated"
    assert reviewed.status_code == 200, reviewed.text
    assert published.status_code == 200, published.text
    assert published.json()["project_skill_revision"] == 1
    assert published.json()["publication_revision"] == 1
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["publication_revision"] == 2
    assert rollback.json()["project_skill_revision"] == 2
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    assert records.read("project_skills", "skill-default").payload["revision"] == 2
    assert records.read("project_skills", "skill-default").payload["status"] == "rolled_back"


def test_repeat_sidecar_startup_keeps_finalized_bootstrap_revisions_stable(
    tmp_path: Path,
) -> None:
    first_app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(first_app):
        first_report = first_app.state.fresh_vault_shared_trust_audit_bootstrap
    rebuild_root = tmp_path / ".rebuild-data"
    records = SQLiteStructuredRecordStore(rebuild_root / STRUCTURED_DATABASE_NAME)
    authority = SQLiteAggregateAuthorityStore(rebuild_root / AUTHORITY_DATABASE_NAME)
    first_revisions = {
        member: authority.get("default", member).revision for member in MEMBERS
    }
    operation_records = records.list("shared_trust_audit_activation_operations")

    second_app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(second_app):
        second_report = second_app.state.fresh_vault_shared_trust_audit_bootstrap

    assert first_report.outcome == "activated"
    assert second_report.outcome == "already_active"
    assert {
        member: authority.get("default", member).revision for member in MEMBERS
    } == first_revisions
    assert records.list("shared_trust_audit_activation_operations") == operation_records
    assert SQLiteSharedTrustAuditActivationSagaStore(records).list_recoverable() == ()


def test_formal_vault_startup_enables_companion_candidate_review_publication_and_forget(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    copyfile(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    app = create_app(build_api_container(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/api/rebuild/companion/chat",
            json={"request_id": "fresh-formal-companion", "text": "周末阅读纸质书。"},
        )
        message_id = created.json()["user_message"]["message_id"]
        proposed = client.post(
            f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={},
        )
        candidate_id = proposed.json()["candidate"]["candidate_id"]
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "用户确认该偏好值得长期记忆。"},
        )
        atom_id = reviewed.json().get("promoted_object_id")
        published = client.post(
            f"/api/rebuild/staging-atoms/{atom_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布该长期记忆。"},
        )
        publication_record = next(
            record.payload
            for record in SQLiteStructuredRecordStore(
                tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
            ).list("memory_publications")
            if record.payload.get("published_object_id") == atom_id
        )
        pulse = client.get("/api/rebuild/pet/mood")
        library = client.get("/api/rebuild/library/overview")
        reviewed_today = client.post(
            "/api/rebuild/companion/chat",
            json={
                "request_id": "fresh-formal-today-review",
                "text": "请根据我已经确认发布的长期记忆，回顾今天值得注意的变化。请区分有证据的事实与暂无依据的推断。",
            },
        )
        deleted = client.delete(f"/api/rebuild/companion/messages/{message_id}")

    assert app.state.fresh_vault_shared_trust_audit_bootstrap.outcome == "activated"
    authority = SQLiteAggregateAuthorityStore(
        tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    assert all(authority.get("default", member).state == "sqlite_active" for member in MEMBERS)
    assert created.status_code == 201
    assert proposed.status_code == 201
    assert reviewed.status_code == 200 and atom_id
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    published_at = datetime.fromisoformat(str(publication_record["published_at"]))
    assert published_at.tzinfo is not None
    assert published_at.astimezone().date() == datetime.now(timezone.utc).astimezone().date()
    assert pulse.status_code == 200, pulse.text
    assert pulse.json()["published_memory_count"] == 1
    assert pulse.json()["today_memory_count"] == 1
    assert reviewed_today.status_code == 201
    review_trace = reviewed_today.json()["trace"]["memory_recall"]
    assert review_trace["status"] == "recalled"
    assert review_trace["backend"] == "publication_authority"
    assert review_trace["review_scope"] == "today_memory_review"
    assert [item["memory_id"] for item in review_trace["selected"]] == [atom_id]
    assert "周末阅读纸质书" not in str(review_trace)
    assert pulse.json()["pending_memory_candidate_count"] == 0
    assert library.status_code == 200, library.text
    assert library.json()["counts"]["atom"] == 1
    assert deleted.status_code == 200
    assert deleted.json()["receipt"]["affected"] == {
        "candidate": 1, "message": 1, "published_memory": 1,
    }


def test_formal_vault_published_companion_memory_rebuilds_fts_and_cannot_resurface_after_forget(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    copyfile(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    app = create_app(build_api_container(tmp_path))
    canary = "霜鲸七号只喝武夷岩茶"

    with TestClient(app) as client:
        created = client.post(
            "/api/rebuild/companion/chat",
            json={"request_id": "fresh-formal-recall-source", "text": canary},
        )
        message_id = created.json()["user_message"]["message_id"]
        proposed = client.post(
            f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={},
        )
        candidate_id = proposed.json()["candidate"]["candidate_id"]
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "用户确认该偏好值得长期记忆。"},
        )
        atom_id = reviewed.json()["promoted_object_id"]
        published = client.post(
            f"/api/rebuild/staging-atoms/{atom_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布该长期记忆。"},
        )
        freshness = client.get("/api/rebuild/index/freshness")
        rebuilt = client.post("/api/rebuild/index/rebuild", json={})
        recalled = client.post(
            "/api/rebuild/companion/chat",
            json={"request_id": "fresh-formal-recall-query", "text": "霜鲸七号喝什么？"},
        )
        deleted = client.delete(f"/api/rebuild/companion/messages/{message_id}")
        after_forget = client.get(
            "/api/rebuild/library/search",
            params={"q": "霜鲸七号", "project_id": "default", "layers": "l1_atom"},
        )
        recalled_after_forget = client.post(
            "/api/rebuild/companion/chat",
            json={"request_id": "fresh-formal-recall-after-forget", "text": "霜鲸七号喝什么？"},
        )

    assert published.status_code == 200
    assert freshness.status_code == 200
    assert freshness.json()["status"] in {"missing", "stale"}
    assert freshness.json()["can_rebuild"] is True
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["status"] == "fresh"
    trace = recalled.json()["trace"]["memory_recall"]
    assert trace["status"] == "recalled"
    assert trace["backend"] == "sqlite_fts5"
    assert [item["memory_id"] for item in trace["selected"]] == [atom_id]
    assert canary not in str(trace)
    assert deleted.status_code == 200
    assert after_forget.status_code == 200
    assert after_forget.json()["index_stale"] is True
    assert after_forget.json()["hits"] == []
    after_trace = recalled_after_forget.json()["trace"]["memory_recall"]
    assert after_trace["selected"] == []
    assert canary not in str(after_trace)


def test_formal_vault_memory_export_reads_compound_sqlite_publication_authority(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    copyfile(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    app = create_app(build_api_container(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/api/rebuild/companion/chat",
            json={"request_id": "fresh-formal-export", "text": "我偏好纸质书。"},
        )
        message_id = created.json()["user_message"]["message_id"]
        proposed = client.post(
            f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={},
        )
        candidate_id = proposed.json()["candidate"]["candidate_id"]
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={"action": "promote_to_atom", "reason": "用户确认该偏好值得长期记忆。"},
        )
        atom_id = reviewed.json()["promoted_object_id"]
        published = client.post(
            f"/api/rebuild/staging-atoms/{atom_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布该长期记忆。"},
        )
        preview = client.post("/api/rebuild/memory/export/preview", json={
            "preset": "full_asset_package",
            "scope": {"skip_low_trust": False},
        })
        exported = client.post("/api/rebuild/memory/export/file", json={
            "preset": "full_asset_package",
            "scope": {"skip_low_trust": False},
        })

    assert published.status_code == 200
    assert preview.status_code == 200, preview.text
    assert preview.json()["authority_identity"] == "sqlite:structured-records-v1"
    assert preview.json()["published_memory_count"] >= 1
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content), "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        atoms = archive.read("memories/l1_atomic_facts.ndjson").decode()
        assert manifest["memory_count"] >= 1
        assert atom_id in atoms
