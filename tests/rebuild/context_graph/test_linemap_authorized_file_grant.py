from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.capability_packages.thought_graph_context import MarkdownGraphImporter, ThoughtDAGImporter
from core.context_graph import (
    AuthorizedContextFile,
    AuthorizedContextFileError,
    ImportLimits,
    issue_authorized_context_file,
)
from core.context_graph.validation import ContextGraphValidationError


def _thoughtdag(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "name": "grant-fixture",
                "exportedAt": "2026-08-30T00:00:00Z",
                "nodes": [
                    {
                        "id": "n1",
                        "type": "thought",
                        "position": {"x": 0, "y": 0},
                        "data": {"question": "Question", "response": "Answer"},
                    }
                ],
                "edges": [],
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_importer_requires_platform_issued_grant_not_legacy_path(tmp_path: Path) -> None:
    source = _thoughtdag(tmp_path / "fixture.thoughtdag.json")

    with pytest.raises(ContextGraphValidationError, match="authorized_context_file_grant_required"):
        ThoughtDAGImporter().import_authorized_file(
            authorized_path=source,
            project_id="project-a",
        )

    grant = issue_authorized_context_file(
        source,
        project_id="project-a",
        importer_ids=(ThoughtDAGImporter.importer_id,),
        allowed_paths=(source,),
    )
    snapshot = ThoughtDAGImporter().import_authorized_file(grant=grant)

    assert snapshot.project_id == "project-a"
    assert snapshot.provenance.source_ref == source.name


def test_grant_is_bound_to_allow_scope_project_importer_and_revision(tmp_path: Path) -> None:
    authorized_root = tmp_path / "selected"
    authorized_root.mkdir()
    source = _thoughtdag(authorized_root / "fixture.thoughtdag.json")
    outside = _thoughtdag(tmp_path / "outside.thoughtdag.json")

    with pytest.raises(AuthorizedContextFileError, match="outside_allow_scope"):
        issue_authorized_context_file(
            outside,
            project_id="project-a",
            importer_ids=(ThoughtDAGImporter.importer_id,),
            allowed_roots=(authorized_root,),
        )

    grant = issue_authorized_context_file(
        source,
        project_id="project-a",
        importer_ids=(ThoughtDAGImporter.importer_id,),
        allowed_roots=(authorized_root,),
    )
    with pytest.raises(AuthorizedContextFileError, match="project_scope_violation"):
        grant.validate_for(project_id="project-b", importer_id=ThoughtDAGImporter.importer_id)
    with pytest.raises(ContextGraphValidationError, match="importer_not_permitted"):
        MarkdownGraphImporter().import_authorized_file(grant=grant)

    source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ContextGraphValidationError, match="revision_drift"):
        ThoughtDAGImporter().import_authorized_file(grant=grant)


def test_grant_cannot_be_constructed_from_an_arbitrary_path(tmp_path: Path) -> None:
    source = _thoughtdag(tmp_path / "fixture.thoughtdag.json").resolve()

    with pytest.raises(AuthorizedContextFileError, match="forged"):
        AuthorizedContextFile(
            resolved_path=source,
            project_id="project-a",
            purpose="context_graph_import",
            importer_ids=(ThoughtDAGImporter.importer_id,),
            source_revision="mtime-0:size-0",
            _issuer_token=object(),
        )


@pytest.mark.parametrize(
    "limits",
    (
        {"max_nodes": -1},
        {"max_depth": True},
        {"secret_canaries": ["not-a-frozen-contract"]},
        {"secret_canaries": ("",)},
    ),
)
def test_import_limits_reject_invalid_runtime_values(limits: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid_context_import"):
        ImportLimits(**limits)  # type: ignore[arg-type]
