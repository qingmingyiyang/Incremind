from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.product_core.progressive_recall_authority_reader import (
    ObjectStoreProgressiveRecallAuthorityReader,
)
from core.product_core.progressive_recall_drilldown import ProgressiveRecallDrilldownError
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "core-contracts"
    / "rebuild"
    / "source.schema.json"
)


def _schema() -> dict[str, object]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _source() -> dict[str, object]:
    return {
        "schema_version": "1.2.0",
        "id": "source-confirmed-1",
        "type": "text",
        "title": "Confirmed text",
        "capture_mode": "inline",
        "storage_uri": "crp://default/sources/source-confirmed-1",
        "original_url": None,
        "content_hash": None,
        "identity_method": "workspace_confirmation",
        "source_revision": 1,
        "confirmation_operation_id": "confirm-workspace-1",
        "workspace_item_id": "workspace-1",
        "media_type": "text/plain",
        "size_bytes": 4,
        "parser_version": None,
        "processing_state": "ready",
        "created_at": "2026-09-24T00:00:00Z",
        "occurred_at": None,
        "recorded_at": "2026-09-24T00:00:00Z",
        "project_id": "project-1",
        "imported_from_legacy": False,
        "trust_status": "user_confirmed",
        "metadata": {"content": "text"},
    }


def test_workspace_confirmation_source_has_explicit_revision_identity() -> None:
    assert validate_contract_instance("source.schema.json", _schema(), _source()) == []


@pytest.mark.parametrize(
    "changes",
    (
        {"content_hash": "a" * 64},
        {"source_revision": 0},
        {"source_revision": True},
        {"confirmation_operation_id": ""},
        {"workspace_item_id": ""},
        {"schema_version": "1.1.0"},
    ),
)
def test_workspace_confirmation_rejects_incomplete_or_fake_identity(changes) -> None:
    source = {**_source(), **changes}
    assert validate_contract_instance("source.schema.json", _schema(), source)


def test_existing_sha_source_contract_remains_valid() -> None:
    source = _source()
    source["schema_version"] = "1.1.0"
    source["content_hash"] = "a" * 64
    for field in (
        "identity_method",
        "source_revision",
        "confirmation_operation_id",
        "workspace_item_id",
    ):
        source.pop(field)
    assert validate_contract_instance("source.schema.json", _schema(), source) == []


def test_progressive_recall_reports_unsupported_revision_evidence(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "vault")
    source = _source()
    store.write("sources", source["id"], source, expected_revision=None)
    reader = ObjectStoreProgressiveRecallAuthorityReader(store)

    with pytest.raises(
        ProgressiveRecallDrilldownError,
        match="source_revision_evidence_unsupported",
    ):
        reader.read_source_evidence(
            project_id="project-1",
            source_refs=(),
            allowed_source_refs=((source["id"], "section:one"),),
            query="text",
        )

    store.write(
        "documents",
        "document-1",
        {
            "id": "document-1",
            "project_id": "project-1",
            "status": "published",
            "revision": 1,
            "content_hash": "a" * 64,
            "blocks": [
                {
                    "id": "block-1",
                    "content": "structured text",
                    "source_refs": [
                        {"source_id": source["id"], "locator": "section:one"}
                    ],
                }
            ],
        },
        expected_revision=None,
    )
    with pytest.raises(
        ProgressiveRecallDrilldownError,
        match="source_revision_evidence_unsupported",
    ):
        reader.read_structured(
            project_id="project-1",
            series_ids=(),
            allowed_source_refs=((source["id"], "section:one"),),
            query="text",
        )
