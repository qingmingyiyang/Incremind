"""阶段 1.6 后端 API 路由测试 — 10 个端点 wiring。

覆盖：
- POST /api/rebuild/memory/import-preflight   文件预检
- POST /api/rebuild/memory/import-batch       批量导入
- POST /api/rebuild/memory/import-external    外部 LLM 导入
- POST /api/rebuild/memory/import-retry       失败项重试
- GET  /api/rebuild/memory/import-batches     导入批次列表
- POST /api/rebuild/memory/candidates/review  候选确认 / 忽略 / 编辑
- POST /api/rebuild/memory/candidates/conflict 冲突解决
- POST /api/rebuild/memory/export             导出
- POST /api/rebuild/memory/export/preview     导出预览（含脱敏）
- POST /api/rebuild/memory/export/round-trip  资产包回导校验
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from core.memory_core import (
    ObjectStoreMemoryStore,
    SQLiteMemoryReader,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)

# 把 src/ 加入 sys.path
import sys
import os
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backend.api.app import create_app  # noqa: E402
from backend.api.routes.product import (  # noqa: E402
    memory_import_records as product_memory_import_records,
    repositories as product_repositories,
)
from core.aggregate_repository_factory import (  # noqa: E402
    AggregateRepositoryFactory,
    AUTHORITY_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.storage_provider import (  # noqa: E402
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _install_import_batch_write_failure(tmp_path, monkeypatch, *, revision: int):
    original_object_store = product_repositories._object_store
    store, settings = original_object_store(tmp_path)

    class _FailBatchWrite:
        def __getattr__(self, name):
            return getattr(store, name)

        def write(self, collection, object_id, payload, expected_revision):
            if (
                collection == "memory_import_batches"
                and expected_revision == revision
            ):
                raise OSError(f"injected batch revision {revision} write failure")
            return store.write(collection, object_id, payload, expected_revision)

    monkeypatch.setattr(
        product_repositories,
        "_object_store",
        lambda _runtime_root: (_FailBatchWrite(), settings),
    )
    return store


def _activate_project_skill_sqlite_authority(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    authority = SQLiteAggregateAuthorityStore(
        tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    members = (
        "memory_atoms",
        "memory_publications",
        "memory_scenarios",
        "memory_series_memory",
        "memory_transitions",
        "project_skills",
    )
    evidence = AggregateAuthorityEvidence(
        "project-skill-roundtrip-v1",
        "a" * 64,
        "b" * 64,
        TARGET_IDENTITY,
    )
    with records.begin() as transaction:
        for member in members:
            transaction.put(
                "aggregate_authority_targets",
                f"default~{member}",
                {
                    "namespace_id": "default",
                    "aggregate": member,
                    "migration_id": evidence.migration_id,
                    "source_fingerprint": evidence.source_fingerprint,
                    "target_fingerprint": evidence.target_fingerprint,
                    "target_identity": evidence.target_identity,
                },
                expected_revision=0,
            )
        transaction.put(
            "aggregate_authority_compound_activations",
            shared_trust_audit_activation_id("default"),
            shared_trust_audit_activation_payload(
                namespace_id="default",
                target_identity=TARGET_IDENTITY,
                activation_id=evidence.migration_id,
                member_migrations={
                    member: evidence.migration_id for member in members
                },
                source_fingerprint=evidence.source_fingerprint,
                target_fingerprint=evidence.target_fingerprint,
                activated_at="2026-07-27T00:00:00+00:00",
            ),
            expected_revision=0,
        )
        transaction.commit()
    for member in members:
        initial = authority.create_json_active(
            namespace_id="default",
            aggregate=member,
            reason="roundtrip test initial",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="roundtrip test staged",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="roundtrip test active",
        )


# ── 1. import-preflight ──


def test_import_preflight_returns_categories_for_mixed_files(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-preflight", json={
        "files": [
            {"name": "notes.md", "size": 1024, "type": "text/markdown"},
            {"name": "photo.png", "size": 2048576, "type": "image/png"},
            {"name": "clip.mp3", "size": 5_000_000, "type": "audio/mpeg"},
            {"name": "doc.pdf", "size": 500_000, "type": "application/pdf"},
            {"name": "unknown.xyz", "size": 100, "type": "application/octet-stream"},
        ],
    })
    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]
    assert len(items) == 5
    by_name = {i["name"]: i for i in items}
    assert by_name["notes.md"]["category"] == "text"
    assert by_name["notes.md"]["parseable"] is True
    assert by_name["photo.png"]["category"] == "image"
    assert by_name["photo.png"]["needs_ocr"] is True
    assert by_name["photo.png"]["needs_external_provider"] is True
    assert by_name["clip.mp3"]["category"] == "audio"
    assert by_name["clip.mp3"]["needs_asr"] is True
    assert by_name["doc.pdf"]["needs_external_provider"] is True
    assert by_name["unknown.xyz"]["category"] == "other"
    assert by_name["unknown.xyz"]["parseable"] is False
    summary = payload["summary"]
    assert summary["total"] == 5
    assert summary["needs_external_provider"] == 3  # png + mp3 + pdf
    # mp3 is 5MB < 100MB threshold, so too_large should be 0
    assert summary["too_large"] == 0


def test_import_preflight_rejects_empty_files(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-preflight", json={"files": []})
    assert response.status_code == 400
    payload = response.json()
    assert "non-empty array" in payload["reason"]


def test_import_preflight_rejects_missing_body(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-preflight", json=None)
    assert response.status_code == 400


def test_import_preflight_detects_same_bytes_not_same_filename(tmp_path) -> None:
    client = _client(tmp_path)
    original = b"content-addressed duplicate"
    digest = hashlib.sha256(original).hexdigest()
    first = client.post("/api/rebuild/memory/import-batch", json={
        "files": [{
            "name": "original.txt",
            "size": len(original),
            "type": "text/plain",
            "content_base64": base64.b64encode(original).decode(),
        }],
    })
    assert first.status_code == 201

    response = client.post("/api/rebuild/memory/import-preflight", json={
        "files": [
            {
                "name": "renamed.txt",
                "size": len(original),
                "type": "text/plain",
                "sha256": digest,
            },
            {
                "name": "original.txt",
                "size": 9,
                "type": "text/plain",
                "sha256": hashlib.sha256(b"different").hexdigest(),
            },
        ],
    })

    assert response.status_code == 200
    duplicates = response.json()["duplicates"]
    assert len(duplicates) == 1
    assert duplicates[0]["name"] == "renamed.txt"
    assert duplicates[0]["sha256"] == digest
    assert duplicates[0]["existing_title"] == "original"


# ── 2. import-batch ──


def test_import_batch_succeeds_for_text_file(tmp_path) -> None:
    client = _client(tmp_path)
    raw_content = "我喜欢简洁的界面设计。项目目标是构建个人记忆工作台。".encode("utf-8")
    content_b64 = base64.b64encode(raw_content).decode()
    response = client.post("/api/rebuild/memory/import-batch", json={
        "files": [
            {
                "name": "notes.md",
                "size": len(raw_content),
                "type": "text/markdown",
                "content_base64": content_b64,
            },
        ],
    })
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["succeeded"] == 1
    assert payload["failed"] == 0
    assert payload["candidate_count"] == 1
    assert payload["batch_id"].startswith("batch-")
    assert payload["occurred_at"] == payload["recorded_at"]

    # 验证候选已写入 ObjectStore
    store = _store(tmp_path)
    candidates = list(store.list("memory_candidates"))
    assert len(candidates) == 1
    assert candidates[0]["layer"] == "L1"
    assert candidates[0]["status"] == "candidate"
    assert candidates[0]["source_ref"].startswith("crp://default/sources/source-file-")
    assert candidates[0]["occurred_at"] == candidates[0]["recorded_at"]
    sources = list(store.list("sources"))
    assets = list(store.list("workbench_original_assets"))
    links = list(store.list("source_asset_links"))
    assert len(sources) == len(assets) == len(links) == 1
    assert sources[0]["occurred_at"] == sources[0]["recorded_at"]
    assert links[0]["source_id"] == sources[0]["id"]
    assert links[0]["asset_id"] == assets[0]["id"]
    assert assets[0]["link_status"] == "linked"
    managed_path = (
        tmp_path
        / "library"
        / assets[0]["vault_ref"]
    )
    assert managed_path.read_bytes() == raw_content

    # 新 TestClient 模拟 sidecar 重启，仍能通过 Source 找到同一受管原档。
    with _client(tmp_path) as restarted:
        availability = restarted.get(
            f"/api/rebuild/library/sources/{sources[0]['id']}/original-asset"
        )
    assert availability.status_code == 200
    assert availability.json()["status"] == "available"
    assert availability.json()["reason"] == "verified_original_file"


def test_import_batch_reimport_is_idempotent_across_source_asset_and_candidate(
    tmp_path,
) -> None:
    raw_content = "重复导入不应分叉。".encode()
    request_body = {
        "files": [{
            "name": "same-note.txt",
            "size": len(raw_content),
            "type": "text/plain",
            "content_base64": base64.b64encode(raw_content).decode(),
        }],
    }
    client = _client(tmp_path)

    first = client.post("/api/rebuild/memory/import-batch", json=request_body)
    second = client.post("/api/rebuild/memory/import-batch", json=request_body)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["candidate_count"] == 1
    assert second.json()["candidate_count"] == 0
    assert second.json()["skipped"] == 1
    store = _store(tmp_path)
    assert len(list(store.list("sources"))) == 1
    assert len(list(store.list("workbench_original_assets"))) == 1
    assert len(list(store.list("source_asset_links"))) == 1
    assert len(list(store.list("memory_candidates"))) == 1
    asset = list(store.list("workbench_original_assets"))[0]
    assert asset["link_status"] == "linked"
    assert len(asset["linked_source_ids"]) == 1


def test_import_batch_applies_truthful_duplicate_resolutions(tmp_path) -> None:
    raw_content = b"duplicate resolution contract"
    digest = hashlib.sha256(raw_content).hexdigest()
    encoded = base64.b64encode(raw_content).decode()
    client = _client(tmp_path)

    initial = client.post("/api/rebuild/memory/import-batch", json={
        "files": [{
            "name": "initial.txt",
            "size": len(raw_content),
            "type": "text/plain",
            "content_base64": encoded,
        }],
    })
    assert initial.status_code == 201

    def submit(name: str, resolution: str):
        return client.post("/api/rebuild/memory/import-batch", json={
            "files": [{
                "name": name,
                "size": len(raw_content),
                "type": "text/plain",
                "content_base64": encoded,
            }],
            "duplicate_resolutions": {digest: resolution},
        })

    skipped = submit("skip.txt", "skip")
    l0_only = submit("archive-only.txt", "l0_only")
    imported = submit("initial.txt", "import_anyway")

    assert skipped.status_code == l0_only.status_code == imported.status_code == 201
    assert skipped.json()["items"][0]["status"] == "skipped_duplicate"
    assert l0_only.json()["items"][0]["status"] == "l0_only"
    assert l0_only.json()["candidate_count"] == 0
    assert imported.json()["candidate_count"] == 1
    store = _store(tmp_path)
    assert len(list(store.list("workbench_original_assets"))) == 1
    assert len(list(store.list("sources"))) == 2
    assert len(list(store.list("memory_candidates"))) == 2


def test_import_batch_rejects_resolution_without_existing_asset(tmp_path) -> None:
    raw_content = b"not imported yet"
    digest = hashlib.sha256(raw_content).hexdigest()
    client = _client(tmp_path)

    response = client.post("/api/rebuild/memory/import-batch", json={
        "files": [{
            "name": "new.txt",
            "size": len(raw_content),
            "type": "text/plain",
            "content_base64": base64.b64encode(raw_content).decode(),
        }],
        "duplicate_resolutions": {digest: "skip"},
    })

    assert response.status_code == 400
    store = _store(tmp_path)
    assert list(store.list("workbench_original_assets")) == []
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []
    assert list(store.list("memory_import_batches")) == []


def test_import_batch_same_source_keeps_project_candidate_scope(tmp_path) -> None:
    raw_content = b"shared source, project-scoped candidate"
    file_record = {
        "name": "shared.txt",
        "size": len(raw_content),
        "type": "text/plain",
        "content_base64": base64.b64encode(raw_content).decode(),
    }
    client = _client(tmp_path)

    first = client.post("/api/rebuild/memory/import-batch", json={
        "project_id": "project-one",
        "files": [file_record],
    })
    second = client.post("/api/rebuild/memory/import-batch", json={
        "project_id": "project-two",
        "files": [file_record],
    })

    assert first.status_code == second.status_code == 201
    candidates = list(_store(tmp_path).list("memory_candidates"))
    assert len(candidates) == 2
    assert {item["project_id"] for item in candidates} == {"project-one", "project-two"}
    assert len({item["source_ref"] for item in candidates}) == 1


def test_import_batch_returns_partial_on_invalid_base64(tmp_path) -> None:
    client = _client(tmp_path)
    good_bytes = "valid content".encode("utf-8")
    good = base64.b64encode(good_bytes).decode()
    response = client.post("/api/rebuild/memory/import-batch", json={
        "files": [
            {
                "name": "good.txt",
                "size": len(good_bytes),
                "type": "text/plain",
                "content_base64": good,
            },
            {"name": "bad.txt", "size": 5, "type": "text/plain", "content_base64": "@@@invalid@@@"},
        ],
    })
    assert response.status_code == 207
    payload = response.json()
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["error"] == "invalid_base64"


def test_import_batch_rejects_empty_files(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-batch", json={"files": []})
    assert response.status_code == 400


# ── 3. import-external ──


def test_import_external_succeeds_for_markdown_bundle(tmp_path) -> None:
    client = _client(tmp_path)
    markdown_content = "# 项目目标\n\n我喜欢简洁的界面设计。项目目标是构建个人记忆工作台。"
    response = client.post("/api/rebuild/memory/import-external", json={
        "bundle": markdown_content,
        "bundle_name": "notes.md",
    })
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["batch_id"].startswith("ext-")
    assert payload["platform"] == "generic"
    assert payload["candidate_count"] >= 1
    assert len(payload["candidates"]) >= 1
    assert payload["validation"]["is_valid"] is True
    store = _store(tmp_path)
    batch = store.read("memory_import_batches", payload["batch_id"])
    assert batch["status"] == "completed"
    assert batch["candidate_count"] == payload["candidate_count"]
    assert batch["occurred_at"] == batch["recorded_at"]
    assert batch["recorded_at"] == payload["recorded_at"]
    assert len(list(store.list("memory_candidates"))) == payload["candidate_count"]
    sources = list(store.list("sources"))
    assert len(sources) == len(payload["sources"])
    assert all(source["id"].startswith("source-external-") for source in sources)
    assert all(source["occurred_at"] is None for source in sources)
    assert all(source["recorded_at"] == payload["recorded_at"] for source in sources)
    assert all(
        candidate["source_ref"].startswith("crp://default/sources/source-external-")
        for candidate in store.list("memory_candidates")
    )
    source_id = sources[0]["id"]
    content_read = client.post(f"/api/rebuild/sources/{source_id}/content-read", json={})
    assert content_read.status_code == 200
    assert content_read.json()["status"] == "completed"
    assert "项目目标" in store.read("source_content_reads", f"content-read-{source_id}")["text"]


def test_import_external_succeeds_for_base64_zip_bundle(tmp_path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("notes/project.md", "# ZIP 项目\n\n项目目标是保留二进制导入合同。")
    client = _client(tmp_path)

    response = client.post("/api/rebuild/memory/import-external", json={
        "bundle_base64": base64.b64encode(archive.getvalue()).decode("ascii"),
        "bundle_encoding": "base64",
        "bundle_name": "knowledge.zip",
    })

    assert response.status_code == 201
    payload = response.json()
    assert payload["platform"] == "generic"
    assert payload["candidate_count"] >= 1
    store = _store(tmp_path)
    sources = list(store.list("sources"))
    assert len(sources) == 1
    assert "ZIP 项目" in sources[0]["metadata"]["content"]
    assert all(
        candidate["source_ref"].startswith("crp://default/sources/source-external-")
        for candidate in store.list("memory_candidates")
    )


@pytest.mark.parametrize("import_kind", ("file", "external"))
def test_memory_import_initial_batch_failure_prevents_product_writes(
    tmp_path, monkeypatch, import_kind,
) -> None:
    store = _install_import_batch_write_failure(tmp_path, monkeypatch, revision=0)
    client = _client(tmp_path)
    if import_kind == "file":
        content = "批次初始化必须早于文件写入".encode()
        endpoint = "/api/rebuild/memory/import-batch"
        payload = {"files": [{
            "name": "initial-failure.txt",
            "size": len(content),
            "type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
        }]}
    else:
        endpoint = "/api/rebuild/memory/import-external"
        payload = {"bundle": "# 初始批次失败\n\n不应写入外部资料。"}

    response = client.post(endpoint, json=payload)

    assert response.status_code == 500
    assert response.json()["status"] == "not_started"
    assert list(store.list("memory_import_batches")) == []
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []
    assert list(store.list("workbench_original_assets")) == []


@pytest.mark.parametrize("import_kind", ("file", "external"))
def test_memory_import_final_batch_failure_remains_auditable(
    tmp_path, monkeypatch, import_kind,
) -> None:
    store = _install_import_batch_write_failure(tmp_path, monkeypatch, revision=1)
    client = _client(tmp_path)
    if import_kind == "file":
        content = "最终批次失败仍需保留审计入口".encode()
        endpoint = "/api/rebuild/memory/import-batch"
        payload = {"files": [{
            "name": "final-failure.txt",
            "size": len(content),
            "type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
        }]}
    else:
        endpoint = "/api/rebuild/memory/import-external"
        payload = {
            "bundle": (
                "# 最终批次失败\n\n"
                "我喜欢简洁的界面设计。项目目标是构建可追踪的个人记忆工作台。"
            )
        }

    response = client.post(endpoint, json=payload)

    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "processing"
    batch = store.read("memory_import_batches", body["batch_id"])
    assert batch is not None
    assert batch["status"] == "processing"
    assert list(store.list("sources"))
    assert list(store.list("memory_candidates"))


def test_import_external_rejects_invalid_base64_without_persisting(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post("/api/rebuild/memory/import-external", json={
        "bundle_base64": "not-valid-%%%",
        "bundle_encoding": "base64",
        "bundle_name": "knowledge.zip",
    })

    assert response.status_code == 400
    assert "invalid" in response.json()["reason"]
    store = _store(tmp_path)
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []
    assert list(store.list("memory_import_batches")) == []


def test_external_custom_instruction_requires_l4_draft_and_user_confirmation(
    tmp_path,
) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "custom_instructions.md",
            "请保持简洁回答，并保留可追溯证据。",
        )
    client = _client(tmp_path)

    imported = client.post(
        "/api/rebuild/memory/import-external",
        json={
            "bundle_base64": base64.b64encode(archive.getvalue()).decode("ascii"),
            "bundle_encoding": "base64",
            "bundle_name": "persona.zip",
            "project_id": "project-alpha",
        },
    )

    assert imported.status_code == 201
    candidates = imported.json()["candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["layer"] == "L4"
    assert candidate["target_layer"] == "persona"
    assert candidate["status"] == "pending_review"
    assert candidate["source_id"].startswith("source-external-")
    store = _store(tmp_path)
    assert store.list("memory_persona") == ()
    assert store.list("memory_persona_drafts") == ()

    conflicted = client.post(
        "/api/rebuild/memory/candidates/review",
        json={
            "candidate_id": candidate["memory_id"],
            "action": "stage_l4_persona",
            "scope": "project",
            "expected_draft_revision": 1,
            "expected_current_revision": 0,
        },
    )

    assert conflicted.status_code == 409
    assert store.list("memory_persona") == ()
    assert store.list("memory_persona_drafts") == ()

    staged = client.post(
        "/api/rebuild/memory/candidates/review",
        json={
            "candidate_id": candidate["memory_id"],
            "action": "stage_l4_persona",
            "scope": "project",
            "expected_draft_revision": 0,
            "expected_current_revision": 0,
            "comment": "确认进入 L4 草稿，仍需最终确认。",
        },
    )

    assert staged.status_code == 200
    assert staged.json()["status"] == "promoted_l4_draft"
    persona_state = staged.json()["persona"]
    assert persona_state["confirmation"]["status"] == "pending"
    assert persona_state["draft_available"] is True
    assert persona_state["current_digest"]["ready"] is False
    assert store.list("memory_persona") == ()
    assert len(store.list("memory_persona_drafts")) == 1

    confirmed = client.post(
        "/api/rebuild/library/persona/confirm",
        json={
            "scope": "project",
            "status": "confirmed",
            "actor": "user",
            "reason": "确认该外部偏好是我的稳定规则。",
            "expected_draft_revision": persona_state["draft_cas_revision"],
            "expected_current_revision": persona_state["current_cas_revision"],
        },
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    current = store.read("memory_persona", "persona-project")
    assert current is not None
    assert current["confirmation"]["status"] == "confirmed"
    assert current["trust_status"] == "user_confirmed"
    assert store.list("memory_persona_drafts") == ()


@pytest.mark.parametrize(
    ("archive_bytes", "expected_reason"),
    [
        (b"PK\x03\x04not-a-valid-central-directory", "ZIP"),
        (b"\x00\x01\x02unrecognized", "no_matching_adapter"),
    ],
)
def test_import_external_rejects_unusable_archive_without_persisting(
    tmp_path, archive_bytes: bytes, expected_reason: str,
) -> None:
    client = _client(tmp_path)

    response = client.post("/api/rebuild/memory/import-external", json={
        "bundle_base64": base64.b64encode(archive_bytes).decode("ascii"),
        "bundle_encoding": "base64",
        "bundle_name": "knowledge.zip",
    })

    assert response.status_code == 400
    assert expected_reason in response.json()["reason"]
    store = _store(tmp_path)
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []
    assert list(store.list("memory_import_batches")) == []


def test_import_external_rejects_empty_zip_without_persisting(tmp_path) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w"):
        pass
    client = _client(tmp_path)

    response = client.post("/api/rebuild/memory/import-external", json={
        "bundle_base64": base64.b64encode(archive.getvalue()).decode("ascii"),
        "bundle_encoding": "base64",
        "bundle_name": "empty.zip",
    })

    assert response.status_code == 400
    assert "未包含可解析内容" in response.json()["reason"]
    store = _store(tmp_path)
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []
    assert list(store.list("memory_import_batches")) == []


def test_import_external_reimport_is_idempotent_and_unrelated_bundle_does_not_overwrite(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    first_body = {
        "bundle": "# 第一份\n\n项目目标是构建个人知识库。",
        "bundle_name": "first.md",
        "project_id": "project-one",
    }
    second_body = {
        "bundle": "# 第二份\n\n项目目标是制作研究报告。",
        "bundle_name": "second.md",
        "project_id": "project-one",
    }

    first = client.post("/api/rebuild/memory/import-external", json=first_body)
    repeated = client.post("/api/rebuild/memory/import-external", json=first_body)
    unrelated = client.post("/api/rebuild/memory/import-external", json=second_body)

    assert first.status_code == repeated.status_code == unrelated.status_code == 201
    assert first.json()["candidate_count"] >= 1
    assert repeated.json()["candidate_count"] == 0
    assert repeated.json()["skipped_count"] == first.json()["candidate_count"]
    store = _store(tmp_path)
    sources = list(store.list("sources"))
    candidates = list(store.list("memory_candidates"))
    assert len(sources) == 2
    assert len(candidates) == (
        first.json()["candidate_count"] + unrelated.json()["candidate_count"]
    )
    assert any("个人知识库" in source["metadata"]["content"] for source in sources)
    assert any("研究报告" in source["metadata"]["content"] for source in sources)


def test_import_external_same_content_keeps_project_scope(tmp_path) -> None:
    client = _client(tmp_path)
    body = {
        "bundle": "# 共享资料\n\n同一资料可用于两个项目。",
        "bundle_name": "shared.md",
    }
    one = client.post("/api/rebuild/memory/import-external", json={
        **body, "project_id": "project-one",
    })
    two = client.post("/api/rebuild/memory/import-external", json={
        **body, "project_id": "project-two",
    })

    assert one.status_code == two.status_code == 201
    candidates = list(_store(tmp_path).list("memory_candidates"))
    assert {item["project_id"] for item in candidates} == {"project-one", "project-two"}


def test_import_external_rejects_missing_bundle(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-external", json={})
    assert response.status_code == 400
    assert "bundle" in response.json()["reason"]


# ── 4. import-retry ──


def test_import_retry_removes_failure_from_batch(tmp_path) -> None:
    store = _store(tmp_path)
    # 先写入一个带 failure 的批次
    batch_record = {
        "batch_id": "batch-retry-test",
        "source_type": "file",
        "status": "partial",
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "needs_review": 1,
        "candidate_count": 1,
        "delta_summary": "test",
        "created_at": "2026-07-04T00:00:00Z",
        "completed_at": "2026-07-04T00:00:00Z",
        "failures": [
            {"id": "item-0-abc", "name": "bad.txt", "error": "invalid_base64", "user_message": "失败"},
        ],
        "series": [],
        "items": [],
    }
    store.write("memory_import_batches", "batch-retry-test", batch_record, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-retry", json={
        "batch_id": "batch-retry-test",
        "item_id": "item-0-abc",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["succeeded"] == 2
    assert payload["failed"] == 0
    assert payload["remaining_failures"] == 0


def test_import_retry_returns_404_for_missing_batch(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-retry", json={
        "batch_id": "nonexistent",
        "item_id": "item-1",
    })
    assert response.status_code == 404


def test_import_retry_rejects_missing_fields(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-retry", json={"batch_id": "x"})
    assert response.status_code == 400


# ── 5. import-batches (list) ──


def test_import_batches_list_returns_sorted_records(tmp_path) -> None:
    store = _store(tmp_path)
    for idx, ts in enumerate(["2026-07-04T00:00:00Z", "2026-07-05T00:00:00Z"], start=1):
        store.write(
            "memory_import_batches",
            f"batch-{idx}",
            {
                "batch_id": f"batch-{idx}",
                "source_type": "file",
                "status": "completed",
                "total": 1,
                "succeeded": 1,
                "failed": 0,
                "needs_review": 1,
                "candidate_count": 1,
                "delta_summary": "ok",
                "created_at": ts,
                "completed_at": ts,
                "failures": [],
                "series": [],
            },
            expected_revision=0,
        )

    client = _client(tmp_path)
    response = client.get("/api/rebuild/memory/import-batches")
    assert response.status_code == 200
    payload = response.json()
    # 倒序：batch-2 在前
    assert payload["count"] == 2
    assert payload["items"][0]["batch_id"] == "batch-2"
    assert payload["items"][1]["batch_id"] == "batch-1"
    assert payload["items"][0]["occurred_at"] == "2026-07-05T00:00:00Z"
    assert payload["items"][0]["recorded_at"] == "2026-07-05T00:00:00Z"
    assert payload["read_only"] is True


def test_import_batches_list_filters_by_project_id(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_import_batches",
        "batch-a",
        {
            "batch_id": "batch-a",
            "source_type": "file",
            "status": "completed",
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "needs_review": 1,
            "candidate_count": 1,
            "delta_summary": "ok",
            "created_at": "2026-07-04T00:00:00Z",
            "completed_at": "2026-07-04T00:00:00Z",
            "failures": [],
            "series": [],
            "project_id": "proj-a",
        },
        expected_revision=0,
    )
    store.write(
        "memory_import_batches",
        "batch-b",
        {
            "batch_id": "batch-b",
            "source_type": "file",
            "status": "completed",
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "needs_review": 1,
            "candidate_count": 1,
            "delta_summary": "ok",
            "created_at": "2026-07-04T00:00:00Z",
            "completed_at": "2026-07-04T00:00:00Z",
            "failures": [],
            "series": [],
            "project_id": "proj-b",
        },
        expected_revision=0,
    )

    client = _client(tmp_path)
    response = client.get("/api/rebuild/memory/import-batches?project_id=proj-a")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["batch_id"] == "batch-a"


def test_import_batches_list_classifies_old_runtime_processing_without_writing(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_import_batches",
        "batch-old-runtime",
        {
            "batch_id": "batch-old-runtime",
            "source_type": "file",
            "status": "processing",
            "runtime_session_id": "ended-runtime",
            "created_at": "2026-07-27T00:00:00Z",
        },
        expected_revision=0,
    )

    response = _client(tmp_path).get("/api/rebuild/memory/import-batches")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "interrupted"
    assert item["stored_status"] == "processing"
    assert item["recovery_required"] is True
    assert item["recovery_action"] == "confirm_interrupted"
    assert item["cas_revision"] == 1
    assert store.read("memory_import_batches", "batch-old-runtime")["status"] == "processing"


def test_import_batches_list_keeps_current_runtime_processing_active(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_import_batches",
        "batch-current-runtime",
        {
            "batch_id": "batch-current-runtime",
            "source_type": "file",
            "status": "processing",
            "runtime_session_id": product_memory_import_records._MEMORY_IMPORT_RUNTIME_SESSION_ID,
            "created_at": "2026-07-27T00:00:00Z",
        },
        expected_revision=0,
    )

    response = _client(tmp_path).get("/api/rebuild/memory/import-batches")

    item = response.json()["items"][0]
    assert item["status"] == "processing"
    assert item["stored_status"] == "processing"
    assert item["recovery_required"] is False


def test_recover_interrupted_import_batch_requires_confirmation_and_cas(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_import_batches",
        "batch-interrupted",
        {
            "batch_id": "batch-interrupted",
            "source_type": "external",
            "status": "processing",
            "runtime_session_id": "ended-runtime",
            "created_at": "2026-07-27T00:00:00Z",
        },
        expected_revision=0,
    )
    client = _client(tmp_path)

    unconfirmed = client.post(
        "/api/rebuild/memory/import-batches/recover-interrupted",
        json={"batch_id": "batch-interrupted", "expected_revision": 1},
    )
    stale = client.post(
        "/api/rebuild/memory/import-batches/recover-interrupted",
        json={
            "batch_id": "batch-interrupted",
            "expected_revision": 2,
            "confirm": True,
        },
    )
    recovered = client.post(
        "/api/rebuild/memory/import-batches/recover-interrupted",
        json={
            "batch_id": "batch-interrupted",
            "expected_revision": 1,
            "confirm": True,
        },
    )
    replay = client.post(
        "/api/rebuild/memory/import-batches/recover-interrupted",
        json={
            "batch_id": "batch-interrupted",
            "expected_revision": 1,
            "confirm": True,
        },
    )

    assert unconfirmed.status_code == 400
    assert stale.status_code == 409
    assert recovered.status_code == 200
    assert recovered.json()["idempotent"] is False
    assert recovered.json()["batch"]["status"] == "interrupted"
    assert recovered.json()["batch"]["cas_revision"] == 2
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    stored = store.read("memory_import_batches", "batch-interrupted")
    assert stored["status"] == "interrupted"
    assert stored["recovery_action"] == "reimport_original_input"


def test_recover_interrupted_import_batch_rejects_current_runtime(tmp_path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_import_batches",
        "batch-current-runtime",
        {
            "batch_id": "batch-current-runtime",
            "status": "processing",
            "runtime_session_id": product_memory_import_records._MEMORY_IMPORT_RUNTIME_SESSION_ID,
        },
        expected_revision=0,
    )

    response = _client(tmp_path).post(
        "/api/rebuild/memory/import-batches/recover-interrupted",
        json={
            "batch_id": "batch-current-runtime",
            "expected_revision": 1,
            "confirm": True,
        },
    )

    assert response.status_code == 409
    assert "active runtime session" in response.json()["reason"]
    assert store.read("memory_import_batches", "batch-current-runtime")["status"] == "processing"


# ── 6. candidates/review ──


def test_candidates_review_confirm(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "mem-confirm-1", {
        "schema_version": "1.0.0",
        "id": "mem-confirm-1",
        "memory_id": "mem-confirm-1",
        "layer": "L1",
        "type": "fact",
        "content": "test content",
        "summary": "test summary",
        "status": "candidate",
        "trust_level": "high",
        "group": "needs_review",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "mem-confirm-1",
        "action": "confirm",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "confirmed"
    assert payload["candidate"]["layer"] == "L1"


def test_candidates_review_promote_l3_changes_layer(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "mem-promote-1", {
        "schema_version": "1.0.0",
        "id": "mem-promote-1",
        "memory_id": "mem-promote-1",
        "layer": "L1",
        "type": "fact",
        "content": "test",
        "summary": "test",
        "status": "candidate",
        "trust_level": "high",
        "group": "needs_review",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "mem-promote-1",
        "action": "promote_l3",
    })
    assert response.status_code == 200
    assert response.json()["candidate"]["layer"] == "L3"


def test_candidates_review_edit_updates_content(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "mem-edit-1", {
        "schema_version": "1.0.0",
        "id": "mem-edit-1",
        "memory_id": "mem-edit-1",
        "layer": "L1",
        "type": "fact",
        "content": "original",
        "summary": "original summary",
        "status": "candidate",
        "trust_level": "high",
        "group": "needs_review",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "mem-edit-1",
        "action": "edit",
        "edits": {"content": "edited content", "summary": "edited summary"},
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate"]["content"] == "edited content"
    assert payload["candidate"]["summary"] == "edited summary"


def test_candidates_review_returns_404_for_missing(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "nonexistent",
        "action": "confirm",
    })
    assert response.status_code == 404


def test_candidates_review_rejects_invalid_action(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "x",
        "action": "invalid",
    })
    assert response.status_code == 400


# ── 7. candidates/conflict ──


def test_candidates_conflict_resolve_keep_existing(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidate_conflicts", "conflict-1", {
        "conflict_id": "conflict-1",
        "existing": {"content": "old"},
        "incoming": {"content": "new"},
        "status": "needs_review",
        "resolution": "",
        "created_at": "2026-07-04T00:00:00Z",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": "conflict-1",
        "resolution": "keep_existing",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "resolved"
    assert payload["resolution"] == "keep_existing"


def test_candidates_conflict_merge_with_merged_content(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidate_conflicts", "conflict-2", {
        "conflict_id": "conflict-2",
        "existing": {"content": "old"},
        "incoming": {"content": "new"},
        "status": "needs_review",
        "resolution": "",
        "created_at": "2026-07-04T00:00:00Z",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": "conflict-2",
        "resolution": "merge",
        "merged_content": "merged result",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["conflict"]["merged"]["content"] == "merged result"


def test_candidates_conflict_returns_404_for_missing(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": "nonexistent",
        "resolution": "pending",
    })
    assert response.status_code == 404


def test_candidates_conflict_rejects_invalid_resolution(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": "x",
        "resolution": "invalid",
    })
    assert response.status_code == 400


def test_candidates_conflict_accepts_accept_new_alias(tmp_path) -> None:
    """前端使用 accept_new，后端应接受并归一化为 accept_incoming。"""
    store = _store(tmp_path)
    store.write("memory_candidate_conflicts", "conflict-alias-1", {
        "conflict_id": "conflict-alias-1",
        "existing": {"content": "old"},
        "incoming": {"content": "new"},
        "status": "needs_review",
        "resolution": "",
        "created_at": "2026-07-04T00:00:00Z",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": "conflict-alias-1",
        "resolution": "accept_new",
    })
    assert response.status_code == 200
    payload = response.json()
    # 归一化后存储的 resolution 应为 accept_incoming
    assert payload["resolution"] == "accept_incoming"


def test_candidates_conflict_accepts_needs_review_alias(tmp_path) -> None:
    """前端使用 needs_review，后端应接受并归一化为 pending。"""
    store = _store(tmp_path)
    store.write("memory_candidate_conflicts", "conflict-alias-2", {
        "conflict_id": "conflict-alias-2",
        "existing": {"content": "old"},
        "incoming": {"content": "new"},
        "status": "needs_review",
        "resolution": "",
        "created_at": "2026-07-04T00:00:00Z",
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": "conflict-alias-2",
        "resolution": "needs_review",
    })
    assert response.status_code == 200
    payload = response.json()
    # 归一化后存储的 resolution 应为 pending，status 应为 needs_review
    assert payload["resolution"] == "pending"
    assert payload["status"] == "needs_review"


# ── 8. export ──


def test_export_returns_result_for_markdown_preset(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "mem-export-1", {
        "schema_version": "1.0.0",
        "id": "mem-export-1",
        "memory_id": "mem-export-1",
        "layer": "L1",
        "type": "fact",
        "content": "可导出的内容",
        "summary": "summary",
        "status": "confirmed",
        "trust_level": "high",
        "group": "auto_publish",
        "evidence_refs": [],
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export", json={
        "preset": "generic_markdown_knowledge_base",
        "scope": {"redact_secrets": True},
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["preset"] == "generic_markdown_knowledge_base"
    assert payload["format"] == "markdown"
    assert payload["memory_count"] == 1
    assert payload["error"] == ""


def test_export_rejects_missing_preset(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export", json={"scope": {}})
    assert response.status_code == 400


def test_import_batch_rejects_declared_size_mismatch_without_candidate(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/import-batch", json={
        "files": [{
            "name": "truncated.txt",
            "size": 999,
            "type": "text/plain",
            "content_base64": base64.b64encode(b"actual").decode(),
        }],
    })

    assert response.status_code == 400
    assert response.json()["failures"][0]["error"] == "size_mismatch"
    assert list(_store(tmp_path).list("memory_candidates")) == []


def test_export_metadata_and_preview_reject_unknown_preset(tmp_path) -> None:
    client = _client(tmp_path)

    metadata = client.post("/api/rebuild/memory/export", json={
        "preset": "unknown",
        "scope": {},
    })
    preview = client.post("/api/rebuild/memory/export/preview", json={
        "preset": "unknown",
        "scope": {},
    })

    assert metadata.status_code == 400
    assert preview.status_code == 400
    assert metadata.json()["reason"] == "unknown_preset"
    assert preview.json()["reason"] == "unknown_preset"


def test_export_file_returns_actual_attachment_bytes(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "mem-export-file-1", {
        "schema_version": "1.0.0",
        "id": "mem-export-file-1",
        "memory_id": "mem-export-file-1",
        "layer": "L1",
        "type": "fact",
        "content": "真实导出内容 api_key=sk-1234567890abcdef",
        "summary": "summary",
        "status": "confirmed",
        "trust_level": "high",
        "group": "auto_publish",
        "evidence_refs": [],
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/file", json={
        "preset": "generic_markdown_knowledge_base",
        "scope": {"redact_secrets": False},
    })

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="memory_knowledge_base_export-'
    )
    assert response.headers["x-chriptmas-export-format"] == "markdown"
    assert "真实导出内容".encode() in response.content
    assert b"sk-1234567890abcdef" not in response.content


def test_export_file_rejects_missing_and_unknown_preset(tmp_path) -> None:
    client = _client(tmp_path)
    missing = client.post("/api/rebuild/memory/export/file", json={"scope": {}})
    unknown = client.post("/api/rebuild/memory/export/file", json={
        "preset": "unknown",
        "scope": {},
    })

    assert missing.status_code == 400
    assert unknown.status_code == 400


@pytest.mark.parametrize(("preset", "expected_format", "extension"), (
    ("full_asset_package", "zip", ".zip"),
    ("generic_markdown_knowledge_base", "markdown", ".md"),
    ("compact_persona_prompt", "prompt_text", ".md"),
    ("full_memory_brief", "markdown", ".md"),
    ("generic_llm_project_knowledge", "markdown", ".md"),
    ("generic_custom_instructions", "markdown", ".md"),
    ("generic_rag_corpus", "ndjson", ".ndjson"),
))
def test_export_file_supports_every_declared_preset(
    tmp_path, preset: str, expected_format: str, extension: str,
) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/file", json={
        "preset": preset,
        "scope": {},
    })

    assert response.status_code == 200
    assert response.headers["x-chriptmas-export-format"] == expected_format
    assert response.headers["content-disposition"].endswith(f'{extension}"')


def test_full_asset_file_uses_published_memory_source_tag_evidence_and_round_trips(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    store.write("sources", "source-export-authority", {
        "id": "source-export-authority",
        "type": "text",
        "title": "正式来源",
        "storage_uri": "crp://default/sources/source-export-authority",
        "media_type": "text/plain",
        "created_at": "2026-07-24T00:00:00+00:00",
        "metadata": {},
    }, expected_revision=0)
    ObjectStoreMemoryStore(store).publish("atom", {
        "id": "atom-export-authority",
        "source_id": "source-export-authority",
        "content": "正式发布记忆",
        "atom_type": "fact",
        "tags": ["authority"],
        "confidence": 1.0,
        "source_refs": [{
            "source_id": "source-export-authority",
            "locator": "char:0-6",
        }],
        "created_at": "2026-07-24T00:00:00+00:00",
        "updated_at": "2026-07-24T00:00:00+00:00",
        "trust_status": "user_confirmed",
    })
    client = _client(tmp_path)

    preview = client.post("/api/rebuild/memory/export/preview", json={
        "preset": "full_asset_package",
        "scope": {"skip_low_trust": False},
    })
    exported = client.post("/api/rebuild/memory/export/file", json={
        "preset": "full_asset_package",
        "scope": {"skip_low_trust": False},
    })

    assert preview.status_code == 200
    assert preview.json()["manifest"]["memory_count"] == 1
    assert preview.json()["manifest"]["source_count"] == 1
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content), "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["memory_count"] == 1
        assert manifest["source_count"] == 1
        assert manifest["tag_count"] == 1
        assert manifest["evidence_link_count"] == 1
        assert "atom-export-authority" in archive.read(
            "memories/l1_atomic_facts.ndjson"
        ).decode()
        assert "source-export-authority" in archive.read(
            "sources/source_manifest.ndjson"
        ).decode()

    round_trip = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(exported.content).decode(),
    })
    assert round_trip.status_code == 200
    assert round_trip.json()["is_valid"] is True
    assert round_trip.json()["rebuilt"] is True


# ── 9. export/preview ──


def test_export_preview_returns_manifest_and_samples(tmp_path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "mem-preview-1", {
        "schema_version": "1.0.0",
        "id": "mem-preview-1",
        "memory_id": "mem-preview-1",
        "layer": "L1",
        "type": "fact",
        "content": "preview content with api_key=sk-1234567890",
        "summary": "summary",
        "status": "confirmed",
        "trust_level": "high",
        "group": "auto_publish",
        "evidence_refs": [],
    }, expected_revision=0)

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/preview", json={
        "preset": "generic_markdown_knowledge_base",
        "scope": {"redact_secrets": True},
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["preset"] == "generic_markdown_knowledge_base"
    assert payload["total_memories"] == 1
    assert payload["manifest"]["memory_count"] == 1
    # 含 api_key 的内容应触发脱敏采样
    assert len(payload["redaction_samples"]) >= 1
    assert payload["redaction_samples"][0]["before"] == "[敏感内容已隐藏]"
    assert "sk-1234567890" not in response.text


def test_export_preview_rejects_missing_preset(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/preview", json={})
    assert response.status_code == 400


# ── 10. export/round-trip ──


def _build_valid_asset_zip(*, project_id: str | None = None) -> bytes:
    """构造一个合法的 Memory Asset Package ZIP。"""
    manifest = {
        "format": "memory_asset_package",
        "version": "1.0",
        "export_batch_id": "rt-test",
        "created_at": "2026-07-04T00:00:00Z",
        "memory_count": 1,
    }
    l1_memory = {
        "memory_id": "rt-mem-1",
        "layer": "L1",
        "type": "fact",
        "content": "round-trip 重建测试",
        "summary": "rt summary",
        "confidence": 0.9,
        "trust_level": "high",
        "source_platform": "roundtrip",
        "source_ref": "rt-source-test",
        "evidence_refs": [],
        "created_at": "2026-07-04T00:00:00Z",
        "privacy_level": "private",
    }
    if project_id is not None:
        l1_memory["project_id"] = project_id
    l1_line = json.dumps(l1_memory, ensure_ascii=False)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("memories/l1_atomic_facts.ndjson", l1_line)
        zf.writestr("memories/l2_scenarios.ndjson", "")
        zf.writestr("memories/l3_persona_series_project_skill.ndjson", "")
        zf.writestr(
            "sources/source_manifest.ndjson",
            json.dumps({
                "source_id": "rt-source-test",
                "source_type": "text",
                "title": "round-trip 测试来源",
                "content_ref": "crp://default/sources/rt-source-test",
                "media_type": "text/plain",
                "created_at": "2026-07-04T00:00:00Z",
                "is_audio_visual": False,
            }, ensure_ascii=False),
        )
    return buf.getvalue()


def _build_hierarchy_asset_zip() -> bytes:
    memories = {
        "memories/l1_atomic_facts.ndjson": [{
            "memory_id": "atom-portable",
            "layer": "L1",
            "type": "fact",
            "content": "可追溯的 Atom",
            "project_id": "project-portable",
            "source_ref": "source-portable",
        }],
        "memories/l2_scenarios.ndjson": [{
            "memory_id": "scenario-portable",
            "layer": "L2",
            "type": "scenario",
            "content": "引用 Atom 的 Scenario",
            "project_id": "project-portable",
            "series_id": "series-portable",
            "atom_ids": ["atom-portable"],
            "source_ref": "source-portable",
        }],
        "memories/l3_persona_series_project_skill.ndjson": [{
            "memory_id": "series-portable",
            "layer": "L3",
            "type": "series_memory",
            "content": "引用 Scenario 的 Series Memory",
            "project_id": "project-portable",
            "series_id": "series-portable",
            "scenario_ids": ["scenario-portable"],
            "source_ref": "source-portable",
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({
            "format": "memory_asset_package",
            "version": "1.2",
            "memory_count": 3,
            "source_asset_count": 0,
        }))
        for path, rows in memories.items():
            zf.writestr(
                path,
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            )
        zf.writestr("memories/l4_persona.ndjson", "")
        zf.writestr("sources/source_manifest.ndjson", json.dumps({
            "source_id": "source-portable",
            "source_type": "text",
            "title": "层级迁移来源",
            "content_ref": "crp://default/sources/source-portable",
            "media_type": "text/plain",
            "created_at": "2026-07-27T00:00:00Z",
            "is_audio_visual": False,
        }, ensure_ascii=False))
        zf.writestr("sources/source_assets.ndjson", "")
    return buf.getvalue()


def _build_project_skill_asset_zip() -> bytes:
    source_ref = {"source_id": "source-skill-portable", "locator": "source"}
    skill = {
        "schema_version": "1.0.0",
        "id": "skill-project-portable",
        "project_id": "project-portable",
        "name": "便携项目方法",
        "purpose": "保留资产包中的可复用项目处理方法。",
        "markdown_uri": "crp://default/projects/project-portable/project-skill.md",
        "json_uri": "crp://default/projects/project-portable/project-skill.json",
        "markdown_revision": 3,
        "json_revision": 3,
        "required_context": [{
            "context_id": "context-portable",
            "kind": "source",
            "object_id": "source-skill-portable",
            "uri": "crp://default/sources/source-skill-portable.json",
            "reason": "资产包原始依据",
            "stale": False,
        }],
        "output_rules": [{
            "rule_id": "rule-portable",
            "origin": "user",
            "rule": "输出前核对原始依据。",
            "priority": "must",
            "source_refs": [source_ref],
            "locked_by_user": True,
        }],
        "style_preferences": {
            "voice": "直接",
            "format_defaults": ["Markdown"],
        },
        "outline": [{
            "section_id": "sources",
            "title": "来源",
            "kind": "sources",
            "required": True,
        }],
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": [],
        },
        "source_refs": [source_ref],
        "evidence_refs": [source_ref],
        "decision_log": [{
            "decision_id": "decision-portable",
            "reason": "用户在原 Vault 中确认",
            "actor": "user",
            "created_at": "2026-07-27T00:00:00+00:00",
        }],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "revision": 3,
        "status": "active",
        "trust_status": "user_confirmed",
        "created_at": "2026-07-27T00:00:00+00:00",
        "updated_at": "2026-07-27T00:01:00+00:00",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": "memory_asset_package",
            "version": "1.2",
            "memory_count": 0,
            "project_skill_count": 1,
            "source_asset_count": 0,
        }))
        for path in (
            "memories/l1_atomic_facts.ndjson",
            "memories/l2_scenarios.ndjson",
            "memories/l3_persona_series_project_skill.ndjson",
            "memories/l4_persona.ndjson",
            "sources/source_assets.ndjson",
        ):
            archive.writestr(path, "")
        archive.writestr("sources/source_manifest.ndjson", json.dumps({
            "source_id": "source-skill-portable",
            "source_type": "text",
            "title": "项目方法来源",
            "content_ref": "crp://default/sources/source-skill-portable",
            "media_type": "text/plain",
            "created_at": "2026-07-27T00:00:00+00:00",
            "is_audio_visual": False,
        }, ensure_ascii=False))
        archive.writestr(
            "project_skills/project_skill_cards.ndjson",
            json.dumps(skill, ensure_ascii=False),
        )
    return buf.getvalue()


def _project_skill_zip_without_source_refs() -> bytes:
    source = _build_project_skill_asset_zip()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source), "r") as original:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rewritten:
            for info in original.infolist():
                content = original.read(info.filename)
                if info.filename == "project_skills/project_skill_cards.ndjson":
                    skill = json.loads(content)
                    skill["source_refs"] = []
                    content = json.dumps(skill, ensure_ascii=False).encode()
                rewritten.writestr(info.filename, content)
    return output.getvalue()


def test_round_trip_rebuilds_candidates_from_valid_zip(tmp_path) -> None:
    zip_bytes = _build_valid_asset_zip()
    zip_b64 = base64.b64encode(zip_bytes).decode()

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": zip_b64,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_valid"] is True
    assert payload["rebuilt"] is True
    assert payload["rebuilt_count"] == 1
    assert payload["source_imported_count"] == 1
    assert payload["import_batch_id"].startswith("roundtrip-")

    # 验证候选已重建到 ObjectStore
    store = _store(tmp_path)
    candidates = list(store.list("memory_candidates"))
    assert len(candidates) == 1
    assert candidates[0]["id"] == "rt-mem-1"
    assert candidates[0]["status"] == "pending_review"
    assert candidates[0]["group"] == "needs_review"
    assert candidates[0]["target_layer"] == "atom"
    source = store.read("sources", "rt-source-test")
    assert source["processing_state"] == "reference_only"
    assert source["metadata"]["raw_content_restored"] is False


def test_round_trip_restores_project_skill_through_review_publication_and_restart(
    tmp_path,
) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_project_skill_asset_zip()).decode(),
    })
    assert imported.status_code == 200, imported.text
    assert imported.json()["rebuilt_count"] == 1
    assert imported.json()["project_skill_imported_count"] == 1
    assert imported.json()["manifest"]["project_skill_count"] == 1

    store = _store(tmp_path)
    candidate = store.read("memory_candidates", "skill-project-portable")
    assert candidate["status"] == "pending_review"
    assert candidate["target_layer"] == "project_skill"
    assert candidate["project_skill_draft"]["name"] == "便携项目方法"
    assert list(store.list("project_skills")) == []

    reviewed = client.post(
        "/api/rebuild/memory-candidates/skill-project-portable/review",
        json={
            "action": "promote_to_project_skill",
            "reason": "用户确认便携 Project Skill 进入 staging。",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    draft_id = reviewed.json()["promoted_object_id"]
    assert store.read("project_skills", "skill-project-portable") is None

    published = client.post(
        f"/api/rebuild/staging-project-skills/{draft_id}/publication",
        json={
            "confirm": True,
            "reason": "用户确认发布便携 Project Skill。",
        },
    )
    assert published.status_code == 200, published.text

    restarted = _store(tmp_path)
    restored = AggregateRepositoryFactory(
        runtime_root=tmp_path,
        namespace_id="default",
        json_store=restarted,
    ).project_skill_repository().load("project-portable")
    assert restored["name"] == "便携项目方法"
    assert restored["purpose"] == "保留资产包中的可复用项目处理方法。"
    assert restored["output_rules"][0]["rule"] == "输出前核对原始依据。"
    assert restored["style_preferences"]["voice"] == "直接"
    assert restored["outline"][0]["section_id"] == "sources"
    assert restored["required_context"][0]["context_id"] == "context-portable"
    assert restored["decision_log"][0]["decision_id"] == "decision-portable"


def test_round_trip_project_skill_reimport_is_idempotent(tmp_path) -> None:
    client = _client(tmp_path)
    payload = {
        "zip_base64": base64.b64encode(_build_project_skill_asset_zip()).decode(),
    }
    first = client.post("/api/rebuild/memory/export/round-trip", json=payload)
    repeated = client.post("/api/rebuild/memory/export/round-trip", json=payload)

    assert first.status_code == 200
    assert first.json()["rebuilt_count"] == 1
    assert repeated.status_code == 200
    assert repeated.json()["rebuilt_count"] == 0
    assert repeated.json()["skipped_count"] == 1
    assert repeated.json()["project_skill_skipped_count"] == 1
    candidates = list(_store(tmp_path).list("memory_candidates"))
    assert [candidate["id"] for candidate in candidates] == [
        "skill-project-portable"
    ]


def test_round_trip_rejects_invalid_project_skill_before_any_write(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(
            _project_skill_zip_without_source_refs()
        ).decode(),
    })

    assert response.status_code == 400
    assert "project skill source_refs are required" in response.json()["reason"]
    store = _store(tmp_path)
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []


def test_round_trip_confirm_uses_formal_staging_and_is_idempotent(tmp_path) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(
            _build_valid_asset_zip(project_id="project-roundtrip")
        ).decode(),
    })
    assert imported.status_code == 200

    first = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "confirm",
        "comment": "用户确认导入 Atom 进入 staging。",
    })
    repeated = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "confirm",
        "comment": "重复确认不得创建第二份草稿。",
    })

    assert first.status_code == 200
    assert first.json()["status"] == "promoted"
    assert first.json()["promoted_layer"] == "atom"
    assert first.json()["promoted_object_id"] == "rt-mem-1"
    assert first.json()["publication_state"] == "staged_not_published"
    assert repeated.status_code == 200
    assert repeated.json()["promoted_object_id"] == "rt-mem-1"
    store = _store(tmp_path)
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    staged = list(records.list("staging_atoms"))
    assert len(staged) == 1
    assert staged[0].payload["id"] == "rt-mem-1"
    assert list(records.list("memory_atoms")) == []
    assert list(store.list("staging_atoms")) == []


def test_round_trip_legacy_candidate_without_project_requires_target_selection(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_valid_asset_zip()).decode(),
    })
    assert imported.status_code == 200

    response = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "confirm",
    })

    assert response.status_code == 409
    assert "choose a target project" in response.json()["reason"]
    candidate = _store(tmp_path).read("memory_candidates", "rt-mem-1")
    assert candidate["status"] == "pending_review"
    assert list(_store(tmp_path).list("staging_atoms")) == []


def test_legacy_candidate_project_assignment_is_cas_guarded_and_idempotent(
    tmp_path,
) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_valid_asset_zip()).decode(),
    })
    assert imported.status_code == 200
    store = _store(tmp_path)
    store.write("project_skills", "skill-project-existing", {
        "id": "skill-project-existing",
        "project_id": "project-existing",
        "name": "已有项目",
    }, expected_revision=0)
    options = client.get("/api/rebuild/memory/project-options")
    assert options.status_code == 200
    assert options.json() == {
        "projects": ["project-existing"],
        "content_included": False,
        "read_only": True,
    }
    revision = store.revision("memory_candidates", "rt-mem-1")

    assigned = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "assign_project",
        "project_id": "project-existing",
        "expected_revision": revision,
        "confirm": True,
    })
    repeated = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "assign_project",
        "project_id": "project-existing",
        "expected_revision": revision,
        "confirm": True,
    })

    assert assigned.status_code == 200
    assert assigned.json()["candidate"]["project_id"] == "project-existing"
    assert assigned.json()["candidate"]["status"] == "pending_review"
    assert assigned.json()["idempotent"] is False
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    reopened = _store(tmp_path)
    assert reopened.read("memory_candidates", "rt-mem-1")["project_id"] == "project-existing"
    assert list(reopened.list("staging_atoms")) == []

    staged = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "confirm",
    })
    assert staged.status_code == 200
    assert staged.json()["publication_state"] == "staged_not_published"
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    assert records.read("staging_atoms", "rt-mem-1") is not None
    assert list(reopened.list("staging_atoms")) == []


def test_legacy_candidate_project_assignment_rejects_unknown_and_stale_targets(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_valid_asset_zip()).decode(),
    })
    store = _store(tmp_path)
    revision = store.revision("memory_candidates", "rt-mem-1")
    unknown = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "assign_project",
        "project_id": "project-missing",
        "expected_revision": revision,
        "confirm": True,
    })
    store.write("project_skills", "skill-known", {
        "id": "skill-known",
        "project_id": "project-known",
    }, expected_revision=0)
    candidate = dict(store.read("memory_candidates", "rt-mem-1"))
    candidate["summary"] = "并发更新"
    store.write(
        "memory_candidates",
        "rt-mem-1",
        candidate,
        expected_revision=revision,
    )
    stale = client.post("/api/rebuild/memory/candidates/review", json={
        "candidate_id": "rt-mem-1",
        "action": "assign_project",
        "project_id": "project-known",
        "expected_revision": revision,
        "confirm": True,
    })

    assert unknown.status_code == 409
    assert "does not exist" in unknown.json()["reason"]
    assert stale.status_code == 409
    assert "revision changed" in stale.json()["reason"]
    assert store.read("memory_candidates", "rt-mem-1")["project_id"] == ""


def test_round_trip_staging_preserves_l1_l2_l3_portable_hierarchy(
    tmp_path,
) -> None:
    _activate_project_skill_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    imported = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_hierarchy_asset_zip()).decode(),
    })
    assert imported.status_code == 200
    assert imported.json()["rebuilt_count"] == 3

    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    staged_payloads = {}
    for candidate_id, staging_collection, publication_path in (
        ("atom-portable", "staging_atoms", "/api/rebuild/staging-atoms/atom-portable/publication"),
        ("scenario-portable", "staging_scenarios", "/api/rebuild/staging-scenarios/scenario-portable/publication"),
        ("series-portable", "staging_series_memory", "/api/rebuild/staging-series-memory/series-portable/publication"),
    ):
        reviewed = client.post("/api/rebuild/memory/candidates/review", json={
            "candidate_id": candidate_id,
            "action": "confirm",
            "comment": "确认便携层级对象进入 staging。",
        })
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["promoted_object_id"] == candidate_id
        assert reviewed.json()["publication_state"] == "staged_not_published"
        staged = records.read(staging_collection, candidate_id)
        assert staged is not None
        staged_payloads[candidate_id] = staged.payload
        published = client.post(publication_path, json={
            "confirm": True,
            "reason": "用户二次确认导入层级对象正式发布。",
        })
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "published"

    reopened = _store(tmp_path)
    atom = staged_payloads["atom-portable"]
    scenario = staged_payloads["scenario-portable"]
    series = staged_payloads["series-portable"]
    assert atom["id"] == "atom-portable"
    assert scenario["id"] == "scenario-portable"
    assert scenario["project_id"] == "project-portable"
    assert scenario["series_id"] == "series-portable"
    assert scenario["atom_ids"] == ["atom-portable"]
    assert series["id"] == "series-portable"
    assert series["series_id"] == "series-portable"
    assert series["project_ids"] == ["project-portable"]
    assert series["scenario_ids"] == ["scenario-portable"]
    assert len(records.list("memory_atoms")) == 1
    assert len(records.list("memory_scenarios")) == 1
    assert len(records.list("memory_series_memory")) == 1
    assert list(reopened.list("staging_atoms")) == []
    assert list(reopened.list("staging_scenarios")) == []
    assert list(reopened.list("staging_series_memory")) == []

    recalled = SQLiteMemoryReader(
        SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / "structured-records.sqlite3"
        )
    ).list_by_project(
        "project-portable"
    )
    assert {item["id"] for item in recalled} == {
        "atom-portable",
        "scenario-portable",
        "series-portable",
    }


def test_round_trip_rejects_invalid_base64(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": "@@@not base64@@@",
    })
    assert response.status_code == 400
    assert "base64" in response.json()["reason"].lower()


def test_round_trip_rejects_missing_zip(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={})
    assert response.status_code == 400
    assert "zip_base64" in response.json()["reason"]


def test_round_trip_returns_invalid_for_bad_zip(tmp_path) -> None:
    # 不是合法 ZIP
    bad_zip_b64 = base64.b64encode(b"not a zip file").decode()
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": bad_zip_b64,
    })
    assert response.status_code == 400
    payload = response.json()
    assert payload["is_valid"] is False
    assert payload["rebuilt"] is False


def test_round_trip_reimport_is_idempotent_and_conflict_safe(tmp_path) -> None:
    client = _client(tmp_path)
    encoded = base64.b64encode(_build_valid_asset_zip()).decode()

    first = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": encoded,
    })
    repeated = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": encoded,
    })

    assert first.status_code == 200
    assert first.json()["rebuilt_count"] == 1
    assert repeated.status_code == 200
    assert repeated.json()["rebuilt_count"] == 0
    assert repeated.json()["skipped_count"] == 1
    assert repeated.json()["conflict_count"] == 0
    assert repeated.json()["source_skipped_count"] == 1

    store = _store(tmp_path)
    existing = dict(store.read("memory_candidates", "rt-mem-1"))
    existing["content"] = "本地已编辑内容"
    store.write("memory_candidates", "rt-mem-1", existing, expected_revision=None)

    conflict = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": encoded,
    })

    assert conflict.status_code == 200
    assert conflict.json()["rebuilt_count"] == 0
    assert conflict.json()["conflict_count"] == 1
    assert store.read("memory_candidates", "rt-mem-1")["content"] == "本地已编辑内容"
    conflict_id = conflict.json()["conflict_ids"][0]
    assert store.read("memory_candidate_conflicts", conflict_id)["status"] == "needs_review"

    accepted = client.post("/api/rebuild/memory/candidates/conflict", json={
        "conflict_id": conflict_id,
        "resolution": "accept_incoming",
    })
    assert accepted.status_code == 200
    restored = store.read("memory_candidates", "rt-mem-1")
    assert restored["content"] == "round-trip 重建测试"
    assert restored["status"] == "candidate"
    assert restored["group"] == "needs_review"


def test_round_trip_malformed_memory_writes_nothing(tmp_path) -> None:
    manifest = {
        "format": "memory_asset_package",
        "version": "1.0",
        "memory_count": 1,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("memories/l1_atomic_facts.ndjson", "{not-json")
        archive.writestr("memories/l2_scenarios.ndjson", "")
        archive.writestr("memories/l3_persona_series_project_skill.ndjson", "")

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(buf.getvalue()).decode(),
    })

    assert response.status_code == 400
    assert response.json()["is_valid"] is False
    assert list(_store(tmp_path).list("memory_candidates")) == []
    assert list(_store(tmp_path).list("memory_import_batches")) == []


def test_round_trip_complete_preflight_failure_writes_nothing(tmp_path) -> None:
    source = io.BytesIO(_build_valid_asset_zip())
    target = io.BytesIO()
    with zipfile.ZipFile(source) as input_zip, zipfile.ZipFile(
        target, "w", zipfile.ZIP_DEFLATED,
    ) as output_zip:
        for entry in input_zip.infolist():
            output_zip.writestr(entry, input_zip.read(entry.filename))
        output_zip.writestr("evidence/evidence_graph.ndjson", "{not-json")

    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(target.getvalue()).decode(),
    })

    assert response.status_code == 400
    body = response.json()
    assert body["is_valid"] is False
    assert body["rebuilt"] is False
    assert body["rebuilt_count"] == 0
    assert any("evidence/evidence_graph.ndjson" in error for error in body["errors"])
    store = _store(tmp_path)
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []
    assert list(store.list("memory_import_batches")) == []


def test_round_trip_final_batch_write_failure_remains_auditable(
    tmp_path, monkeypatch,
) -> None:
    original_object_store = product_repositories._object_store
    store, settings = original_object_store(tmp_path)

    class _FailFinalBatchWrite:
        def __getattr__(self, name):
            return getattr(store, name)

        def write(self, collection, object_id, payload, expected_revision):
            if collection == "memory_import_batches" and expected_revision == 1:
                raise OSError("injected final batch write failure")
            return store.write(collection, object_id, payload, expected_revision)

    monkeypatch.setattr(
        product_repositories,
        "_object_store",
        lambda _runtime_root: (_FailFinalBatchWrite(), settings),
    )
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_valid_asset_zip()).decode(),
    })

    assert response.status_code == 500
    body = response.json()
    assert body["rebuilt"] is True
    assert body["status"] == "processing"
    batch = store.read("memory_import_batches", body["import_batch_id"])
    assert batch is not None
    assert batch["status"] == "processing"
    assert store.read("memory_candidates", "rt-mem-1") is not None


def test_round_trip_initial_batch_write_failure_prevents_product_writes(
    tmp_path, monkeypatch,
) -> None:
    original_object_store = product_repositories._object_store
    store, settings = original_object_store(tmp_path)

    class _FailInitialBatchWrite:
        def __getattr__(self, name):
            return getattr(store, name)

        def write(self, collection, object_id, payload, expected_revision):
            if collection == "memory_import_batches":
                raise OSError("injected initial batch write failure")
            return store.write(collection, object_id, payload, expected_revision)

    monkeypatch.setattr(
        product_repositories,
        "_object_store",
        lambda _runtime_root: (_FailInitialBatchWrite(), settings),
    )
    client = _client(tmp_path)
    response = client.post("/api/rebuild/memory/export/round-trip", json={
        "zip_base64": base64.b64encode(_build_valid_asset_zip()).decode(),
    })

    assert response.status_code == 500
    assert response.json()["rebuilt"] is False
    assert list(store.list("memory_import_batches")) == []
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []


# ── detect_file_category 单元测试 ──


def test_detect_file_category_matches_frontend_logic() -> None:
    from core.product_core.external_import_framework import detect_file_category
    assert detect_file_category("notes.md") == "text"
    assert detect_file_category("data.json") == "text"
    assert detect_file_category("photo.png") == "image"
    assert detect_file_category("photo.JPG") == "image"  # 大小写不敏感
    assert detect_file_category("clip.mp3") == "audio"
    assert detect_file_category("video.mp4") == "video"
    assert detect_file_category("bundle.zip") == "archive"
    assert detect_file_category("unknown") == "other"
    assert detect_file_category("") == "other"
    assert detect_file_category("noext") == "other"
