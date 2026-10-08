"""The moved composition and legacy imports are one runtime authority."""
import importlib
import pytest

NAMES=('ai_runtime','ai_execution_control','agent_coordinator',
       'agent_runtime_composition','agent_organization_runtime')

@pytest.mark.parametrize('name',NAMES)
def test_legacy_and_product_composition_are_the_same_module(name):
    product=importlib.import_module('backend.memory_app.kernel.'+name)
    legacy=importlib.import_module('backend.api.'+name)
    assert legacy is product

def test_legacy_private_helpers_and_monkeypatch_target_real_globals(monkeypatch):
    product=importlib.import_module('backend.memory_app.kernel.ai_runtime')
    legacy=importlib.import_module('backend.api.ai_runtime')
    marker=object()
    monkeypatch.setattr(legacy,'resolve_model_gateway_runtime',marker)
    assert product.build_ai_runtime.__globals__['resolve_model_gateway_runtime'] is marker
    coordinator=importlib.import_module('backend.memory_app.kernel.agent_coordinator')
    assert coordinator._child_run is importlib.import_module('backend.api.agent_coordinator')._child_run
    assert coordinator._observed_usage is importlib.import_module('backend.api.agent_coordinator')._observed_usage

def test_new_source_layout_owns_its_venv_assets(tmp_path):
    from backend.api.ai_runtime import resolve_runtime_asset_root
    module=tmp_path/'src/backend/memory_app/kernel/ai_runtime.py'
    module.parent.mkdir(parents=True);module.write_text('# synthetic moved module')
    config=tmp_path/'config';config.mkdir();(config/'codex-hooks.toml').write_text('[hooks]\nenabled = false\n')
    executable=tmp_path/'.venv/Scripts/python.exe'
    executable.parent.mkdir(parents=True);executable.write_text('synthetic executable')
    assert resolve_runtime_asset_root(module).runtime_executable==executable
