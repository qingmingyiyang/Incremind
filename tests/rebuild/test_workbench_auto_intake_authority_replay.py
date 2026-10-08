from __future__ import annotations

from collections.abc import Mapping

import pytest

from core.product_core.workbench_auto_intake import (
    OrchestrateWorkbenchAutoIntake,
    _frozen_source_snapshot_for_asset,
    _replay_media_capture_result,
)


class _MemoryStore:
    def __init__(self, collections: dict[str, dict[str, dict[str, object]]]) -> None:
        self._collections = collections
        self._revisions = {
            (collection, item_id): 1
            for collection, items in collections.items()
            for item_id in items
        }

    def list(self, collection: str):
        return tuple(dict(item) for item in self._collections.get(collection, {}).values())

    def read(self, collection: str, item_id: str):
        item = self._collections.get(collection, {}).get(item_id)
        return dict(item) if item is not None else None

    def revision(self, collection: str, item_id: str):
        return self._revisions.get((collection, item_id))

    def write(self, collection: str, item_id: str, item: Mapping[str, object], *, expected_revision: int | None):
        current = self.revision(collection, item_id)
        assert current == expected_revision
        self._collections.setdefault(collection, {})[item_id] = dict(item)
        self._revisions[(collection, item_id)] = current + 1


def _source(*, size_bytes: int, authorization: bool) -> dict[str, object]:
    metadata: dict[str, object] = {
        "file_reference": "asset-synthetic-docx",
        "display_name": "synthetic source",
    }
    if authorization:
        metadata["document_authorization"] = {
            "status": "authorized",
            "authorization_id": "authorized-document-source-file-synthetic",
            "authorization_ref": "crp://default/authorized-documents/authorized-document-source-file-synthetic.json",
            "file_reference": "asset-synthetic-docx",
            "media_type": "application/vnd.test.document",
        }
    return {
        "id": "source-file-synthetic", "type": "file", "project_id": "default",
        "title": "synthetic source",
        "storage_uri": "crp://default/sources/source-file-synthetic",
        "content_hash": "synthetic-source-identity", "media_type": "application/vnd.test.document",
        "size_bytes": size_bytes, "capture_mode": "reference", "metadata": metadata,
    }


def _authorization_record() -> dict[str, object]:
    return {
        "id": "authorized-document-source-file-synthetic",
        "source_id": "source-file-synthetic",
        "status": "authorized",
        "file_reference": "asset-synthetic-docx",
        "media_type": "application/vnd.test.document",
    }


@pytest.mark.parametrize("authorization_key", ["document_authorization", "file_authorization"])
def test_replay_reuses_the_exact_preexisting_source_without_overwriting_frozen_authority(authorization_key) -> None:
    prior = _source(size_bytes=17, authorization=True)
    prior["metadata"][authorization_key] = prior["metadata"].pop("document_authorization")
    store = _MemoryStore({
        "sources": {"source-file-synthetic": prior},
        "authorized_file_refs": {"authorized-document-source-file-synthetic": _authorization_record()},
        "workbench_original_assets": {
            "original-asset-synthetic": {
                "id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx",
                "storage_mode": "stored_original", "availability": "available",
            },
        },
        "source_asset_links": {
            "source-asset-synthetic": {
                "id": "source-asset-synthetic", "source_id": "source-file-synthetic",
                "source_uri": "crp://default/sources/source-file-synthetic",
                "asset_id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx", "role": "original",
            },
        },
    })
    snapshot = _frozen_source_snapshot_for_asset(
        store, "asset-synthetic-docx", expected_source_type="file",
        expected_media_type="application/vnd.test.document", expected_title="synthetic source",
        expected_display_name="synthetic source", project_id="default",
    )
    assert snapshot is not None
    assert snapshot.authorization_ready is True
    replay = _replay_media_capture_result(snapshot)
    current = store.read("sources", "source-file-synthetic")
    assert current == prior
    assert store.revision("sources", "source-file-synthetic") == 1
    assert replay.source_id == "source-file-synthetic"
    assert current["metadata"][authorization_key]["authorization_id"] == (
        "authorized-document-source-file-synthetic"
    )


def test_file_replay_skips_the_preparation_hook_after_reusing_frozen_source() -> None:
    prior = _source(size_bytes=17, authorization=True)
    store = _MemoryStore({
        "sources": {"source-file-synthetic": prior},
        "authorized_file_refs": {"authorized-document-source-file-synthetic": _authorization_record()},
        "workbench_original_assets": {
            "original-asset-synthetic": {
                "id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx",
            },
        },
        "source_asset_links": {
            "source-asset-synthetic": {
                "id": "source-asset-synthetic", "source_id": "source-file-synthetic",
                "source_uri": "crp://default/sources/source-file-synthetic",
                "asset_id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx", "role": "original",
            },
        },
    })
    prepared: list[tuple[str, str]] = []
    intake = OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=object(),
        job_repository=object(),
        fetch_url=lambda _url: "",
        prepare_file_source=lambda source_id, asset_ref: prepared.append((source_id, asset_ref)),
    )

    item = intake._capture_file_source(
        title="synthetic source",
        display_name="synthetic source",
        media_type="application/vnd.test.document",
        input_type="file",
        workflow="document_text_extraction",
        original_asset_ref="asset-synthetic-docx",
    )

    assert item.source_id == "source-file-synthetic"
    assert item.auto_organization["replayed_frozen_source"] is True
    assert prepared == []
    assert store.revision("sources", "source-file-synthetic") == 1


def test_file_replay_retries_preparation_when_authorization_is_missing_without_recapturing() -> None:
    prior = _source(size_bytes=17, authorization=False)
    store = _MemoryStore({
        "sources": {"source-file-synthetic": prior},
        "workbench_original_assets": {
            "original-asset-synthetic": {
                "id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx",
            },
        },
        "source_asset_links": {
            "source-asset-synthetic": {
                "id": "source-asset-synthetic", "source_id": "source-file-synthetic",
                "source_uri": "crp://default/sources/source-file-synthetic",
                "asset_id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx", "role": "original",
            },
        },
    })
    prepared: list[tuple[str, str]] = []
    intake = OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=object(),
        job_repository=object(),
        fetch_url=lambda _url: "",
        prepare_file_source=lambda source_id, asset_ref: prepared.append((source_id, asset_ref)),
    )

    item = intake._capture_file_source(
        title="synthetic source",
        display_name="synthetic source",
        media_type="application/vnd.test.document",
        input_type="file",
        workflow="document_text_extraction",
        original_asset_ref="asset-synthetic-docx",
    )

    assert item.source_id == "source-file-synthetic"
    assert item.auto_organization["replayed_frozen_source"] is True
    assert item.auto_organization["frozen_source_authorization_ready"] is False
    assert prepared == [("source-file-synthetic", "asset-synthetic-docx")]
    assert store.revision("sources", "source-file-synthetic") == 1


def test_replay_rejects_a_linked_source_with_a_different_media_type() -> None:
    prior = _source(size_bytes=17, authorization=True)
    store = _MemoryStore({
        "sources": {"source-file-synthetic": prior},
        "workbench_original_assets": {
            "original-asset-synthetic": {
                "id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx",
            },
        },
        "source_asset_links": {
            "source-asset-synthetic": {
                "id": "source-asset-synthetic", "source_id": "source-file-synthetic",
                "source_uri": "crp://default/sources/source-file-synthetic",
                "asset_id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx", "role": "original",
            },
        },
    })
    with pytest.raises(ValueError, match="incompatible"):
        _frozen_source_snapshot_for_asset(
            store, "asset-synthetic-docx", expected_source_type="file", expected_media_type="image/png",
            expected_title="synthetic source", expected_display_name="synthetic source", project_id="default",
        )
    assert store.read("sources", "source-file-synthetic") == prior


def test_replay_rejects_ambiguous_exact_source_asset_relations() -> None:
    first = _source(size_bytes=17, authorization=True)
    second = dict(
        first,
        id="source-file-synthetic-second",
        storage_uri="crp://default/sources/source-file-synthetic-second",
    )
    store = _MemoryStore({
        "sources": {first["id"]: first, second["id"]: second},
        "workbench_original_assets": {
            "original-asset-synthetic": {
                "id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx",
            },
        },
        "source_asset_links": {
            "source-asset-first": {
                "id": "source-asset-first", "source_id": first["id"], "source_uri": first["storage_uri"],
                "asset_id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx", "role": "original",
            },
            "source-asset-second": {
                "id": "source-asset-second", "source_id": second["id"], "source_uri": second["storage_uri"],
                "asset_id": "original-asset-synthetic", "asset_ref": "asset-synthetic-docx", "role": "original",
            },
        },
    })

    with pytest.raises(ValueError, match="ambiguous"):
        _frozen_source_snapshot_for_asset(
            store, "asset-synthetic-docx", expected_source_type="file",
            expected_media_type="application/vnd.test.document", expected_title="synthetic source",
            expected_display_name="synthetic source", project_id="default",
        )
