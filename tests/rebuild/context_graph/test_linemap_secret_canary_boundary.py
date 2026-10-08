from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from backend.api.context_binding_runtime import ContextBindingRegistry
from core.ai_kernel import SQLiteAITurnStore
from core.capability_packages.thought_graph_context import ThoughtDAGImporter
from core.context_graph import ContextGraphValidationError, ImportLimits
from core.context_graph.protocols import issue_authorized_context_file


def _authorized_canvas(canary: str) -> dict[str, object]:
    return {
        "version": 1,
        "name": "authorized-input",
        "exportedAt": "2026-08-30T00:00:00Z",
        "nodes": [{
            "id": "n1",
            "type": "thought",
            "position": {"x": 0, "y": 0},
            "data": {
                "question": "Untrusted external material",
                "response": canary,
                "createdAt": "2026-08-30T00:00:00Z",
            },
        }],
        "edges": [],
        "events": [],
    }


def _table_count(database: Path, table: str) -> int:
    with sqlite3.connect(database) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _runtime_files(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in root.rglob("*") if path.is_file())


def test_secret_canary_external_input_fails_closed_before_any_runtime_artifact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # Construct the non-secret sentinel at runtime so the only complete value is
    # the explicitly authorized external input file, never this test source.
    canary = "".join(("LM", "-CANARY", "-INPUT", "-ONLY", "-20260830"))
    authorized_input = tmp_path / "authorized-input"
    authorized_input.mkdir()
    source = authorized_input / "untrusted.thoughtdag.json"
    source.write_text(json.dumps(_authorized_canvas(canary)), encoding="utf-8")

    runtime_root = tmp_path / "runtime-output"
    binding_root = runtime_root / "bindings"
    turn_database = runtime_root / "turns.sqlite3"
    bindings = ContextBindingRegistry(binding_root)
    turns = SQLiteAITurnStore(turn_database)
    effect_database = Path(turns.effect_runner.log.database)
    export_root = runtime_root / "exports"

    # These are the real durable roots LineMap would otherwise use.  The rejected
    # import must not create a ContextBinding, Turn, Effect, Receipt, or export.
    assert bindings is not None
    assert turn_database.is_file()
    assert effect_database.is_file()
    assert not export_root.exists()

    caplog.set_level(logging.DEBUG)
    with pytest.raises(ContextGraphValidationError) as raised:
        ThoughtDAGImporter().import_authorized_file(
            grant=issue_authorized_context_file(
                source,
                project_id="project-secret-canary",
                importer_ids=(ThoughtDAGImporter.importer_id,),
                allowed_paths=(source,),
            ),
            limits=ImportLimits(secret_canaries=(canary,)),
        )

    assert "secret_canary_detected" in raised.value.issues
    assert canary not in str(raised.value)
    assert canary not in caplog.text

    binding_objects = binding_root / ".rebuild-data" / "objects"
    assert not binding_objects.exists() or not _runtime_files(binding_objects)
    for table in (
        "ai_turns",
        "ai_turn_events",
        "ai_turn_payloads",
        "ai_turn_immutable_payloads",
    ):
        assert _table_count(turn_database, table) == 0
    for table in ("effect", "effect_receipt", "effect_gate_fact", "effect_intent_fact"):
        assert _table_count(effect_database, table) == 0
    assert not export_root.exists()

    # Search only runtime/output roots.  The input file is deliberately excluded:
    # it is the sole authorized location that contains this non-secret sentinel.
    for artifact in _runtime_files(runtime_root):
        assert canary.encode("utf-8") not in artifact.read_bytes()
