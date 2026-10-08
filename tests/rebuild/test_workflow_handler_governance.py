from core.product_core.workflow_handler_governance import (
    WORKFLOW_HANDLER_GOVERNANCE,
    MAIN_WORKFLOW_HANDLER_KINDS,
    INLINE_WORKFLOW_EFFECT_KINDS,
    AI_WORKFLOW_EFFECT_KINDS,
    PPT_MASTER_WORKFLOW_HANDLER_KINDS,
    WorkflowHandlerCategory,
    validate_workflow_handler_governance,
    workflow_handler_governance_projection,
)


def test_every_required_workflow_category_has_real_handler_and_closed_mode():
    entries = tuple(WORKFLOW_HANDLER_GOVERNANCE.values())
    assert {entry.category for entry in entries} == set(WorkflowHandlerCategory)
    assert {entry.mode.value for entry in entries} == {"auto", "auto_with_notice", "ask"}


def test_handler_governance_fails_closed_for_unclassified_or_stale_kind():
    kinds = tuple(WORKFLOW_HANDLER_GOVERNANCE)
    validate_workflow_handler_governance(kinds)
    try:
        validate_workflow_handler_governance((*kinds, "private_confirmation_handler"))
    except RuntimeError as error:
        assert "private_confirmation_handler" in str(error)
    else:
        raise AssertionError("unclassified Handler must fail closed")

    assert workflow_handler_governance_projection("provider_model_discovery") == {
        "category": "model",
        "boundary": "reversible_visible_change",
        "default_mode": "auto_with_notice",
    }
    assert workflow_handler_governance_projection("workbench_content_transform") == {
        "category": "media",
        "boundary": "deterministic",
        "default_mode": "auto",
    }
    assert workflow_handler_governance_projection("bilibili_media_postprocess") == {
        "category": "media",
        "boundary": "deterministic",
        "default_mode": "auto",
    }
    validate_workflow_handler_governance(
        tuple(MAIN_WORKFLOW_HANDLER_KINDS), expected_kinds=MAIN_WORKFLOW_HANDLER_KINDS,
    )
    validate_workflow_handler_governance(
        tuple(PPT_MASTER_WORKFLOW_HANDLER_KINDS),
        expected_kinds=PPT_MASTER_WORKFLOW_HANDLER_KINDS,
    )
    validate_workflow_handler_governance(
        tuple(INLINE_WORKFLOW_EFFECT_KINDS),
        expected_kinds=INLINE_WORKFLOW_EFFECT_KINDS,
    )
    validate_workflow_handler_governance(
        tuple(AI_WORKFLOW_EFFECT_KINDS), expected_kinds=AI_WORKFLOW_EFFECT_KINDS,
    )


def test_ai_partition_effect_constructors_are_all_governed():
    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    runtime = (root / "src" / "core" / "ai_kernel" / "runtime.py").read_text(encoding="utf-8")
    store = (root / "src" / "core" / "ai_kernel" / "sqlite_store.py").read_text(encoding="utf-8")
    assert 'kind="model_call"' in store
    assert 'kind="mcp_call"' in store
    assert 'kind="memory_propose"' in store
    assert 'kind=f"tool_call_{effect_class.value.lower()}"' in runtime
    assert AI_WORKFLOW_EFFECT_KINDS <= set(WORKFLOW_HANDLER_GOVERNANCE)
