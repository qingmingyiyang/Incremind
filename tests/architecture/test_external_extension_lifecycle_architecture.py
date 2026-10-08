"""Architecture gates for governed external-extension lifecycle work.

These checks deliberately inspect production AST rather than exercising a
mocked happy path.  They freeze the ownership boundary while the HTTP route is
being added: one pre-existing EffectRuntime owns all lifecycle execution, and
external Skill materialization remains a local, data-only handler strategy.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
RUNTIME = SOURCE / "core" / "external_extension_runtime"
STARTUP = SOURCE / "backend" / "api" / "external_extension_runtime_startup.py"
WORKFLOW = SOURCE / "backend" / "api" / "external_extension_install_workflow.py"
ROUTE = SOURCE / "backend" / "api" / "routes" / "external_extensions.py"
AI_RUNTIME = SOURCE / "backend" / "memory_app" / "kernel" / "ai_runtime.py"
SKILL_SNAPSHOT = SOURCE / "backend" / "api" / "application_skill_snapshot.py"
LIFECYCLE_COMMANDS = RUNTIME / "lifecycle_commands.py"
GATE_AUTHORITY = RUNTIME / "gate_authority.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _calls(tree: ast.AST, names: set[str]) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted(node.func) in names
    ]


def _functions(tree: ast.AST, name: str) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef]:
    return (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _argument_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        argument.arg
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        )
    }


def test_lifecycle_composition_reuses_the_existing_effect_runtime_and_core_registry() -> None:
    """Startup must only attach strategies to the application's Runtime."""

    startup = _tree(STARTUP)
    assert not _calls(startup, {"EffectRuntime", "EffectRunner", "EffectReaper"})

    registration = next(_functions(startup, "register_external_extension_runtime"))
    calls = _calls(registration, {"register_external_extension_lifecycle_handlers"})
    assert len(calls) == 1
    assert [_dotted(argument) for argument in calls[0].args] == [
        "effect_runtime.handlers", "terminal_receipts", "materializer",
    ]

    # The pre-existing Runtime is the only lifecycle dispatch object passed to
    # the command service.  It is not replaced by a private Runner/Reaper.
    commands = _calls(registration, {"ExternalExtensionLifecycleCommandService"})
    assert len(commands) == 1
    assert _dotted(commands[0].args[-1]) == "effect_runtime"


def test_lifecycle_handler_has_one_registration_factory_and_no_private_runtime() -> None:
    """Lifecycle handlers stay Core Handler strategies, never a scheduler."""

    terminal = RUNTIME / "terminal_receipts.py"
    tree = _tree(terminal)
    assert not _calls(tree, {"EffectRuntime", "EffectRunner", "EffectReaper"})

    handler_construction = _calls(tree, {"ExternalExtensionLifecycleHandler"})
    assert len(handler_construction) == 1
    parent = next(_functions(tree, "register_external_extension_lifecycle_handlers"))
    assert handler_construction[0] in list(ast.walk(parent))

    consumers: list[Path] = []
    for path in SOURCE.rglob("*.py"):
        parsed = _tree(path)
        if _calls(parsed, {"ExternalExtensionLifecycleHandler"}):
            consumers.append(path.relative_to(ROOT))
    assert consumers == [terminal.relative_to(ROOT)]


def test_lifecycle_services_do_not_construct_another_effect_runtime_runner_or_reaper() -> None:
    for path in (
        RUNTIME / "lifecycle_commands.py",
        RUNTIME / "installation.py",
        RUNTIME / "skill_materializer.py",
        WORKFLOW,
    ):
        assert not _calls(_tree(path), {"EffectRuntime", "EffectRunner", "EffectReaper"}), path


def test_lifecycle_backfill_and_terminal_projection_use_only_core_coordinator_hooks() -> None:
    startup = _tree(STARTUP)
    public_registration = next(
        _functions(startup, "register_external_extension_recovery")
    )
    registration = next(
        _functions(startup, "_register_external_extension_recovery_callbacks")
    )
    assert _calls(public_registration, {"handles.register_recovery"})
    assert len(_calls(registration, {"coordinator.register_backfill"})) == 3
    assert len(_calls(registration, {"coordinator.register_coordination"})) == 1
    assert not _calls(registration, {"EffectRuntime", "EffectRunner", "EffectReaper"})
    assert not _calls(registration, {"EffectHandlerRegistration"})

    registrations = {
        _dotted(node.func): {
            keyword.arg: (
                keyword.value.value
                if isinstance(keyword.value, ast.Constant)
                else _dotted(keyword.value)
            )
            for keyword in node.args[0].keywords
        }
        for node in ast.walk(registration)
        if isinstance(node, ast.Call)
        and _dotted(node.func) in {
            "coordinator.register_backfill", "coordinator.register_coordination",
        }
        and node.args
        and isinstance(node.args[0], ast.Call)
    }
    backfill_kinds = {
        node.args[0].keywords[0].value.value
        for node in ast.walk(registration)
        if isinstance(node, ast.Call)
        and _dotted(node.func) == "coordinator.register_backfill"
        and node.args
        and isinstance(node.args[0], ast.Call)
    }
    assert backfill_kinds == {
        "external-extension-confirmed-acquire-intents",
        "external-extension-lifecycle-intents",
        "external-extension-resolve-intents",
    }
    assert registrations["coordinator.register_coordination"]["kind"] == (
        "external-extension-terminal-projections"
    )

    lifecycle = _tree(LIFECYCLE_COMMANDS)
    reconcile = next(_functions(lifecycle, "reconcile_terminal_projections"))
    calls = {_dotted(node.func) for node in ast.walk(reconcile) if isinstance(node, ast.Call)}
    assert calls.isdisjoint({
        "self._runtime.execute_v2",
        "self._runtime.dispatch_planned",
        "self._runtime.dispatch_operation",
        "self._runtime.recover_expired",
        "self._runtime.runner.execute_planned",
        "self._gate_authority.authorize",
    })
    terminal_record = next(_functions(lifecycle, "_reconcile_terminal_record"))
    assert _calls(terminal_record, {"self._runtime.log.get"})


def test_workflow_derives_gate_and_effect_identities_server_side() -> None:
    """Caller-facing workflow APIs cannot inject Effect/Gate authority fields."""

    tree = _tree(WORKFLOW)
    forbidden = {
        "gate", "gate_fact", "gate_decision_id", "kind", "effect_kind", "rev_set",
        "session", "session_id", "step", "step_key", "idem", "idem_key", "now",
    }
    workflow = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExternalExtensionInstallWorkflow"
    )
    public = [
        function for function in workflow.body
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and function.name in {"preview", "confirm"}
    ]
    assert len(public) == 2
    assert all(_argument_names(function).isdisjoint(forbidden) for function in public)

    # Workflow supplies only an immutable authorization reference; the
    # startup-private intake executor re-derives the Gate grant.  Neither
    # caller may manufacture ALLOW or a GateDecisionFact.
    lifecycle = _tree(LIFECYCLE_COMMANDS)
    assert not _calls(tree, {"GateDecisionFact"})
    assert not _calls(lifecycle, {"GateDecisionFact"})
    for caller in (tree, lifecycle):
        assert not any(
            isinstance(node, ast.Attribute)
            and _dotted(node) == "GateDecision.ALLOW"
            for node in ast.walk(caller)
        )
    assert not _calls(tree, {"self._handles.gate_authority.authorize"})
    assert len(_calls(lifecycle, {"self._gate_authority.authorize"})) == 1

    startup = _tree(STARTUP)
    registration = next(_functions(startup, "register_external_extension_runtime"))
    assert len(_calls(registration, {"ExternalExtensionGateAuthority"})) == 1
    assert len(_calls(_tree(GATE_AUTHORITY), {"GateDecisionFact"})) == 1
    executor = next(
        node for node in startup.body
        if isinstance(node, ast.ClassDef) and node.name == "_ExternalExtensionIntakeExecutor"
    )
    intake = next(
        node for node in executor.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_install"
    )
    assert len(_calls(intake, {"self._gate_authority.authorize"})) == 1


def test_startup_hides_raw_intake_commands_and_rederives_gate_facts() -> None:
    """Same-process callers cannot inject a Gate fact or Effect identity."""

    startup = _tree(STARTUP)
    handles = next(
        node for node in startup.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ExternalExtensionRuntimeStartupHandles"
    )
    fields = {
        node.target.id
        for node in handles.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert fields == {
        "install_workflow", "active_packages", "active_sources", "_register_recovery",
    }
    assert fields.isdisjoint({"facts", "gate_authority", "_intake", "installations", "lifecycle_commands"})
    public = {
        node.name: _argument_names(node)
        for node in handles.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"resolve_install", "acquire_resolved"}
    }
    assert public == {}
    forbidden = {"gate_fact", "gate_decision_id", "session_id", "root_id", "step_key", "rev_set"}
    assert all(arguments.isdisjoint(forbidden) for arguments in public.values())

    executor = next(
        node for node in startup.body
        if isinstance(node, ast.ClassDef) and node.name == "_ExternalExtensionIntakeExecutor"
    )
    executor_methods = {
        node.name: _argument_names(node)
        for node in executor.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"resolve_install", "acquire_resolved"}
    }
    assert all(arguments.isdisjoint(forbidden) for arguments in executor_methods.values())
    for method in ("resolve_install", "acquire_resolved"):
        function = next(node for node in executor.body if isinstance(node, ast.FunctionDef) and node.name == method)
        assert len(_calls(function, {"self._gate_authority.authorize"})) == 1
    resolve = next(node for node in executor.body if isinstance(node, ast.FunctionDef) and node.name == "resolve_install")
    assert len(_calls(resolve, {"self._facts._record_intent_and_plan_effect"})) == 1
    assert len(_calls(resolve, {"self._runtime.dispatch_operation"})) == 1
    acquire = next(node for node in executor.body if isinstance(node, ast.FunctionDef) and node.name == "acquire_resolved")
    assert len(_calls(acquire, {"self._facts._record_intent_and_plan_effect"})) == 1

    # The publicly reachable workflow receives only bound high-level calls;
    # its port may not retain any raw stateful authority object.
    workflow = next(
        node for node in _tree(WORKFLOW).body
        if isinstance(node, ast.ClassDef) and node.name == "_ExternalExtensionWorkflowPort"
    )
    port_fields = {
        node.target.id for node in workflow.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert port_fields.isdisjoint({"_facts", "_gate_authority", "_runtime", "_installations", "_lifecycle", "_intake"})

    fact_store = _tree(RUNTIME / "fact_store.py")
    assert not any(
        isinstance(node, ast.FunctionDef) and node.name == "record_intent_and_plan_effect"
        for node in ast.walk(fact_store)
    )
    private_planner = [
        node for node in ast.walk(fact_store)
        if isinstance(node, ast.FunctionDef) and node.name == "_record_intent_and_plan_effect"
    ]
    assert len(private_planner) == 1
    planner_calls = [
        path for path in SOURCE.rglob("*.py")
        if _calls(_tree(path), {"self._facts._record_intent_and_plan_effect"})
    ]
    assert planner_calls == [STARTUP]
    assert len(_calls(acquire, {"self._runtime.dispatch_operation"})) == 1


def test_install_preview_is_local_and_cannot_enter_gate_or_effect_execution() -> None:
    tree = _tree(WORKFLOW)
    preview = next(_functions(tree, "preview"))
    calls = {_dotted(node.func) for node in ast.walk(preview) if isinstance(node, ast.Call)}
    assert calls.isdisjoint({
        "self._handles.gate_authority.authorize",
        "self._handles.resolve_install",
        "self._handles.acquire_resolved",
        "self._handles.lifecycle_commands.execute",
    })


def test_route_keeps_client_inputs_to_reviewable_install_fields_when_present() -> None:
    """Route assertion becomes live with the route; do not hide this pending work."""

    if not ROUTE.exists():
        pytest.skip("external-extension HTTP route has not been added; add request DTO AST gate with the route")
    tree = _tree(ROUTE)
    forbidden = {
        "gate", "gate_fact", "gate_decision_id", "kind", "effect_kind", "rev_set",
        "session", "session_id", "step", "step_key", "idem", "idem_key", "now",
    }
    request_models = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name.endswith("Request")
    ]
    assert request_models
    fields = {
        target.id
        for model in request_models
        for node in model.body
        if isinstance(node, ast.AnnAssign)
        for target in [node.target]
        if isinstance(target, ast.Name)
    }
    assert fields.isdisjoint(forbidden)


def test_materializer_is_local_data_only_and_active_packages_freeze_authority() -> None:
    """A reviewed Skill cannot make network/process/secret calls or bypass checks."""

    tree = _tree(RUNTIME / "skill_materializer.py")
    forbidden_import_roots = {
        "socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp",
        "subprocess", "asyncio", "secrets",
    }
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imports.isdisjoint(forbidden_import_roots)

    forbidden_calls = {
        "eval", "exec", "compile", "os.system", "os.popen", "os.execv", "os.execve",
        "subprocess.Popen", "subprocess.run", "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
    assert not _calls(tree, forbidden_calls)
    assert not any("secret" in _dotted(node.func).lower() for node in ast.walk(tree) if isinstance(node, ast.Call))

    active = next(_functions(tree, "active_packages"))
    names = {_dotted(node.func) for node in ast.walk(active) if isinstance(node, ast.Call)}
    assert "self._installations.active_revisions" in names
    assert "self._read_materialized" in names
    assert "self._exact_bindings" in names
    assert "discover_selected" not in names

    frozen = next(_functions(tree, "_packages_from_verified_tree"))
    frozen_names = {
        _dotted(node.func) for node in ast.walk(frozen) if isinstance(node, ast.Call)
    }
    assert "ApplicationSkillVerifiedContent.from_mapping" in frozen_names
    assert "catalog.package_from_verified_content" in frozen_names


def test_production_turn_consumes_immutable_external_packages_not_paths() -> None:
    runtime = _tree(AI_RUNTIME)
    build = next(_functions(runtime, "build_ai_runtime"))
    getattr_fields = {
        node.args[1].value
        for node in ast.walk(build)
        if isinstance(node, ast.Call)
        and _dotted(node.func) == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    }
    assert "active_packages" in getattr_fields
    assert "active_sources" not in getattr_fields

    authority_calls = _calls(build, {"TurnApplicationSkillSnapshotAuthority"})
    assert len(authority_calls) == 1
    keywords = {keyword.arg: _dotted(keyword.value) for keyword in authority_calls[0].keywords}
    assert keywords.get("external_packages") == "external_application_skill_packages"
    assert "external_sources" not in keywords

    snapshot = _tree(SKILL_SNAPSHOT)
    acquire = next(_functions(snapshot, "acquire"))
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "snapshot_from_packages"
        for node in ast.walk(acquire)
    )


def test_windows_quarantine_commit_read_and_promotion_are_handle_relative() -> None:
    tree = _tree(RUNTIME / "artifact_evidence.py")
    constructor = next(_functions(tree, "__init__"))
    constructor_calls = {
        _dotted(node.func) for node in ast.walk(constructor) if isinstance(node, ast.Call)
    }
    assert "self._handle_io.ensure_directory_chain" in constructor_calls

    expected = {
        "_commit_windows": "self._handle_io.write_new_tree",
        "_load_windows": "self._handle_io.read_bounded_tree",
        "_promote_windows": "self._handle_io.move_dir_no_replace",
    }
    forbidden = {"os.replace", "os.walk", "Path.exists", "Path.read_text"}
    for function_name, required_call in expected.items():
        function = next(_functions(tree, function_name))
        calls = {
            _dotted(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)
        }
        assert required_call in calls
        assert calls.isdisjoint(forbidden)


def test_current_lifecycle_path_only_activates_pure_application_skills() -> None:
    """Plugin, MCP and Hook compatibility remains detected/reviewed, not activated."""

    installation = _tree(RUNTIME / "installation.py")
    materializer = _tree(RUNTIME / "skill_materializer.py")
    pure_property = next(
        node for node in ast.walk(installation)
        if isinstance(node, ast.FunctionDef) and node.name == "is_pure_application_skill"
    )
    constants = {node.value for node in ast.walk(pure_property) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert constants == {"application_skill_import"}

    guarded = [
        node for node in ast.walk(materializer)
        if isinstance(node, ast.Attribute) and node.attr == "is_pure_application_skill"
    ]
    assert len(guarded) >= 2  # lifecycle execution and active-source projection


def test_uninstall_is_a_tombstoned_queryable_effect_with_handle_relative_quarantine() -> None:
    terminal = _tree(RUNTIME / "terminal_receipts.py")
    contracts = next(
        node for node in terminal.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_CONTRACTS" for target in node.targets)
    )
    assert any(
        isinstance(node, ast.Constant) and node.value == "external_extension_uninstall"
        for node in ast.walk(contracts)
    )

    lifecycle = _tree(LIFECYCLE_COMMANDS)
    ensure = next(_functions(lifecycle, "_ensure_intent"))
    calls = [
        _dotted(node.func) for node in ast.walk(ensure) if isinstance(node, ast.Call)
    ]
    assert "self._installations.plan_uninstall_in_uow" in calls
    assert "self._runtime.log.plan_v2_in_connection" in calls

    materializer = _tree(RUNTIME / "skill_materializer.py")
    quarantine = next(_functions(materializer, "_quarantine_uninstalled_tree"))
    quarantine_calls = {
        _dotted(node.func) for node in ast.walk(quarantine) if isinstance(node, ast.Call)
    }
    assert "io.move_dir_no_replace" in quarantine_calls
    assert "shutil.rmtree" not in quarantine_calls

    probe = next(_functions(materializer, "_probe_uninstall"))
    constants = {
        node.value for node in ast.walk(probe)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "error:external-extension-uninstall-owned-tree-missing" in constants
    assert any(
        isinstance(node, ast.Attribute)
        and _dotted(node) == "EffectState.UNKNOWN"
        for node in ast.walk(probe)
    )
