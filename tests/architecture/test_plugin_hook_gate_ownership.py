from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_plugin_hook_is_a_local_gate_evaluator_without_private_recovery_authority() -> None:
    source = (ROOT / "src" / "backend" / "api" / "plugin_hook_runtime.py").read_text(encoding="utf-8")

    assert 'artifact.effect != "read"' in source
    assert 'artifact.operation_semantics != "read_only"' in source
    assert "artifact.requested_resources" in source
    assert "_PluginHookLifecycle" not in source
    assert "_recover_plugin_hook_lifecycles" not in source
    assert "plugin-hook-lifecycles.sqlite3" not in source
    assert "cleanup_known_identity" in source


def test_plugin_hook_runtime_does_not_import_execution_or_policy_authorities() -> None:
    source = (ROOT / "src" / "backend" / "api" / "plugin_hook_runtime.py").read_text(encoding="utf-8")

    assert "EffectRuntime" not in source
    assert "EffectRunner" not in source
    assert "EffectReaper" not in source
    assert "BoundaryEvaluator" not in source
    assert "SecretStore" not in source
