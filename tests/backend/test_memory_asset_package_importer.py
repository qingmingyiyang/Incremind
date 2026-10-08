from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from backend.api.memory_asset_package_importer import (
    MemoryAssetPackageImportError,
    import_memory_asset_package,
    parse_memory_asset_package,
)
from core.product_core.external_persona_candidate_staging import (
    StageExternalPersonaCandidate,
)
from core.product_core.persona import ObjectStorePersonaRepository
from core.product_core.workbench_original_asset import ResolveWorkbenchOriginalAsset
from core.storage_provider import (
    JsonObjectStore,
    SQLiteStructuredRecordStore,
    SourceAssetRuntimeStore,
)


def _package(
    rows: dict[str, list[dict[str, object] | str]],
    *,
    memory_count: int | None = None,
    version: str = "1.0",
    original_content: bytes | None = None,
) -> bytes:
    paths = {
        "L1": "memories/l1_atomic_facts.ndjson",
        "L2": "memories/l2_scenarios.ndjson",
        "L3": "memories/l3_persona_series_project_skill.ndjson",
        "L4": "memories/l4_persona.ndjson",
    }
    count = sum(len(items) for items in rows.values())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        manifest = {
            "format": "memory_asset_package",
            "version": version,
            "memory_count": count if memory_count is None else memory_count,
        }
        if version == "1.2":
            manifest["source_asset_count"] = 1 if original_content is not None else 0
        archive.writestr("manifest.json", json.dumps(manifest))
        for layer, path in paths.items():
            lines = [
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for item in rows.get(layer, [])
            ]
            archive.writestr(path, "\n".join(lines))
        archive.writestr(
            "sources/source_manifest.ndjson",
            json.dumps({
                "source_id": "source-one",
                "source_type": "text",
                "title": "导入来源",
                "content_ref": "crp://default/sources/source-one",
                "media_type": "text/plain",
                "created_at": "2026-07-24T00:00:00Z",
                "is_audio_visual": False,
            }, ensure_ascii=False),
        )
        if version == "1.2":
            rows = ""
            if original_content is not None:
                digest = __import__("hashlib").sha256(original_content).hexdigest()
                rows = json.dumps({
                    "source_id": "source-one",
                    "asset_id": f"original-file-{digest[:16]}",
                    "display_name": "原档.txt",
                    "media_type": "text/plain",
                    "byte_count": len(original_content),
                    "sha256": digest,
                    "blob_path": f"sources/content/{digest}",
                    "is_audio_visual": False,
                }, ensure_ascii=False)
                archive.writestr(f"sources/content/{digest}", original_content)
            archive.writestr("sources/source_assets.ndjson", rows)
    return buf.getvalue()


def _memory(memory_id: str = "memory-one", *, layer: str = "L1") -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "layer": layer,
        "type": "fact",
        "content": "用户确认的事实",
        "summary": "事实摘要",
        "tags": ["confirmed"],
        "confidence": 0.9,
        "trust_level": "high",
        "evidence_refs": ["source-one#char:0-8"],
        "privacy_level": "private",
    }


@pytest.mark.parametrize(("payload", "message"), (
    (_package({"L1": ["{bad-json"]}), "invalid JSON"),
    (_package({"L1": [_memory()]}, memory_count=2), "does not match"),
    (_package({"L1": [_memory(layer="L2")]}), "does not match L1"),
    (
        _package({"L1": [_memory("duplicate")], "L2": [_memory("duplicate", layer="L2")]}),
        "duplicate memory_id",
    ),
))
def test_parse_memory_asset_package_rejects_invalid_package(
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(MemoryAssetPackageImportError, match=message):
        parse_memory_asset_package(payload)


def test_import_is_pending_idempotent_and_preserves_conflicting_local_record(
    tmp_path: Path,
) -> None:
    store = JsonObjectStore(tmp_path / "object-store")
    package = parse_memory_asset_package(_package({"L1": [_memory()]}))

    first = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="batch-one",
        created_at="2026-07-24T00:00:00Z",
    )
    repeated = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="batch-two",
        created_at="2026-07-24T00:01:00Z",
    )

    assert first.imported_count == 1
    assert repeated.skipped_count == 1
    candidate = store.read("memory_candidates", "memory-one")
    assert candidate["status"] == "pending_review"
    assert candidate["group"] == "needs_review"
    assert candidate["target_layer"] == "atom"
    assert candidate["proposed_content"] == "用户确认的事实"
    assert candidate["source_refs"] == [
        {"source_id": "source-one", "locator": "char:0-8"}
    ]
    assert candidate["review"]["requires_user_confirmation"] is True
    assert candidate["review"]["auto_promote_allowed"] is False
    assert candidate["tags"] == ["confirmed"]
    source = store.read("sources", "source-one")
    assert source["processing_state"] == "reference_only"
    assert source["trust_status"] == "imported_unverified"
    assert source["metadata"]["raw_content_restored"] is False
    assert source["occurred_at"] == "2026-07-24T00:00:00Z"
    assert source["recorded_at"] == "2026-07-24T00:00:00Z"
    assert source["metadata"]["source_temporal"]["exported_recorded_at"] == "2026-07-24T00:00:00Z"
    schema = json.loads(
        (Path(__file__).parents[2] / "core-contracts" / "rebuild" / "source.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(source)
    reopened = JsonObjectStore(tmp_path / "object-store")
    assert reopened.read("sources", "source-one")["processing_state"] == "reference_only"

    local = dict(candidate)
    local["content"] = "本地修改必须保留"
    store.write("memory_candidates", "memory-one", local, expected_revision=None)
    conflict = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="batch-three",
        created_at="2026-07-24T00:02:00Z",
    )

    assert conflict.conflict_count == 1
    assert store.read("memory_candidates", "memory-one")["content"] == "本地修改必须保留"
    conflict_record = store.read(
        "memory_candidate_conflicts",
        conflict.conflict_ids[0],
    )
    assert conflict_record["incoming"]["content"] == "用户确认的事实"
    assert conflict_record["status"] == "needs_review"


def test_asset_package_preserves_explicit_temporal_pair_and_reads_legacy_time(
    tmp_path: Path,
) -> None:
    memory = {
        **_memory(),
        "occurred_at": "2026-07-20T10:00:00Z",
        "recorded_at": "2026-07-21T10:00:00Z",
    }
    package = parse_memory_asset_package(_package({"L1": [memory]}, version="1.2"))
    assert package.memories[0]["occurred_at"] == "2026-07-20T10:00:00Z"
    assert package.memories[0]["recorded_at"] == "2026-07-21T10:00:00Z"

    store = JsonObjectStore(tmp_path / "object-store")
    import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="temporal-pair",
        created_at="2026-07-24T00:00:00Z",
    )
    candidate = store.read("memory_candidates", "memory-one")
    assert candidate["occurred_at"] == "2026-07-20T10:00:00Z"
    assert candidate["recorded_at"] == "2026-07-24T00:00:00Z"
    source = store.read("sources", "source-one")
    assert source["occurred_at"] == "2026-07-24T00:00:00Z"
    assert source["recorded_at"] == "2026-07-24T00:00:00Z"

    legacy = parse_memory_asset_package(_package({"L1": [_memory()]}, version="1.0"))
    assert legacy.memories[0]["occurred_at"] is None
    assert legacy.memories[0]["recorded_at"] == ""


def test_asset_package_rejects_invalid_explicit_timestamp() -> None:
    memory = {**_memory(), "occurred_at": "not-a-time", "recorded_at": "2026-07-21T10:00:00Z"}
    with pytest.raises(MemoryAssetPackageImportError, match="RFC 3339"):
        parse_memory_asset_package(_package({"L1": [memory]}, version="1.2"))


def test_import_preserves_l1_l2_l3_project_and_hierarchy_identity(
    tmp_path: Path,
) -> None:
    atom = {
        **_memory("atom-portable"),
        "project_id": "project-portable",
    }
    scenario = {
        **_memory("scenario-portable", layer="L2"),
        "type": "scenario",
        "project_id": "project-portable",
        "series_id": "series-portable",
        "atom_ids": ["atom-portable"],
    }
    series = {
        **_memory("series-portable", layer="L3"),
        "type": "series_memory",
        "project_id": "project-portable",
        "series_id": "series-portable",
        "scenario_ids": ["scenario-portable"],
    }
    store = JsonObjectStore(tmp_path / "object-store")
    package = parse_memory_asset_package(
        _package(
            {"L1": [atom], "L2": [scenario], "L3": [series]},
            version="1.2",
        )
    )

    result = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="batch-hierarchy",
        created_at="2026-07-27T00:00:00Z",
    )

    assert result.imported_count == 3
    imported_atom = store.read("memory_candidates", "atom-portable")
    imported_scenario = store.read("memory_candidates", "scenario-portable")
    imported_series = store.read("memory_candidates", "series-portable")
    assert imported_atom["project_id"] == "project-portable"
    assert imported_atom["portable_object_id"] == "atom-portable"
    assert imported_scenario["target_layer"] == "scenario"
    assert imported_scenario["series_id"] == "series-portable"
    assert imported_scenario["atom_ids"] == ["atom-portable"]
    assert imported_series["target_layer"] == "series_memory"
    assert imported_series["series_id"] == "series-portable"
    assert imported_series["scenario_ids"] == ["scenario-portable"]
    assert all(
        candidate["status"] == "pending_review"
        for candidate in (imported_atom, imported_scenario, imported_series)
    )


def test_source_roundtrip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = JsonObjectStore(tmp_path / "object-store")
    package = parse_memory_asset_package(_package({"L1": [_memory()]}))

    first = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="source-first",
        created_at="2026-07-24T00:00:00Z",
    )
    repeated = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="source-repeat",
        created_at="2026-07-24T00:01:00Z",
    )

    assert first.source_imported_count == 1
    assert repeated.source_skipped_count == 1
    local = dict(store.read("sources", "source-one") or {})
    local["title"] = "本地来源标题"
    store.write(
        "sources",
        "source-one",
        local,
        expected_revision=store.revision("sources", "source-one"),
    )

    conflict = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="source-conflict",
        created_at="2026-07-24T00:02:00Z",
    )

    assert conflict.source_conflict_count == 1
    assert store.read("sources", "source-one")["title"] == "本地来源标题"
    conflict_record = store.read(
        "source_import_conflicts",
        conflict.source_conflict_ids[0],
    )
    assert conflict_record["incoming"]["title"] == "导入来源"


def test_missing_referenced_source_rejects_before_any_write(tmp_path: Path) -> None:
    payload = _package({"L1": [_memory()]})
    with zipfile.ZipFile(io.BytesIO(payload), "r") as source_archive:
        entries = {
            info.filename: source_archive.read(info.filename)
            for info in source_archive.infolist()
            if info.filename != "sources/source_manifest.ndjson"
        }
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
        archive.writestr("sources/source_manifest.ndjson", "")

    with pytest.raises(MemoryAssetPackageImportError, match="missing source source-one"):
        parse_memory_asset_package(rebuilt.getvalue())

    store = JsonObjectStore(tmp_path / "object-store")
    assert list(store.list("sources")) == []
    assert list(store.list("memory_candidates")) == []


def test_v11_l4_persona_import_uses_modern_review_contract(tmp_path: Path) -> None:
    persona_memory = _memory("persona-one", layer="L4")
    persona_memory["type"] = "preference"
    payload = _package(
        {"L4": [persona_memory]},
        version="1.1",
    )

    package = parse_memory_asset_package(payload)
    store = JsonObjectStore(tmp_path / "object-store")
    result = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="batch-l4",
        created_at="2026-07-27T12:00:00Z",
    )

    assert package.memories[0]["layer"] == "L4"
    assert package.memories[0]["memory_id"] == "persona-one"
    assert result.imported_count == 1
    candidate = store.read("memory_candidates", "persona-one")
    assert candidate["target_layer"] == "persona"
    assert candidate["proposed_content"] == "用户确认的事实"
    assert candidate["source_id"] == "source-one"
    assert candidate["source_refs"] == [
        {"source_id": "source-one", "locator": "char:0-8"}
    ]
    assert store.read("sources", "source-one") is not None
    repository = ObjectStorePersonaRepository(store)
    draft = StageExternalPersonaCandidate(
        repository,
        now="2026-07-27T12:01:00Z",
    ).execute(
        candidate,
        scope="project",
        expected_draft_revision=0,
        expected_current_revision=0,
    )
    assert draft["confirmation"]["status"] == "pending"
    assert draft["evidence_refs"][0]["object_id"] == "source-one"
    assert repository.get("project") is None


def test_v12_original_asset_roundtrip_restores_verified_file_idempotently(
    tmp_path: Path,
) -> None:
    original = "原始文件正文。".encode()
    package = parse_memory_asset_package(
        _package({"L1": [_memory()]}, version="1.2", original_content=original)
    )
    store = JsonObjectStore(tmp_path / "object-store")
    library_root = tmp_path / "library"

    first = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="asset-first",
        created_at="2026-07-27T12:00:00Z",
        library_root=library_root,
    )
    repeated = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="asset-repeat",
        created_at="2026-07-27T12:01:00Z",
        library_root=library_root,
    )

    assert first.asset_imported_count == 1
    assert repeated.asset_skipped_count == 1
    source = store.read("sources", "source-one")
    assert source["processing_state"] == "ready"
    assert source["metadata"]["raw_content_restored"] is True
    asset = tuple(store.list("workbench_original_assets"))[0]
    restored = library_root / str(asset["vault_ref"])
    assert restored.read_bytes() == original
    assert tuple(store.list("source_asset_links"))[0]["source_id"] == "source-one"
    availability = ResolveWorkbenchOriginalAsset(
        object_store=store,
        library_root=library_root,
    ).for_source("source-one")
    assert availability is not None
    assert availability.status == "available"


def test_v12_original_asset_roundtrip_uses_sqlite_authority_without_json_write(
    tmp_path: Path,
) -> None:
    original = b"sqlite package original"
    package = parse_memory_asset_package(
        _package({"L1": [_memory()]}, version="1.2", original_content=original)
    )
    json_store = JsonObjectStore(tmp_path / "object-store")
    records = SQLiteStructuredRecordStore(tmp_path / "structured.sqlite3")
    store = SourceAssetRuntimeStore(
        json_store=json_store,
        sqlite_records=records,
        library_root=tmp_path / "library",
        authority_identity="sqlite:structured-records-v1",
    )

    result = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="asset-sqlite",
        created_at="2026-07-29T12:00:00Z",
        library_root=tmp_path / "library",
    )

    assert result.asset_imported_count == 1
    assert json_store.list("workbench_original_assets") == ()
    assert json_store.list("source_asset_links") == ()
    assert len(records.list("asset_blobs")) == 1
    assert len(records.list("original_assets")) == 1
    assert len(records.list("source_asset_links")) == 1
    asset = store.list("workbench_original_assets")[0]
    assert str(asset["vault_ref"]).startswith("assets/blobs/")
    assert (tmp_path / "library" / str(asset["vault_ref"])).read_bytes() == original


def test_v12_rejects_tampered_or_undeclared_original_before_writes(tmp_path: Path) -> None:
    payload = _package(
        {"L1": [_memory()]},
        version="1.2",
        original_content=b"original",
    )
    with zipfile.ZipFile(io.BytesIO(payload), "r") as source:
        entries = {info.filename: source.read(info.filename) for info in source.infolist()}
    blob_path = next(path for path in entries if path.startswith("sources/content/"))
    entries[blob_path] = b"tampered"
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)

    with pytest.raises(MemoryAssetPackageImportError, match="blob size|blob hash"):
        parse_memory_asset_package(rebuilt.getvalue())
    assert list(JsonObjectStore(tmp_path / "object-store").list("sources")) == []


def test_v12_source_conflict_blocks_original_asset_write(tmp_path: Path) -> None:
    package = parse_memory_asset_package(
        _package(
            {"L1": [_memory()]},
            version="1.2",
            original_content=b"incoming-original",
        )
    )
    store = JsonObjectStore(tmp_path / "object-store")
    store.write("sources", "source-one", {
        "id": "source-one",
        "type": "text",
        "title": "本地不同来源",
        "storage_uri": "crp://default/sources/source-one",
        "media_type": "text/plain",
        "processing_state": "ready",
        "trust_status": "user_confirmed",
        "metadata": {},
    }, expected_revision=0)

    result = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="asset-source-conflict",
        created_at="2026-07-27T12:00:00Z",
        library_root=tmp_path / "library",
    )

    assert result.source_conflict_count == 1
    assert result.asset_failed_count == 1
    assert list(store.list("workbench_original_assets")) == []
    assert not (tmp_path / "library" / "assets" / "originals").exists()


def test_v12_metadata_failure_rolls_back_new_asset_records_and_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = parse_memory_asset_package(
        _package(
            {"L1": [_memory()]},
            version="1.2",
            original_content=b"rollback-original",
        )
    )
    store = JsonObjectStore(tmp_path / "object-store")
    original_write = JsonObjectStore.write

    def fail_link(self, collection, object_id, payload, *, expected_revision):
        if collection == "source_asset_links":
            raise OSError("injected link failure")
        return original_write(
            self,
            collection,
            object_id,
            payload,
            expected_revision=expected_revision,
        )

    monkeypatch.setattr(JsonObjectStore, "write", fail_link)
    result = import_memory_asset_package(
        store=store,
        package=package,
        import_batch_id="asset-rollback",
        created_at="2026-07-27T12:00:00Z",
        library_root=tmp_path / "library",
    )

    assert result.asset_failed_count == 1
    assert list(store.list("workbench_original_assets")) == []
    assert list(store.list("source_asset_links")) == []
    assert list((tmp_path / "library").rglob("original-file-*")) == []
    source = store.read("sources", "source-one")
    assert source["metadata"]["raw_content_restored"] is False
