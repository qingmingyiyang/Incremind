from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application_skill import (
    ApplicationSkillCatalog,
    ApplicationSkillProposalRegistry,
    ObjectStoreApplicationSkillTraceRepository,
    SkillLearningWorkshop,
    SkillLearningWorkshopError,
)
from core.storage_provider import JsonObjectStore
from backend.api.application_skill_learning_runtime import ApplicationSkillLearningRuntime
from backend.api.routes.application_skill_learning import _TurnProjectAuthority


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / "store", legacy_root=tmp_path / "legacy")


def _package(catalog: ApplicationSkillCatalog, tmp_path: Path, *, source_kind: str = "user"):
    skill = (
        "---\nname: review-method\ndescription: Review one scoped implementation.\n"
        "trigger_boundary: Use for focused implementation review.\n"
        "---\n\nInspect the evidence before proposing a change.\n"
    )
    return catalog.package_from_verified_content(
        {"SKILL.md": skill.encode("utf-8")},
        source_id=f"{source_kind}-managed",
        source_kind=source_kind,
        package_root=tmp_path / "immutable" / "review-method",
    )


def _trace(package, *, fingerprint: str | None = None) -> dict[str, object]:
    selected_fingerprint = fingerprint or package.fingerprint
    selected = {
        "skill_id": "review-method", "skill_fingerprint": selected_fingerprint,
        "binding_id": "binding-review", "binding_revision": 1, "score": 100,
        "priority": 700, "instruction_bytes": 50, "resource_count": 0,
    }
    return {
        "schema_version": "1.0.0", "resolution_id": "skill-resolution-" + "a" * 32,
        "invocation_id": "request-alpha", "project_id": "project-alpha",
        "consumer": "answer.model-request", "task_kind": "implementation-review",
        "task_fingerprint": "b" * 64,
        "matched": [{
            "skill_id": "review-method", "skill_fingerprint": selected_fingerprint,
            "binding_id": "binding-review", "binding_revision": 1, "score": 100,
            "priority": 700, "reasons": ["trigger term"], "selected": True,
        }],
        "selected": [selected], "budget_excluded_skill_ids": [],
        "loaded_instruction_bytes": 50, "context_size_bytes": 100,
        "fallback": "none", "recorded_at": "2026-08-31T15:00:00+00:00",
    }


def _request(package, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "turn_receipt": {"turn_id": "turn-alpha", "receipt_id": "receipt-alpha", "project_id": "project-alpha", "status": "completed"},
        "resolution_id": "skill-resolution-" + "a" * 32,
        "skill_id": "review-method", "expected_fingerprint": package.fingerprint,
        "reusable_signal": {"kind": "explicit_correction", "evidence": "The reviewer corrected a recurring ordering error."},
        "proposed_content": {"summary": "Keep ordering evidence before implementation.", "instructions": "Check the recorded receipt and selection trace before drafting a proposal."},
    }
    value.update(overrides)
    return value


def _workshop(tmp_path: Path, *, source_kind: str = "user"):
    catalog = ApplicationSkillCatalog()
    package = _package(catalog, tmp_path, source_kind=source_kind)
    store = _store(tmp_path)
    ObjectStoreApplicationSkillTraceRepository(store).save_trace(_trace(package))
    snapshot = catalog.snapshot_from_packages((package,), scanned_source_count=1)
    return SkillLearningWorkshop(store, snapshot), store, package


def test_completed_selected_user_skill_creates_idempotent_pending_update(tmp_path: Path) -> None:
    workshop, store, package = _workshop(tmp_path)

    first = workshop.propose(**_request(package))
    replay = workshop.propose(**_request(package))

    assert first == replay
    assert first["status"] == "pending_review"
    assert first["action"] == "skill.update"
    assert first["write_effect"] == "proposal_only"
    stored = store.read("application_skill_proposals", str(first["proposal_id"]))
    assert stored is not None and stored["status"] == "pending_review"
    assert stored["payload"]["mutation_mode"] == "update_existing_user_skill"
    assert stored["payload"]["safety_diagnostics"] == [
        {"code": "credential_path_command_and_scope_scan", "status": "passed"},
    ]


def test_non_user_skill_can_only_fork_to_user_proposal(tmp_path: Path) -> None:
    workshop, _store_value, package = _workshop(tmp_path, source_kind="bundled")

    proposal = workshop.propose(**_request(package))

    assert proposal["action"] == "skill.fork"


def test_learning_proposal_can_be_explicitly_approved_or_rejected_without_apply(tmp_path: Path) -> None:
    workshop, store, package = _workshop(tmp_path)
    first = workshop.propose(**_request(package))
    registry = ApplicationSkillProposalRegistry(store)
    record = store.read("application_skill_proposals", str(first["proposal_id"]))
    assert record is not None

    approved = registry.require_approved(
        str(first["proposal_id"]), action="skill.update", expected_payload=record["payload"],
        confirm=True, reason="用户已核对来源与建议内容。",
    )

    assert approved["status"] == "approved"
    assert store.read("application_skill_proposals", str(first["proposal_id"]))["status"] == "approved"  # type: ignore[index]

    rejected_proposal = workshop.propose(**_request(package, proposed_content={
        "summary": "Keep scope checks near the proposal boundary.",
        "instructions": "Review the selected trace before writing a pending proposal.",
    }))
    rejected_record = store.read("application_skill_proposals", str(rejected_proposal["proposal_id"]))
    assert rejected_record is not None
    rejected = registry.reject(
        str(rejected_proposal["proposal_id"]), action="skill.update", expected_payload=rejected_record["payload"],
        confirm=True, reason="这条经验尚未稳定。",
    )

    assert rejected["status"] == "rejected"
    assert "applied_at" not in rejected


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("turn_receipt", {"turn_id": "turn-alpha", "receipt_id": "receipt-alpha", "project_id": "project-alpha", "status": "failed"}, "completed"),
        ("expected_fingerprint", "0" * 64, "fingerprint drifted"),
        ("proposed_content", {"summary": "x", "instructions": "rm -rf project"}, "dangerous command"),
        ("proposed_content", {"summary": "x", "instructions": "Store api_key=abcdefghijklmnop"}, "secret-like"),
        ("proposed_content", {"summary": "x", "instructions": "Read C:\\private\\file before every task"}, "absolute path"),
        ("proposed_content", {"summary": "x", "instructions": "Always apply this to all tasks."}, "overbroad"),
    ],
)
def test_workshop_rejects_untrusted_or_unsafe_evidence(tmp_path: Path, field: str, value: object, message: str) -> None:
    workshop, _store_value, package = _workshop(tmp_path)

    with pytest.raises(SkillLearningWorkshopError, match=message):
        workshop.propose(**_request(package, **{field: value}))


def test_workshop_rejects_skill_that_was_not_selected(tmp_path: Path) -> None:
    workshop, _store_value, package = _workshop(tmp_path)

    with pytest.raises(SkillLearningWorkshopError, match="not selected"):
        workshop.propose(**_request(package, skill_id="other-method"))


def test_runtime_uses_authoritative_completed_turn_and_never_applies_user_skill(tmp_path: Path) -> None:
    workshop, store, package = _workshop(tmp_path)
    runtime = ApplicationSkillLearningRuntime(
        workshop=workshop,
        receipts=SimpleNamespace(receipt_for=lambda turn_id, replayed=False: SimpleNamespace(
            turn_id=turn_id, operation_id="operation-alpha", status="completed",
        )),
        projects=SimpleNamespace(project_id_for=lambda _turn_id: "project-alpha"),
    )

    proposal = runtime.propose(
        turn_id="turn-alpha",
        **{key: value for key, value in _request(package).items() if key != "turn_receipt"},
    )

    assert proposal["status"] == "pending_review"
    assert proposal["write_effect"] == "proposal_only"
    stored = store.read("application_skill_proposals", str(proposal["proposal_id"]))
    assert stored is not None and stored["status"] == "pending_review"


def test_runtime_rejects_nonterminal_or_mismatched_turn_before_proposal_write(tmp_path: Path) -> None:
    workshop, store, package = _workshop(tmp_path)
    runtime = ApplicationSkillLearningRuntime(
        workshop=workshop,
        receipts=SimpleNamespace(receipt_for=lambda _turn_id, replayed=False: SimpleNamespace(
            turn_id="other-turn", operation_id="operation-alpha", status="failed",
        )),
        projects=SimpleNamespace(project_id_for=lambda _turn_id: "project-alpha"),
    )

    with pytest.raises(SkillLearningWorkshopError, match="authoritative completed"):
        runtime.propose(
            turn_id="turn-alpha",
            **{key: value for key, value in _request(package).items() if key != "turn_receipt"},
        )
    assert store.list("application_skill_proposals") == ()


def test_turn_project_authority_reads_persisted_request_scope() -> None:
    reader = _TurnProjectAuthority(SimpleNamespace(
        get_request=lambda turn_id: {
            "turn_id": turn_id,
            "scope": {"kind": "project", "project_id": "project-alpha"},
        },
    ))

    assert reader.project_id_for("turn-alpha") == "project-alpha"


def test_turn_project_authority_rejects_missing_scope() -> None:
    reader = _TurnProjectAuthority(SimpleNamespace(get_request=lambda _turn_id: None))

    with pytest.raises(SkillLearningWorkshopError, match="project authority"):
        reader.project_id_for("turn-alpha")
