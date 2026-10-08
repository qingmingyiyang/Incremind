from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from backend.api.memory_export_assembler import (
    MemoryExportAssemblyError,
    assemble_memory_export,
)
from backend.api.memory_asset_package_importer import (
    import_memory_asset_package,
    parse_memory_asset_package,
)
from core.aggregate_repository_factory import AggregateRepositoryFactoryError
from core.memory_core import ObjectStoreMemoryStore
from core.product_core.memory_export_framework import ExportScope, export_memory
from core.product_core.workbench_original_asset import ResolveWorkbenchOriginalAsset
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_complete_graph(store: JsonObjectStore) -> None:
    store.write("sources", "source-one", {
        "schema_version": "1.0.0",
        "id": "source-one",
        "type": "text",
        "title": "项目来源 api_key=secret-value-123",
        "capture_mode": "inline",
        "storage_uri": "crp://default/sources/source-one",
        "original_url": None,
        "content_hash": "a" * 64,
        "media_type": "text/plain",
        "size_bytes": 10,
        "parser_version": None,
        "processing_state": "ready",
        "created_at": "2026-07-24T00:00:00+00:00",
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {"project_id": "project-one"},
    }, expected_revision=0)
    ObjectStoreMemoryStore(store).publish("atom", {
        "schema_version": "1.0.0",
        "id": "atom-one",
        "source_id": "source-one",
        "content": "正式事实",
        "atom_type": "fact",
        "tags": ["alpha", "shared"],
        "confidence": 0.9,
        "source_refs": [{"source_id": "source-one", "locator": "char:0-4"}],
        "revision": 1,
        "created_at": "2026-07-24T00:00:00+00:00",
        "updated_at": "2026-07-24T00:00:00+00:00",
        "trust_status": "user_confirmed",
    })
    store.write("memory_candidates", "candidate-one", {
        "id": "candidate-one",
        "memory_id": "candidate-one",
        "project_id": "project-one",
        "layer": "L2",
        "type": "decision",
        "content": "待确认决策",
        "tags": ["shared", "candidate"],
        "confidence": 0.5,
        "trust_level": "medium",
        "source_ref": "source-one",
        "evidence_refs": ["source-one#char:5-10"],
        "status": "candidate",
    }, expected_revision=0)
    store.write("memory_candidates", "duplicate-atom", {
        "id": "duplicate-atom",
        "memory_id": "atom-one",
        "project_id": "project-one",
        "layer": "L1",
        "type": "fact",
        "content": "不应覆盖正式事实",
        "status": "candidate",
    }, expected_revision=0)
    store.write("project_skills", "skill-one", {
        "id": "skill-one",
        "project_id": "project-one",
        "name": "项目规则",
        "description": "token=secret-value-456",
    }, expected_revision=0)
    store.write("memory_import_batches", "batch-one", {
        "id": "batch-one",
        "project_id": "project-one",
        "status": "completed",
        "note": "Cookie: session=secret-value-789",
    }, expected_revision=0)
    store.write("jobs", "job-one", {
        "id": "job-one",
        "project_id": "project-one",
        "source_id": "source-one",
        "kind": "intake",
        "status": "completed",
        "private_payload": "must not export",
    }, expected_revision=0)


def _seed_confirmed_persona(store: JsonObjectStore) -> None:
    store.write("memory_persona", "persona-global", {
        "id": "persona-global",
        "scope": "global",
        "statements": [{
            "id": "persona-style",
            "content": "回答时先给结论。",
            "category": "style",
            "confidence": 0.95,
        }],
        "evidence_refs": [{
            "object_type": "atom",
            "object_id": "atom-one",
            "source_refs": [{"source_id": "source-one", "locator": "char:0-4"}],
        }],
        "confirmation": {
            "required": True,
            "status": "confirmed",
            "actor": "user",
            "reason": "用户确认",
        },
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-24T00:00:00+00:00",
        "updated_at": "2026-07-24T00:01:00+00:00",
    }, expected_revision=0)


def _seed_original_asset(tmp_path: Path, store: JsonObjectStore) -> bytes:
    content = "可携带原档正文。".encode()
    digest = __import__("hashlib").sha256(content).hexdigest()
    asset_id = f"original-file-{digest[:16]}"
    target = tmp_path / "library" / "assets" / "originals" / digest[:2] / asset_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    store.write("workbench_original_assets", asset_id, {
        "id": asset_id,
        "asset_ref": f"crp-ref-default-assets-originals-{asset_id}",
        "vault_ref": f"assets/originals/{digest[:2]}/{asset_id}",
        "display_name": "原档.txt",
        "media_type": "text/plain",
        "byte_count": len(content),
        "sha256": digest,
    }, expected_revision=0)
    store.write("source_asset_links", f"source-asset-source-one-{asset_id}", {
        "id": f"source-asset-source-one-{asset_id}",
        "source_id": "source-one",
        "asset_id": asset_id,
        "role": "original",
        "content_hash": digest,
    }, expected_revision=0)
    return content


def test_assembler_combines_published_candidates_sources_tags_evidence_and_manifests(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_complete_graph(store)
    _seed_confirmed_persona(store)

    assembly = assemble_memory_export(
        runtime_root=tmp_path,
        namespace_id="default",
        store=store,
    )

    assert assembly.authority_identity == "json:object-store-v1"
    assert assembly.published_count == 2
    assert assembly.candidate_count == 2
    assert [item.memory_id for item in assembly.payload.memories] == [
        "atom-one",
        "candidate-one",
        "persona-global~persona-style",
    ]
    assert assembly.payload.memories[0].content == "正式事实"
    assert [item.source_id for item in assembly.payload.sources] == ["source-one"]
    assert {item.tag for item in assembly.payload.tags} == {"alpha", "candidate", "shared"}
    assert len(assembly.payload.evidence_links) == 4
    assert assembly.payload.project_skill_cards[0]["description"] == "[REDACTED]"
    assert "secret-value-789" not in json.dumps(assembly.payload.import_batches)
    assert "private_payload" not in assembly.payload.task_history[0]

    result = export_memory(
        assembly.payload,
        ExportScope(preset="full_asset_package", redact_secrets=True, skip_low_trust=False),
        export_batch_id="export-complete-test",
    )
    with zipfile.ZipFile(io.BytesIO(result.bytes_payload), "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        source_manifest = archive.read("sources/source_manifest.ndjson").decode()
        tag_index = json.loads(archive.read("tags/tag_index.json"))
        assert manifest["memory_count"] == 3
        assert manifest["project_skill_count"] == 1
        assert manifest["version"] == "1.2"
        assert manifest["layer_counts"]["L4"] == 1
        assert manifest["source_count"] == 1
        assert manifest["evidence_link_count"] == 4
        assert "source-one" in source_manifest
        assert "secret-value-123" not in source_manifest
        assert tag_index["shared"] == ["atom-one", "candidate-one"]
        l4 = [
            json.loads(line)
            for line in archive.read("memories/l4_persona.ndjson").decode().splitlines()
        ]
        assert l4[0]["layer"] == "L4"
        assert l4[0]["content"] == "回答时先给结论。"
        assert "source-one#char:0-4" in l4[0]["evidence_refs"]
        persona = json.loads(archive.read("persona/persona_structured.json"))
        assert persona["memory_count"] == 1


def test_full_package_carries_verified_original_and_skip_raw_keeps_source_manifest(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_complete_graph(store)
    expected = _seed_original_asset(tmp_path, store)
    assembly = assemble_memory_export(
        runtime_root=tmp_path,
        namespace_id="default",
        store=store,
    )
    assert len(assembly.payload.source_assets) == 1

    included = export_memory(
        assembly.payload,
        ExportScope(preset="full_asset_package", skip_raw_sources=False),
        export_batch_id="with-original",
    )
    with zipfile.ZipFile(io.BytesIO(included.bytes_payload), "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assets = [
            json.loads(line)
            for line in archive.read("sources/source_assets.ndjson").decode().splitlines()
        ]
        assert manifest["source_asset_count"] == 1
        assert archive.read(assets[0]["blob_path"]) == expected

    references_only = export_memory(
        assembly.payload,
        ExportScope(preset="full_asset_package", skip_raw_sources=True),
        export_batch_id="without-original",
    )
    with zipfile.ZipFile(io.BytesIO(references_only.bytes_payload), "r") as archive:
        assert "source-one" in archive.read("sources/source_manifest.ndjson").decode()
        assert archive.read("sources/source_assets.ndjson") == b""
        assert json.loads(archive.read("manifest.json"))["source_asset_count"] == 0


def test_exported_v12_package_restores_original_into_a_fresh_vault(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source-instance"
    source_store = _store(source_root)
    _seed_complete_graph(source_store)
    expected = _seed_original_asset(source_root, source_store)
    assembly = assemble_memory_export(
        runtime_root=source_root,
        namespace_id="default",
        store=source_store,
    )
    exported = export_memory(
        assembly.payload,
        ExportScope(preset="full_asset_package", skip_raw_sources=False),
        export_batch_id="cross-instance",
    )

    target_root = tmp_path / "target-instance"
    target_store = _store(target_root)
    result = import_memory_asset_package(
        store=target_store,
        package=parse_memory_asset_package(exported.bytes_payload),
        import_batch_id="cross-instance-import",
        created_at="2026-07-27T13:00:00Z",
        library_root=target_root / "library",
    )

    assert result.asset_imported_count == 1
    availability = ResolveWorkbenchOriginalAsset(
        object_store=target_store,
        library_root=target_root / "library",
    ).for_source("source-one")
    assert availability is not None
    assert availability.status == "available"
    assert availability.path is not None
    assert availability.path.read_bytes() == expected


def test_assembler_excludes_unconfirmed_persona_from_formal_l4(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_complete_graph(store)
    _seed_confirmed_persona(store)
    persona = dict(store.read("memory_persona", "persona-global") or {})
    persona["confirmation"] = {
        "required": True,
        "status": "pending",
        "actor": None,
        "reason": None,
    }
    persona["trust_status"] = "system_generated"
    store.write(
        "memory_persona",
        "persona-global",
        persona,
        expected_revision=store.revision("memory_persona", "persona-global"),
    )

    assembly = assemble_memory_export(
        runtime_root=tmp_path,
        namespace_id="default",
        store=store,
    )

    assert all(memory.layer != "L4" for memory in assembly.payload.memories)


def test_full_brief_separates_l4_persona_from_l3_project_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_complete_graph(store)
    _seed_confirmed_persona(store)

    assembly = assemble_memory_export(
        runtime_root=tmp_path,
        namespace_id="default",
        store=store,
    )
    result = export_memory(
        assembly.payload,
        ExportScope(preset="full_memory_brief"),
        export_batch_id="brief-five-layer",
    )
    text = result.bytes_payload.decode("utf-8")

    assert "## L4 稳定画像" in text
    assert "回答时先给结论。" in text
    assert "## L3 长期画像" not in text


def test_only_confirmed_export_excludes_pending_l4_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_complete_graph(store)
    _seed_confirmed_persona(store)
    store.write("memory_candidates", "persona-pending", {
        "id": "persona-pending",
        "memory_id": "persona-pending",
        "layer": "L4",
        "type": "preference",
        "content": "尚未确认的偏好",
        "source_ref": "source-one",
        "evidence_refs": ["source-one#char:4-8"],
        "status": "pending_review",
    }, expected_revision=0)

    assembly = assemble_memory_export(
        runtime_root=tmp_path,
        namespace_id="default",
        store=store,
    )
    assert any(
        memory.memory_id == "persona-pending" and not memory.confirmed
        for memory in assembly.payload.memories
    )

    result = export_memory(
        assembly.payload,
        ExportScope(preset="full_asset_package", only_confirmed=True),
        export_batch_id="only-confirmed-l4",
    )
    with zipfile.ZipFile(io.BytesIO(result.bytes_payload), "r") as archive:
        l4 = archive.read("memories/l4_persona.ndjson").decode("utf-8")

    assert "persona-global~persona-style" in l4
    assert "persona-pending" not in l4


def test_assembler_project_filter_excludes_unrelated_candidates_and_sources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_complete_graph(store)
    store.write("sources", "source-other", {
        "id": "source-other",
        "type": "text",
        "title": "other",
        "storage_uri": "crp://default/sources/source-other",
        "media_type": "text/plain",
        "created_at": "2026-07-24T00:00:00+00:00",
        "metadata": {"project_id": "project-other"},
    }, expected_revision=0)
    store.write("memory_candidates", "candidate-other", {
        "id": "candidate-other",
        "project_id": "project-other",
        "content": "other",
    }, expected_revision=0)

    assembly = assemble_memory_export(
        runtime_root=tmp_path,
        namespace_id="default",
        store=store,
        project_id="project-one",
    )

    assert all(item.memory_id != "candidate-other" for item in assembly.payload.memories)
    assert all(item.source_id != "source-other" for item in assembly.payload.sources)


def test_assembler_fails_closed_when_memory_authority_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    def reject(_self):
        raise AggregateRepositoryFactoryError("partially SQLite active")

    monkeypatch.setattr(
        "backend.api.memory_export_assembler.AggregateRepositoryFactory."
        "memory_publication_authority_resolution",
        reject,
    )
    with pytest.raises(MemoryExportAssemblyError, match="partially SQLite active"):
        assemble_memory_export(
            runtime_root=tmp_path,
            namespace_id="default",
            store=store,
        )


def test_assembler_rejects_candidate_with_unknown_layer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("memory_candidates", "bad-layer", {
        "id": "bad-layer",
        "memory_id": "bad-layer",
        "layer": "L9",
        "content": "must not be counted without a matching export file",
    }, expected_revision=0)

    with pytest.raises(MemoryExportAssemblyError, match="unsupported layer"):
        assemble_memory_export(
            runtime_root=tmp_path,
            namespace_id="default",
            store=store,
        )
