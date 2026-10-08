from __future__ import annotations

import asyncio
import ast
import json
import symtable
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.routes import rebuild
from backend.api.routes.product import router, developer_test_lab, model_routes, providers


ROOT = Path(__file__).resolve().parents[4]
PRODUCT = ROOT / "src/backend/api/routes/product"


def test_product_routes_preserve_existing_client_contract_and_order() -> None:
    expected = json.loads((ROOT / "tests/fixtures/product-route-contract.json").read_text(encoding="utf-8"))
    actual = [
        dict(path=route.path, methods=sorted(route.methods), name=route.name,
             operation_id=route.operation_id, unique_id=route.unique_id, tags=route.tags,
             status_code=route.status_code, deprecated=route.deprecated)
        for route in router.routes
    ]
    assert actual == expected
    assert rebuild.router is router
    assert all(route.endpoint.__module__.startswith("backend.api.routes.product.") for route in router.routes)


def test_product_domain_dependencies_are_acyclic_and_aliases_never_shadowed() -> None:
    graph = {}
    for path in PRODUCT.glob("*.py"):
        if path.stem == "__init__":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.level]
        graph[path.stem] = {alias.name for node in imports if node.module is None for alias in node.names}
        aliases = {alias.asname or alias.name for node in imports for alias in node.names}
        scopes = list(symtable.symtable(source, str(path), "exec").get_children())
        while scopes:
            scope = scopes.pop()
            assert not {symbol.get_name() for symbol in scope.get_symbols() if symbol.is_local()} & aliases, (path, scope.get_name())
            scopes.extend(scope.get_children())
        assert all(alias.name != "*" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names)

    visited = set()

    def visit(domain: str, active: frozenset[str]) -> None:
        assert domain not in active, (domain, active)
        if domain in visited:
            return
        for dependency in graph[domain]:
            visit(dependency, active | {domain})
        visited.add(domain)

    for domain in graph:
        visit(domain, frozenset())


@pytest.mark.parametrize("operation", ["preview", "activate", "test_lab"])
def test_model_route_provider_context_is_not_shadowed_by_local_result(tmp_path, monkeypatch, operation) -> None:
    """Exercise all three original local `providers` bindings without network I/O."""
    class ContextReached(RuntimeError):
        pass

    def contexts(container):
        assert container.root_dir == tmp_path
        raise ContextReached

    monkeypatch.setattr(providers, "_model_route_runtime_contexts", contexts)
    container = SimpleNamespace(root_dir=tmp_path)
    if operation == "test_lab":
        monkeypatch.setattr(developer_test_lab, "ModelRouteRuntimeService", lambda _: SimpleNamespace(status=lambda: {"runtime_revision": 0}))
        with pytest.raises(ContextReached):
            developer_test_lab._test_lab_route_resolution(container, route_key="intake.classification", expected_runtime_revision=0)
    else:
        class Request:
            method = "POST"

            async def json(self):
                return {"expected_runtime_revision": 0}

        handler = getattr(model_routes, "model_route_runtime_" + operation)
        with pytest.raises(ContextReached):
            asyncio.run(handler(Request(), container))
