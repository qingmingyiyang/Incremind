from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.memory_core import (
    SQLiteMemoryReader,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.product_core.project_memory_recall import CreateProjectMemoryRecall
from core.project_skill_core import ProjectSkillUpdate, SQLiteProjectSkillRepository
from core.search_and_recall import ObjectStoreRecallRepository
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


ROOT = Path(__file__).resolve().parents[2]
MEMORY_PUBLICATION_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)


def _activate_sqlite_memory_authority(root: Path) -> SQLiteStructuredRecordStore:
    evidence = AggregateAuthorityEvidence(
        migration_id="asset-sqlite-roundtrip-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    records = SQLiteStructuredRecordStore(
        root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    authority = SQLiteAggregateAuthorityStore(
        root / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    for member in MEMORY_PUBLICATION_MEMBERS:
        with records.begin() as transaction:
            transaction.put(
                "aggregate_authority_targets",
                f"default~{member}",
                {
                    "namespace_id": "default",
                    "aggregate": member,
                    "target_identity": TARGET_IDENTITY,
                    "source_fingerprint": evidence.source_fingerprint,
                    "target_fingerprint": evidence.target_fingerprint,
                    "migration_id": evidence.migration_id,
                },
                expected_revision=0,
            )
            transaction.commit()
        initial = authority.create_json_active(
            namespace_id="default",
            aggregate=member,
            reason="fresh test authority",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="fresh test staged",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="fresh test active",
        )
    activation = shared_trust_audit_activation_payload(
        namespace_id="default",
        target_identity=TARGET_IDENTITY,
        activation_id="asset-sqlite-roundtrip-compound-v1",
        member_migrations={
            member: evidence.migration_id for member in MEMORY_PUBLICATION_MEMBERS
        },
        source_fingerprint=evidence.source_fingerprint,
        target_fingerprint=evidence.target_fingerprint,
        activated_at="2026-07-27T00:00:00+00:00",
    )
    with records.begin() as transaction:
        transaction.put(
            "aggregate_authority_compound_activations",
            shared_trust_audit_activation_id("default"),
            activation,
            expected_revision=0,
        )
        transaction.commit()
    structured = json.loads(
        (
            ROOT
            / "core-contracts"
            / "rebuild"
            / "fixtures"
            / "project_skill"
            / "valid-active-skill.json"
        ).read_text(encoding="utf-8")
    )
    SQLiteProjectSkillRepository(records).save(
        ProjectSkillUpdate(
            project_id="project-alpha",
            markdown="# Project Alpha",
            structured=structured,
            expected_revision=0,
            reason="fresh SQLite roundtrip fixture",
        )
    )
    return records


def _hierarchy_package() -> bytes:
    rows = {
        "memories/l1_atomic_facts.ndjson": [{
            "memory_id": "atom-sqlite-portable",
            "layer": "L1",
            "type": "fact",
            "content": "SQLite authority Atom",
            "project_id": "project-alpha",
            "source_ref": "source-sqlite-portable",
        }],
        "memories/l2_scenarios.ndjson": [{
            "memory_id": "scenario-sqlite-portable",
            "layer": "L2",
            "type": "scenario",
            "content": "SQLite authority Scenario",
            "project_id": "project-alpha",
            "series_id": "project-alpha",
            "atom_ids": ["atom-sqlite-portable"],
            "source_ref": "source-sqlite-portable",
        }],
        "memories/l3_persona_series_project_skill.ndjson": [{
            "memory_id": "series-sqlite-portable",
            "layer": "L3",
            "type": "series_memory",
            "content": "SQLite authority Series Memory",
            "project_id": "project-alpha",
            "series_id": "series-alpha",
            "scenario_ids": ["scenario-sqlite-portable"],
            "source_ref": "source-sqlite-portable",
        }],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": "memory_asset_package",
            "version": "1.2",
            "memory_count": 3,
            "source_asset_count": 0,
        }))
        for path, items in rows.items():
            archive.writestr(
                path,
                "\n".join(json.dumps(item) for item in items),
            )
        archive.writestr("memories/l4_persona.ndjson", "")
        archive.writestr("sources/source_assets.ndjson", "")
        archive.writestr("sources/source_manifest.ndjson", json.dumps({
            "source_id": "source-sqlite-portable",
            "source_type": "text",
            "title": "SQLite authority source",
            "content_ref": "crp://default/sources/source-sqlite-portable",
            "media_type": "text/plain",
            "created_at": "2026-07-27T00:00:00+00:00",
            "is_audio_visual": False,
        }))
    return buffer.getvalue()


def _review(client: TestClient, candidate_id: str):
    return client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": candidate_id,
        "action": "confirm",
        "comment": "用户确认进入 SQLite staging。",
    })


def _publish(client: TestClient, layer_path: str, object_id: str):
    return client.post(
        f"/api/rebuild/{layer_path}/{object_id}/publication",
        json={"confirm": True, "reason": "用户二次确认正式发布。"},
    )


def test_asset_package_roundtrip_uses_sqlite_authority_and_recall_after_restart(
    tmp_path: Path,
) -> None:
    records = _activate_sqlite_memory_authority(tmp_path)
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_hierarchy_package()).decode(),
    })
    assert imported.status_code == 200
    assert imported.json()["rebuilt_count"] == 3

    scenario_before_atom = _review(client, "scenario-sqlite-portable")
    assert scenario_before_atom.status_code == 409
    assert "Atom binding is not published" in scenario_before_atom.json()["reason"]

    atom_review = _review(client, "atom-sqlite-portable")
    assert atom_review.status_code == 200
    assert records.read("staging_atoms", "atom-sqlite-portable") is not None
    atom_publish = _publish(client, "staging-atoms", "atom-sqlite-portable")
    assert atom_publish.status_code == 200

    scenario_review = _review(client, "scenario-sqlite-portable")
    assert scenario_review.status_code == 200
    series_before_scenario = _review(client, "series-sqlite-portable")
    assert series_before_scenario.status_code == 409
    assert "Scenario binding is not published" in series_before_scenario.json()["reason"]
    scenario_publish = _publish(
        client,
        "staging-scenarios",
        "scenario-sqlite-portable",
    )
    assert scenario_publish.status_code == 200

    series_review = _review(client, "series-sqlite-portable")
    assert series_review.status_code == 200
    series_publish = _publish(
        client,
        "staging-series-memory",
        "series-sqlite-portable",
    )
    assert series_publish.status_code == 200

    repeated_review = _review(client, "series-sqlite-portable")
    assert repeated_review.status_code == 200
    assert repeated_review.json()["promoted_object_id"] == "series-sqlite-portable"
    repeated_publish = _publish(
        client,
        "staging-series-memory",
        "series-sqlite-portable",
    )
    assert repeated_publish.status_code == 200
    assert repeated_publish.json()["status"] == "published"
    assert len(records.list("memory_publications")) == 3

    reopened_records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    memory = SQLiteMemoryReader(reopened_records)
    recalled_objects = memory.list_by_project("project-alpha")
    assert {item["id"] for item in recalled_objects} == {
        "atom-sqlite-portable",
        "scenario-sqlite-portable",
        "series-sqlite-portable",
    }
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    recall = CreateProjectMemoryRecall(
        skills=SQLiteProjectSkillRepository(reopened_records),
        memory=memory,
        recalls=ObjectStoreRecallRepository(store, namespace_id="default"),
    ).execute(
        "project-alpha",
        query="SQLite authority",
        created_at="2026-07-27T01:00:00+00:00",
    )
    assert recall.hit_count >= 4
    assert {
        hit.get("object_id")
        for hit in recall.evidence_hits
    }.issuperset({
        "atom-sqlite-portable",
        "scenario-sqlite-portable",
        "series-sqlite-portable",
    })

    restarted_client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    brain = restarted_client.get(
        "/api/rebuild/project-brain?scope=project&project_id=project-alpha"
    )
    assert brain.status_code == 200
    assert "atom-sqlite-portable" in brain.text
    assert "scenario-sqlite-portable" in brain.text
    assert "series-sqlite-portable" in brain.text


def test_asset_package_review_fails_closed_for_partial_sqlite_authority(
    tmp_path: Path,
) -> None:
    evidence = AggregateAuthorityEvidence(
        migration_id="partial-asset-authority-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    )
    with records.begin() as transaction:
        transaction.put(
            "aggregate_authority_targets",
            "default~memory_atoms",
            {
                "namespace_id": "default",
                "aggregate": "memory_atoms",
                "target_identity": TARGET_IDENTITY,
                "source_fingerprint": evidence.source_fingerprint,
                "target_fingerprint": evidence.target_fingerprint,
                "migration_id": evidence.migration_id,
            },
            expected_revision=0,
        )
        transaction.commit()
    authority = SQLiteAggregateAuthorityStore(
        tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    initial = authority.create_json_active(
        namespace_id="default",
        aggregate="memory_atoms",
        reason="partial test",
    )
    staged = authority.transition(
        namespace_id="default",
        aggregate="memory_atoms",
        expected_revision=initial.revision,
        to_state="sqlite_staged",
        evidence=evidence,
        reason="partial staged",
    )
    authority.transition(
        namespace_id="default",
        aggregate="memory_atoms",
        expected_revision=staged.revision,
        to_state="sqlite_active",
        evidence=evidence,
        reason="partial active",
    )
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_hierarchy_package()).decode(),
    })
    assert imported.status_code == 200

    reviewed = _review(client, "atom-sqlite-portable")

    assert reviewed.status_code == 409
    assert "partially SQLite active" in reviewed.json()["reason"]
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    assert store.read("memory_candidates", "atom-sqlite-portable")["status"] == "pending_review"
    assert records.read("staging_atoms", "atom-sqlite-portable") is None
