from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "src" / "backend", ROOT / "src" / "core")
PRIVATE_CONTROL_TOKENS = (
    "requires_user_confirmation",
    "confirmation_required",
    "waiting_user",
    "need_user",
    "manual_confirmation",
)


def _handler_registration_modules() -> tuple[Path, ...]:
    return tuple(sorted(
        path
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
        if "EffectHandlerRegistration(" in path.read_text(encoding="utf-8")
    ))


def test_effect_handler_modules_do_not_own_private_confirmation_state_machines():
    modules = _handler_registration_modules()
    assert modules
    violations = {
        str(path.relative_to(ROOT)): token
        for path in modules
        for token in PRIVATE_CONTROL_TOKENS
        if token in path.read_text(encoding="utf-8")
    }
    assert violations == {}


def test_workflow_decision_write_surface_is_single_and_does_not_restore_job_commands():
    route = (ROOT / "src" / "backend" / "api" / "routes" / "workflow_decisions.py").read_text(encoding="utf-8")
    frontend = (ROOT / "src" / "frontend" / "src" / "features" / "rebuild" / "jobStatusApi.js").read_text(encoding="utf-8")
    assert route.count('@router.post("/api/rebuild/workflows/effects/{operation_id}/decision")') == 1
    assert "/retry" not in frontend
    assert "/resume" not in frontend
    assert "/cancel" not in frontend
