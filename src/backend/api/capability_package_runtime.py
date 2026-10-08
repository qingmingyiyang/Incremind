from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from collections.abc import Callable, Mapping
from types import ModuleType

from core.context_graph import (
    CapabilityPackageError,
    CapabilityPackageLoader,
    CapabilityPackageManifest,
    ContextGraphAdapterRegistry,
)


class CapabilityPackageCatalogConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityPackageContributionCatalog:
    """Immutable registrations compiled from active manifests, without execution state."""

    tools: Mapping[str, Callable[..., object]]
    workflows: Mapping[str, Callable[..., object]]
    context: Mapping[str, Callable[..., object]]


def compose_capability_package_catalog(runtime_root: Path) -> CapabilityPackageLoader:
    """Reconcile bundled declarations into the Core-owned durable active pointer."""

    from core import capability_packages

    packages_root = Path(capability_packages.__file__).resolve(strict=True).parent
    loader = CapabilityPackageLoader(
        Path(runtime_root) / ".rebuild-data" / "capability-packages.sqlite3",
        trusted_packages_root=packages_root,
    )
    reconcile_bundled_capability_packages(loader, packages_root)
    return loader


def compile_capability_package_contributions(
    loader: CapabilityPackageLoader,
) -> CapabilityPackageContributionCatalog:
    tools: dict[str, Callable[..., object]] = {}
    workflows: dict[str, Callable[..., object]] = {}
    context: dict[str, Callable[..., object]] = {}
    for manifest in loader.active():
        for declaration in manifest.tools:
            _register_entrypoint(
                tools, str(declaration["id"]), manifest.capability_id,
                str(declaration["handler"]), loader, manifest,
            )
        for declaration in manifest.workflows:
            _register_entrypoint(
                workflows, str(declaration["id"]), manifest.capability_id,
                str(declaration["decider"]), loader, manifest,
            )
        for declaration in manifest.context:
            _register_entrypoint(
                context, str(declaration["id"]), manifest.capability_id,
                str(declaration["entrypoint"]), loader, manifest,
            )
    return CapabilityPackageContributionCatalog(dict(tools), dict(workflows), dict(context))


def compile_context_graph_adapter_registry(
    loader: CapabilityPackageLoader,
    contributions: CapabilityPackageContributionCatalog | None = None,
) -> ContextGraphAdapterRegistry:
    """Compile active importer declarations into the platform-owned registry."""

    catalog = contributions or compile_capability_package_contributions(loader)
    return ContextGraphAdapterRegistry.from_active_capability_contributions(
        loader, catalog.context,
    )


def reconcile_bundled_capability_packages(
    loader: CapabilityPackageLoader, packages_root: Path,
) -> tuple[CapabilityPackageManifest, ...]:
    discovered = {item.capability_id: item for item in loader.discover(packages_root)}
    active = {item.capability_id: item for item in loader.active()}

    try:
        for capability_id in sorted(set(active) - set(discovered)):
            current = active[capability_id]
            loader.detach_missing_bundle(capability_id, expected_revision=current.capability_revision)
        for capability_id in sorted(discovered):
            manifest = discovered[capability_id]
            if loader.is_disabled(capability_id):
                continue
            current = active.get(capability_id)
            if current is None:
                loader.install(manifest)
                continue
            # Discovery describes the live bundle declaration only.  The
            # durable active record additionally binds that declaration to a
            # verified immutable artifact.  Artifact identity is therefore
            # expected to differ here and must not be mistaken for an
            # undeclared same-revision manifest change.
            if replace(current, artifact_id="") == manifest:
                continue
            current_key = _revision_key(current.capability_revision)
            desired_key = _revision_key(manifest.capability_revision)
            if desired_key > current_key:
                loader.upgrade(manifest, expected_revision=current.capability_revision)
                continue
            if desired_key < current_key:
                restored = current
                while _revision_key(restored.capability_revision) > desired_key:
                    restored = loader.rollback(
                        capability_id, expected_revision=restored.capability_revision,
                    )
                if replace(restored, artifact_id="") != manifest:
                    raise CapabilityPackageCatalogConflict(
                        f"capability rollback target drifted: {capability_id}"
                    )
                continue
            raise CapabilityPackageCatalogConflict(
                f"capability manifest changed without revision: {capability_id}"
            )
    except CapabilityPackageError as exc:
        # A second sidecar may have won the same pointer CAS. Refreshing is
        # sufficient only when the durable end state exactly matches discovery.
        refreshed = {item.capability_id: item for item in loader.refresh()}
        expected = {
            capability_id: replace(manifest, artifact_id="")
            for capability_id, manifest in discovered.items()
            if not loader.is_disabled(capability_id)
        }
        if {key: replace(value, artifact_id="") for key, value in refreshed.items()} == expected:
            return loader.active()
        raise CapabilityPackageCatalogConflict(str(exc)) from exc
    return loader.active()


def _revision_key(revision: str) -> tuple[int, int, int]:
    try:
        major, minor, patch = revision.split(".")
        return int(major), int(minor), int(patch)
    except (TypeError, ValueError) as exc:
        raise CapabilityPackageCatalogConflict("invalid capability revision") from exc


def _register_entrypoint(
    target: dict[str, Callable[..., object]], contribution_id: str,
    capability_id: str, entrypoint: str, loader: CapabilityPackageLoader,
    manifest: CapabilityPackageManifest,
) -> None:
    if contribution_id in target:
        raise CapabilityPackageCatalogConflict(
            f"duplicate capability contribution: {contribution_id}"
        )
    try:
        module_path, symbol = entrypoint.split(":", 1)
        module_name = _entrypoint_module_name(module_path)
        if not symbol.isidentifier() or symbol.startswith("_"):
            raise ValueError("invalid entrypoint symbol")
    except ValueError as exc:
        raise CapabilityPackageCatalogConflict("invalid capability artifact entrypoint") from exc
    try:
        artifact_root = loader.artifact_root(manifest)
    except CapabilityPackageError as exc:
        raise CapabilityPackageCatalogConflict(str(exc)) from exc
    isolated_root = f"core.capability_packages._artifact_{capability_id}_{manifest.artifact_id[:16]}"
    module = _load_artifact_module(isolated_root, artifact_root, module_name)
    contribution = getattr(module, symbol, None)
    if not callable(contribution):
        raise CapabilityPackageCatalogConflict(
            f"capability contribution is not callable: {contribution_id}"
        )
    target[contribution_id] = contribution


def _load_artifact_module(root_name: str, root: Path, module_name: str):
    """Load an artifact under an isolated namespace, never from live bundle source."""
    if not root.is_dir():
        raise CapabilityPackageCatalogConflict("capability artifact entrypoint missing")
    _ensure_package(root_name, root)
    cursor = root
    parent_name = root_name
    parts = module_name.split(".")
    for part in parts[:-1]:
        cursor = cursor / part
        parent_name = f"{parent_name}.{part}"
        if not cursor.is_dir():
            raise CapabilityPackageCatalogConflict("capability artifact entrypoint missing")
        _ensure_package(parent_name, cursor)
    full_name = f"{root_name}.{module_name}"
    path = root.joinpath(*parts).with_suffix(".py")
    if not path.is_file() or path.is_symlink():
        raise CapabilityPackageCatalogConflict("capability artifact entrypoint missing")
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise CapabilityPackageCatalogConflict("capability artifact entrypoint unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(full_name, None)
        raise CapabilityPackageCatalogConflict("capability artifact entrypoint unavailable") from exc
    return module


def _ensure_package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    init = path / "__init__.py"
    if init.is_file():
        spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[str(path)])
        if spec is None or spec.loader is None:
            raise CapabilityPackageCatalogConflict("capability artifact package unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(name, None)
            raise CapabilityPackageCatalogConflict("capability artifact package unavailable") from exc
        return
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module


def _entrypoint_module_name(module_path: str) -> str:
    if not module_path.endswith(".py") or module_path.startswith(("/", "\\")):
        raise ValueError("invalid module path")
    raw_parts = module_path.removesuffix(".py").replace("\\", "/").split("/")
    if not raw_parts or any(not part.isidentifier() or part.startswith("_") and part != "__init__" for part in raw_parts):
        raise ValueError("invalid module path")
    return ".".join(raw_parts)
