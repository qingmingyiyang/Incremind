from pathlib import Path
import sqlite3

import pytest

from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_boundary_mode_command import ProjectBoundaryModeCommandService
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.project_capability_selection_command import (
    CapabilitySelectionCommandConflict,
    CapabilitySelectionCommandError,
    ProjectCapabilitySelectionCommandService,
    resolve_exclusion_target,
    resolve_selection_target,
)
from core.ai_boundary import BoundaryGrant
from core.ai_kernel import CapabilityDefinition
from core.ai_tooling import tool_boundary_target_identity, tool_from_capability


def _capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        "memory.recall", 1, "read", False, "read_only",
        "crp://input", "crp://output",
    )


def _external_capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        "external.search", 1, "external", True, "receipt_required",
        "crp://external-input", "crp://external-output",
    )


def _target(tmp_path: Path) -> str:
    return resolve_exclusion_target(
        stable_id="memory.recall",
        profile=ProjectCapabilityProfileStore(tmp_path).get("alpha").profile,
        boundary=ProjectBoundaryProfileStore(tmp_path).get("alpha").profile,
        capabilities=(_capability(),), registry_generation=3,
        expected_registry_generation=3,
    )


def test_exclude_command_is_idempotent_and_does_not_advance_boundary(tmp_path: Path) -> None:
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    try:
        first = service.exclude(
            project_id="alpha", command_id="exclude-1",
            target_stable_id=_target(tmp_path), expected_boundary_revision=1,
            expected_capability_revision=1, expected_registry_generation=3,
        )
        replay = service.exclude(
            project_id="alpha", command_id="exclude-1",
            target_stable_id="memory.recall", expected_boundary_revision=1,
            expected_capability_revision=1, expected_registry_generation=3,
        )
        assert replay == first and first.status == "completed"
        assert first.capability_revision == 2
        capability = ProjectCapabilityProfileStore(tmp_path).get("alpha").profile
        assert capability.denied_tool_ids == ("memory.recall",)
        assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 1
    finally:
        service.close()


def test_exclude_recovers_crash_after_profile_write(tmp_path: Path) -> None:
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    original_set = service._set

    def crash(receipt, status, **changes):
        if status == "capability_updated":
            raise RuntimeError("simulated crash")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            service.exclude(
                project_id="alpha", command_id="exclude-1",
                target_stable_id=_target(tmp_path), expected_boundary_revision=1,
                expected_capability_revision=1, expected_registry_generation=3,
            )
        assert service.get("exclude-1").status == "prepared"  # type: ignore[union-attr]
        service._set = original_set  # type: ignore[method-assign]
        receipt = service.exclude(
            project_id="alpha", command_id="exclude-1",
            target_stable_id="memory.recall", expected_boundary_revision=1,
            expected_capability_revision=1, expected_registry_generation=3,
        )
        assert receipt.status == "completed"
    finally:
        service.close()


def test_exclude_rejects_unknown_generation_and_semantic_reuse(tmp_path: Path) -> None:
    with pytest.raises(CapabilitySelectionCommandError, match="target_unavailable"):
        resolve_exclusion_target(
            stable_id="private.tool",
            profile=ProjectCapabilityProfileStore(tmp_path).get("alpha").profile,
            boundary=ProjectBoundaryProfileStore(tmp_path).get("alpha").profile,
            capabilities=(_capability(),), registry_generation=3,
            expected_registry_generation=2,
        )
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    try:
        service.exclude(
            project_id="alpha", command_id="exclude-1",
            target_stable_id=_target(tmp_path), expected_boundary_revision=1,
            expected_capability_revision=1, expected_registry_generation=3,
        )
        with pytest.raises(CapabilitySelectionCommandConflict):
            service.exclude(
                project_id="alpha", command_id="exclude-1",
                target_stable_id="other.tool", expected_boundary_revision=1,
                expected_capability_revision=1, expected_registry_generation=3,
            )
    finally:
        service.close()


def test_exclude_shares_governance_reservation_with_boundary_mode(tmp_path: Path) -> None:
    mode = ProjectBoundaryModeCommandService(tmp_path)
    original_set = mode._set

    def crash(receipt, status, **changes):
        if status == "boundary_updated":
            raise RuntimeError("leave active")
        return original_set(receipt, status, **changes)

    try:
        mode._set = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            mode.submit(
                project_id="alpha", command_id="mode-1", mode="sealed",
                expected_boundary_revision=1, expected_capability_revision=1,
            )
        selection = ProjectCapabilitySelectionCommandService(tmp_path)
        try:
            with pytest.raises(CapabilitySelectionCommandConflict, match="active"):
                selection.exclude(
                    project_id="alpha", command_id="exclude-1",
                    target_stable_id="memory.recall",
                    expected_boundary_revision=2,
                    expected_capability_revision=1,
                    expected_registry_generation=3,
                )
        finally:
            selection.close()
    finally:
        mode.close()


def test_select_and_reset_exclusion_write_current_contract_binding(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    capability_store.exclude_tool(
        "alpha", tool_id="memory.recall", expected_revision=1,
    )
    profile = capability_store.get("alpha").profile
    boundary = ProjectBoundaryProfileStore(tmp_path).get("alpha").profile
    target = resolve_selection_target(
        action="reset_exclusion", stable_id="memory.recall",
        profile=profile, boundary=boundary, capabilities=(_capability(),),
        registry_generation=3, expected_registry_generation=3,
    )
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    try:
        receipt = service.select(
            project_id="alpha", command_id="reset-1",
            action="reset_exclusion", target=target,
            expected_boundary_revision=1, expected_capability_revision=2,
            expected_registry_generation=3,
        )
        assert receipt.status == "completed" and receipt.capability_revision == 3
        assert "contract_identity" not in receipt.public()
        selected = capability_store.get("alpha").profile
        assert selected.denied_tool_ids == ()
        assert selected.tool_selection_bindings[0].contract_identity == target.contract_identity
    finally:
        service.close()


def test_select_recovers_profile_written_crash_offline(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    capability_store.update(
        "alpha", expected_revision=0,
        boundary_profile_id="project-boundary-alpha",
        boundary_profile_revision=1,
        tool_discovery_policy="confirm_new",
    )
    target = resolve_selection_target(
        action="select", stable_id="memory.recall",
        profile=capability_store.get("alpha").profile,
        boundary=ProjectBoundaryProfileStore(tmp_path).get("alpha").profile,
        capabilities=(_capability(),), registry_generation=3,
        expected_registry_generation=3,
    )
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    original_set = service._set

    def crash(receipt, status, **changes):
        if status == "capability_updated":
            raise RuntimeError("after profile write")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            service.select(
                project_id="alpha", command_id="select-1", action="select",
                target=target, expected_boundary_revision=1,
                expected_capability_revision=1, expected_registry_generation=3,
            )
        receipt = service.get("select-1")
        assert receipt is not None and receipt.status == "prepared"
        service._set = original_set  # type: ignore[method-assign]
        recovered = service.complete_if_profile_written(receipt)
        assert recovered is not None and recovered.status == "completed"
    finally:
        service.close()


def test_selection_rejects_active_grant_reactivation(tmp_path: Path) -> None:
    capability = _capability()
    tool = tool_from_capability(capability)
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    capability_store.exclude_tool(
        "alpha", tool_id="memory.recall", expected_revision=1,
    )
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    boundary_store.create_grant(
        "alpha", expected_revision=1,
        grant=BoundaryGrant(
            "grant-1", "ai-kernel", "alpha",
            tool_boundary_target_identity(tool, capability.capability_id),
            ("read",), ("unclassified",), ("local",), None, 1,
        ),
    )
    capability_store.rebind_boundary(
        "alpha", expected_revision=2,
        boundary_profile_id="project-boundary-alpha", boundary_profile_revision=2,
    )
    with pytest.raises(CapabilitySelectionCommandError, match="target_unavailable"):
        resolve_selection_target(
            action="reset_exclusion", stable_id="memory.recall",
            profile=capability_store.get("alpha").profile,
            boundary=boundary_store.get("alpha").profile,
            capabilities=(capability,), registry_generation=3,
            expected_registry_generation=3,
        )


def test_selection_cannot_be_preapproved_while_boundary_is_sealed(tmp_path: Path) -> None:
    capability_store = ProjectCapabilityProfileStore(tmp_path)
    capability_store.exclude_tool(
        "alpha", tool_id="external.search", expected_revision=1,
    )
    boundary_store = ProjectBoundaryProfileStore(tmp_path)
    boundary = boundary_store.update(
        "alpha", mode="sealed", remote_default="deny", expected_revision=0,
    ).profile
    capability_store.rebind_boundary(
        "alpha", expected_revision=2,
        boundary_profile_id=boundary.profile_id,
        boundary_profile_revision=boundary.revision,
    )
    with pytest.raises(CapabilitySelectionCommandError, match="target_unavailable"):
        resolve_selection_target(
            action="reset_exclusion", stable_id="external.search",
            profile=capability_store.get("alpha").profile, boundary=boundary,
            capabilities=(_external_capability(),), registry_generation=3,
            expected_registry_generation=3,
        )


def test_existing_exclude_receipt_database_migrates_without_losing_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".rebuild-data/capability-selection-commands.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE capability_selection_commands (
            command_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            action TEXT NOT NULL, target_stable_id TEXT NOT NULL,
            status TEXT NOT NULL, expected_boundary_revision INTEGER NOT NULL,
            expected_capability_revision INTEGER NOT NULL,
            expected_registry_generation INTEGER NOT NULL,
            capability_revision INTEGER, actor_id TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        conn.execute("""INSERT INTO capability_selection_commands VALUES (
            'exclude-old', 'alpha', 'exclude', 'memory.recall', 'completed',
            1, 1, 3, 2, 'desktop-user', '2026-08-24T00:00:00+00:00',
            '2026-08-24T00:00:00+00:00'
        )""")
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    try:
        receipt = service.get("exclude-old")
        assert receipt is not None and receipt.target_contract_identity is None
        assert receipt.public()["status"] == "completed"
    finally:
        service.close()
