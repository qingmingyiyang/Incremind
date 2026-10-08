from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.api.context_graph_import_composition import (
    ContextGraphImportRequest,
    ContextGraphImportService,
    ContextGraphFileSelection,
    SQLiteContextGraphImportEvidenceRepository,
)
from backend.api.context_graph_snapshot_runtime import ContextGraphSnapshotRepository
from core.capability_packages.thought_graph_context import MarkdownGraphImporter, ThoughtDAGImporter
from core.context_graph import ContextGraphImportRegistration, ImportLimits
from core.storage_provider import SQLiteStructuredRecordStore


class _Registry:
    def __init__(self, registrations: dict[str, ContextGraphImportRegistration]) -> None:
        self._registrations = registrations

    def resolve(self, source_type: str) -> ContextGraphImportRegistration | None:
        return self._registrations.get(source_type)


class _Selections:
    def __init__(self) -> None:
        self.values: dict[str, ContextGraphFileSelection] = {}
        self.source_types: dict[str, str] = {}
        self.consume_calls = 0

    def issue(self, path: Path, *, project_id: str = "project-a", selection_id: str = "selection-1", source_type: str = "thoughtdag") -> str:
        stat = path.stat()
        self.values[selection_id] = ContextGraphFileSelection(
            selection_id, project_id, path, f"mtime-{stat.st_mtime_ns}:size-{stat.st_size}", f"selection-evidence:{selection_id}",
        )
        self.source_types[selection_id] = source_type
        return selection_id

    def consume(self, selection_id: str, *, project_id: str, source_type: str, actor_id: str, session_instance_id: str) -> ContextGraphFileSelection | None:
        self.consume_calls += 1
        value = self.values.get(selection_id)
        if value is None or self.source_types.get(selection_id) != source_type or value.project_id != project_id or actor_id != "desktop-user" or session_instance_id != "session-a":
            return None
        self.values.pop(selection_id)
        self.source_types.pop(selection_id)
        return value


def _service(tmp_path: Path) -> tuple[ContextGraphImportService, SQLiteContextGraphImportEvidenceRepository, _Selections]:
    records = SQLiteStructuredRecordStore(tmp_path / "context-graphs.sqlite3")
    registry = _Registry({
        "thoughtdag": ContextGraphImportRegistration(
            "thoughtdag", "thought_graph_context.ThoughtDAGImporter", "thought_graph_context", "4.0.0", ThoughtDAGImporter(),
        ),
        "markdown": ContextGraphImportRegistration(
            "markdown", "thought_graph_context.MarkdownGraphImporter", "thought_graph_context", "4.0.0", MarkdownGraphImporter(),
        ),
    })
    evidence = SQLiteContextGraphImportEvidenceRepository(records)
    selections = _Selections()
    return ContextGraphImportService(
        ContextGraphSnapshotRepository(records), registry, evidence, selections,
        limits=ImportLimits(), clock=lambda: "2026-08-30T01:00:00Z",
    ), evidence, selections


def _request(selection_id: str, *, source_type: str, command_id: str = "command-1", predecessor: str | None = None, confirm: bool = True, project: str = "project-a") -> ContextGraphImportRequest:
    return ContextGraphImportRequest(
        project_id=project,
        source_type=source_type,
        command_id=command_id,
        selection_id=selection_id,
        actor_id="desktop-user",
        session_instance_id="session-a",
        confirm_read=confirm,
        expected_predecessor=predecessor,
    )


def _thoughtdag(path: Path, *, name: str = "fixture") -> Path:
    path.write_text(json.dumps({
        "version": 1, "name": name, "exportedAt": "2026-08-30T00:00:00Z",
        "nodes": [{
            "id": "n1", "type": "thought", "position": {"x": 0, "y": 0},
            "data": {"question": "Question", "response": "Answer"},
        }], "edges": [], "events": [],
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("source_type", "filename", "content", "expected_source"),
    (
        ("thoughtdag", "graph.thoughtdag.json", None, "thoughtdag"),
        ("markdown", "graph.md", "# Root\nMaterial\n## Conclusion\nDecision", "markdown"),
    ),
)
def test_authorized_source_type_import_persists_snapshot_and_read_only_preview(
    tmp_path: Path, source_type: str, filename: str, content: str | None, expected_source: str,
) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = tmp_path / filename
    if content is None:
        _thoughtdag(source)
    else:
        source.write_text(content, encoding="utf-8")
    before = source.read_bytes()

    result = service.import_file(_request(selections.issue(source, source_type=source_type), source_type=source_type))

    assert result.ok
    assert result.record is not None and result.preview is not None and result.evidence is not None
    assert result.record.snapshot.source_type == expected_source
    assert result.record.permission_evidence_refs == (result.evidence.evidence_ref,)
    assert result.preview.evidence_ref == result.evidence.evidence_ref
    assert evidence_repo.read(result.evidence.evidence_ref) == result.evidence
    assert result.evidence.command_id == "command-1"
    assert result.evidence.selection_id == "selection-1"
    assert result.evidence.authorized_file_revision.startswith("mtime-")
    assert result.evidence.imported_at == "2026-08-30T01:00:00Z"
    assert source.read_bytes() == before


def test_confirmation_and_authorized_path_scope_fail_before_import(tmp_path: Path) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")

    unconfirmed = service.import_file(_request(selections.issue(source), source_type="thoughtdag", confirm=False))
    assert unconfirmed.error_code == "import_request_invalid"
    assert evidence_repo.read("context-import-evidence-1") is None

    selection_id = selections.issue(source, selection_id="selection-outside")
    selections.values[selection_id] = ContextGraphFileSelection(
        selection_id, "project-a", tmp_path / "not-selected.thoughtdag.json", "mtime-0:size-0", "selection-evidence:outside",
    )
    outside = _request(selection_id, source_type="thoughtdag")
    rejected = service.import_file(outside)
    assert rejected.error_code == "file_authorization_rejected"
    assert evidence_repo.read("context-import-evidence-1") is None


def test_request_carries_only_opaque_selection_and_project_drift_is_rejected(tmp_path: Path) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    selection_id = selections.issue(source, project_id="project-b")
    request = _request(selection_id, source_type="thoughtdag", project="project-a")

    assert set(ContextGraphImportRequest.__dataclass_fields__) == {
        "project_id", "source_type", "selection_id", "actor_id", "confirm_read",
        "session_instance_id", "command_id", "expected_predecessor",
    }
    result = service.import_file(request)
    assert result.error_code == "import_request_invalid"
    assert evidence_repo.read("context-import-evidence-1") is None


def test_wrong_source_type_does_not_consume_the_core_selection(tmp_path: Path) -> None:
    service, _, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    selection_id = selections.issue(source)

    wrong = service.import_file(_request(selection_id, source_type="markdown"))
    assert wrong.error_code == "import_request_invalid"
    assert selection_id in selections.values

    accepted = service.import_file(_request(selection_id, source_type="thoughtdag"))
    assert accepted.ok


def test_constructor_rejects_a_non_callable_clock(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "context-graphs.sqlite3")
    with pytest.raises(ValueError, match="context_graph_import_clock_invalid"):
        ContextGraphImportService(
            ContextGraphSnapshotRepository(records), _Registry({}),
            SQLiteContextGraphImportEvidenceRepository(records), _Selections(),
            limits=ImportLimits(), clock="not-a-clock",  # type: ignore[arg-type]
        )


def test_source_revision_drift_is_rejected_and_never_persists_snapshot(tmp_path: Path, monkeypatch) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    importer = service._registry.resolve("thoughtdag").importer
    original = importer.import_authorized_file

    def mutate_after_grant(**kwargs):
        source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return original(**kwargs)

    monkeypatch.setattr(importer, "import_authorized_file", mutate_after_grant)
    result = service.import_file(_request(selections.issue(source), source_type="thoughtdag"))

    assert result.error_code == "importer_rejected"
    assert evidence_repo.read("context-import-evidence-1") is None


def test_selection_revision_must_match_the_exact_file_revision_before_read(tmp_path: Path) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    selection_id = selections.issue(source)
    source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = service.import_file(_request(selection_id, source_type="thoughtdag"))

    assert result.error_code == "import_request_invalid"
    assert evidence_repo.read("context-import-evidence-1") is None


def test_completed_command_retries_without_consuming_selection_and_identity_drift_fails_closed(tmp_path: Path) -> None:
    service, _, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    first = service.import_file(_request(selections.issue(source), source_type="thoughtdag"))
    assert first.ok and first.record is not None and first.evidence is not None

    retry = service.import_file(_request("selection-1", source_type="thoughtdag"))
    assert retry.ok and retry.record == first.record and retry.evidence == first.evidence

    drift = service.import_file(_request("selection-1", source_type="markdown"))
    assert drift.error_code == "import_request_invalid"


def test_cas_conflict_keeps_authorization_evidence_but_never_replaces_current_snapshot(tmp_path: Path) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    first = service.import_file(_request(selections.issue(source), source_type="thoughtdag"))
    assert first.ok and first.record is not None

    source.write_text(source.read_text(encoding="utf-8").replace("Answer", "Revised answer"), encoding="utf-8")
    conflict = service.import_file(_request(selections.issue(source, selection_id="selection-2"), source_type="thoughtdag", command_id="command-2", predecessor=None))

    assert conflict.error_code == "snapshot_conflict"
    assert evidence_repo.read("context-import-evidence-2") is not None
    assert service._snapshots.current("project-a", first.record.graph_id) == first.record

    retry = service.import_file(_request(
        "selection-2", source_type="thoughtdag", command_id="command-2", predecessor=None,
    ))
    assert retry.error_code == "snapshot_conflict"
    assert retry.error_detail == "authorized_read_not_committed"
    assert selections.consume_calls == 2


def test_registry_and_snapshot_authority_drift_fail_closed_without_evidence(tmp_path: Path) -> None:
    service, evidence_repo, selections = _service(tmp_path)
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")
    missing = service.import_file(_request(selections.issue(source), source_type="unsupported"))
    assert missing.error_code == "importer_unavailable"

    registration = service._registry.resolve("thoughtdag")
    service._registry._registrations["wrong-type"] = registration
    type_drift = service.import_file(_request(selections.issue(source, selection_id="selection-2"), source_type="wrong-type", command_id="command-2"))
    assert type_drift.error_code == "import_request_invalid"
    assert evidence_repo.read("context-import-evidence-1") is None


def test_core_fixed_import_limits_reject_before_evidence_or_snapshot(tmp_path: Path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "context-graphs.sqlite3")
    selections = _Selections()
    service = ContextGraphImportService(
        ContextGraphSnapshotRepository(records),
        _Registry({"thoughtdag": ContextGraphImportRegistration("thoughtdag", "thought_graph_context.ThoughtDAGImporter", "thought_graph_context", "4.0.0", ThoughtDAGImporter())}),
        SQLiteContextGraphImportEvidenceRepository(records), selections,
        limits=ImportLimits(max_nodes=0), clock=lambda: "2026-08-30T01:00:00Z",
    )
    source = _thoughtdag(tmp_path / "graph.thoughtdag.json")

    result = service.import_file(_request(selections.issue(source), source_type="thoughtdag"))

    assert result.error_code == "importer_rejected"
