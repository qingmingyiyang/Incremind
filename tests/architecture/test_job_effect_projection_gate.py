from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

_EFFECT_STATE_COMMANDS = {
    "begin_planned",
    "renew",
    "release_to_planned",
    "settle_ok",
    "settle_verified_ok",
    "settle_error",
    "settle_verified_error",
    "mark_unknown",
    "abandon",
}


def _job_runner_modules() -> tuple[Path, ...]:
    return tuple(sorted((ROOT / "src/core/job_runner").glob("*.py")))


def _attribute_calls(path: Path, names: set[str]) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in names:
            violations.append((node.lineno, node.func.attr))
    return violations


def _call_owner(tree: ast.AST, node: ast.Call) -> str | None:
    for owner in ast.walk(tree):
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and node in ast.walk(owner):
            return owner.name
    return None


def test_production_job_store_is_readonly_legacy_import_only() -> None:
    path = ROOT / "src/core/job_runner/sqlite_store.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = {
        "import_legacy_job_store_history",
    }
    violations: list[tuple[int, str | None, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        sql = node.args[0]
        if not isinstance(sql, ast.Constant) or not isinstance(sql.value, str):
            continue
        if "job_store" not in sql.value.lower():
            continue
        owner = _call_owner(tree, node)
        if owner not in allowed:
            violations.append((node.lineno, owner, sql.value.strip()))
    assert violations == []

    store_source = path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS job_store" not in store_source


def test_production_job_repository_has_no_configurable_authority_split() -> None:
    runtime = (ROOT / "src/backend/api/job_runtime.py").read_text(encoding="utf-8")
    routed = (ROOT / "src/core/job_runner/routed_repository.py").read_text(encoding="utf-8")
    assert "os.environ" not in runtime
    assert "job.get(\"job_type\") in self.sqlite_job_types" not in routed
    assert "self.legacy.save(updated)" not in routed


def test_production_has_no_generic_job_worker_or_job_lease_execution_path() -> None:
    job_runner = ROOT / "src/core/job_runner"
    assert not (job_runner / "sqlite_worker.py").exists()

    package = (job_runner / "__init__.py").read_text(encoding="utf-8")
    lifecycle = (job_runner / "sqlite_lifecycle.py").read_text(encoding="utf-8")
    store = (job_runner / "sqlite_store.py").read_text(encoding="utf-8")
    assert "SQLiteDeterministicJobWorker" not in package
    assert "run_now" not in lifecycle
    assert "_heartbeat" not in lifecycle
    assert "lease_seconds" not in lifecycle
    assert "heartbeat_seconds" not in lifecycle
    for retired in (
        "save_leased",
        "acquire_lease",
        "renew_lease",
        "cancel_leased",
        "release_lease",
        "def complete(",
        "def fail(",
    ):
        assert retired not in store


def test_legacy_job_commands_and_status_recovery_are_not_production_authorities() -> None:
    routes = (ROOT / "src/backend/api/routes/product/jobs.py").read_text(encoding="utf-8")
    runtime = (ROOT / "src/backend/api/job_execution_runtime.py").read_text(encoding="utf-8")
    for action in ("retry", "resume", "cancel"):
        assert f'/api/rebuild/jobs/{{job_id}}/{action}' not in routes
    assert 'record.payload.get("status")' not in runtime
    assert "_TERMINAL_JOB_STATUSES" not in runtime
    assert "job_execution.running_outcome_unknown" not in runtime
    assert 'return EffectState.UNKNOWN, _LEGACY_JOB_EXECUTION_REASON' in runtime


def test_legacy_history_import_is_never_triggered_by_job_reads_or_deleted_after_import() -> None:
    store_path = ROOT / "src/core/job_runner/sqlite_store.py"
    routed_path = ROOT / "src/core/job_runner/routed_repository.py"
    store_tree = ast.parse(store_path.read_text(encoding="utf-8"))
    routed_tree = ast.parse(routed_path.read_text(encoding="utf-8"))

    store_functions = {
        node.name: node
        for node in ast.walk(store_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("read", "all"):
        calls = {
            node.func.attr
            for node in ast.walk(store_functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_connect_readonly" in calls
        assert "_connect" not in calls
        assert "import_legacy_job_store_history" not in calls

    routed_calls = {
        node.func.attr
        for node in ast.walk(routed_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "migrate_legacy_jobs" not in routed_calls
    assert "delete" not in routed_calls


def test_legacy_history_module_cannot_plan_transition_or_recover_effects() -> None:
    path = ROOT / "src/core/job_runner/legacy_history.py"
    text = path.read_text(encoding="utf-8")
    assert "EffectRunner" not in text
    assert "EffectReaper" not in text
    assert "plan_in_connection" not in text
    assert "plan_v2" not in text
    assert _attribute_calls(path, _EFFECT_STATE_COMMANDS) == []


def test_direct_legacy_job_repository_construction_is_limited_to_cutover_composition() -> None:
    allowed = {
        "src/backend/api/job_runtime.py",
        "src/core/composition.py",
    }
    violations: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "ObjectStoreJobRepository(" not in text:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative not in allowed:
            violations.append(relative)
    assert violations == []


def test_job_runner_does_not_command_effect_execution_or_lease_state() -> None:
    """Job APIs may project execution, but cannot own Effect lifecycle commands."""

    violations = {
        path.relative_to(ROOT).as_posix(): _attribute_calls(path, _EFFECT_STATE_COMMANDS)
        for path in _job_runner_modules()
        if _attribute_calls(path, _EFFECT_STATE_COMMANDS)
    }
    assert violations == {}, (
        "Job production modules must not command Effect execution, terminal state, "
        f"or Effect lease state: {violations}"
    )


def test_job_runner_does_not_translate_job_status_or_lease_into_effect_state() -> None:
    """EffectState may feed Job display labels, but Job payload cannot drive Effect state."""

    reverse_helpers = {"_job_effect_state", "_step_effect_state", "_sync_node_state"}
    violations: dict[str, list[tuple[int, str]]] = {}
    for path in _job_runner_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders = [
            (node.lineno, node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in reverse_helpers
        ]
        if offenders:
            violations[path.relative_to(ROOT).as_posix()] = offenders
    assert violations == {}, (
        "Job status, steps, or leases must not derive an Effect target state: "
        f"{violations}"
    )


def test_job_runner_has_no_private_effect_recovery_scheduler() -> None:
    """Only Core Reaper may recover external Effect execution."""

    forbidden = {"EffectReaper", "recover_expired", "recover_pending", "schedule_ready"}
    violations: dict[str, list[int]] = {}
    for path in _job_runner_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in forbidden
        ]
        if lines:
            violations[path.relative_to(ROOT).as_posix()] = lines
    assert violations == {}, f"Job modules cannot host Effect recovery: {violations}"


def test_legacy_media_is_not_registered_or_executed_by_a_job_worker() -> None:
    """Media external execution belongs to its v2 Core Handler; the worker is retired."""

    assert not (ROOT / "src/core/job_runner/sqlite_worker.py").exists()

    composition = (
        ROOT / "src/backend/api/media_hands_composition.py"
    ).read_text(encoding="utf-8")
    assert "media_handler=" not in composition
    lifecycle = (
        ROOT / "src/backend/api/job_lifecycle_runtime.py"
    ).read_text(encoding="utf-8")
    assert "media_handler" not in lifecycle


def test_new_job_effect_nodes_reject_legacy_planning() -> None:
    """New Job nodes cannot bypass the Gate/Intent versioned planning seam."""

    legacy_calls: dict[str, list[tuple[int, str]]] = {}
    for path in _job_runner_modules():
        relative = path.relative_to(ROOT).as_posix()
        offenders = _attribute_calls(path, {"plan_in_connection"})
        if offenders:
            legacy_calls[relative] = offenders
    assert legacy_calls == {}, f"Job nodes cannot use legacy plan_in_connection: {legacy_calls}"


def test_new_job_effect_nodes_require_a_v2_planning_seam() -> None:
    """The accepted seams are public or transaction-bound v2 Gate/Intent planning."""

    v2_calls = {
        path.relative_to(ROOT).as_posix(): _attribute_calls(
            path, {"plan_v2", "plan_v2_in_connection"},
        )
        for path in _job_runner_modules()
        if _attribute_calls(path, {"plan_v2", "plan_v2_in_connection"})
    }
    assert v2_calls, "Job node creation requires plan_v2 or plan_v2_in_connection"
