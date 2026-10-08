"""Pure policy implementations cannot reach product infrastructure."""
import ast
import sys

from tests.architecture.test_model_calls_through_kernel import ROOT, BOUNDARY_EVIDENCE, verified_boundaries


def test_policy_modules_import_only_their_own_package_or_standard_library():
    directory = ROOT / 'src/backend/memory_app/v2/policies'
    forbidden = []
    for path in directory.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    continue
                modules = [node.module or '']
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            forbidden.extend((path.name, module) for module in modules
                if module.split('.')[0] not in sys.stdlib_module_names)
    assert forbidden == []


def test_product_runtime_evidence_requires_real_core_inheritance_and_forwarding():
    runtime_path = 'backend/memory_app/kernel/policy_runtime.py'
    affected = {key for key, evidence in BOUNDARY_EVIDENCE.items()
        if any(path == runtime_path for path, _ in evidence)}
    assert affected
    original = (ROOT / 'src' / runtime_path).read_text(encoding='utf-8-sig')
    for before, after in [
        ('from core.ai_kernel import SynchronousAIRuntime', 'from elsewhere import SynchronousAIRuntime'),
        ('class ProductPolicyRuntime(SynchronousAIRuntime)', 'class ProductPolicyRuntime(object)'),
        ('super().run_accepted_turn', 'unrelated_run'),
    ]:
        def source(path):
            return original.replace(before, after) if path == runtime_path else (ROOT / 'src' / path).read_text(encoding='utf-8-sig')
        verified = verified_boundaries(source)
        assert affected.isdisjoint(verified)
