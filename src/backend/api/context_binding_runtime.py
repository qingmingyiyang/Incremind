from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
from pathlib import Path
from typing import Protocol

from core.ai_kernel import TurnPayloadStorePort
from core.ai_kernel.external_agent_context import (
    ExternalAgentContextError,
    validate_external_agent_safe_projection,
)
from core.context_graph import (
    ContextBinding,
    FrozenContextRevisions,
    context_binding_from_payload,
    context_binding_to_payload,
)
from core.storage_provider import JsonObjectStore
from backend.security.user_context import json_attribution


class ContextBindingRegistryError(ValueError):
    pass


class ContextBindingFactReader(Protocol):
    """Read the durable compilation fact required to consume one binding."""

    def binding_fact(
        self, project_id: str, graph_id: str, graph_revision: str,
        binding_id: str, registry_revision: int,
        revisions: FrozenContextRevisions,
    ) -> bool: ...


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_COLLECTION = "context_bindings"


@dataclass(frozen=True, slots=True)
class TurnContextBindingSnapshot:
    binding_id: str
    project_id: str
    capability_id: str
    capability_revision: str
    registry_revision: int
    payload_ref: str
    binding: ContextBinding


class ContextBindingRegistry:
    """Core-owned, provider-neutral immutable registry for compiled bindings."""

    def __init__(self, root_dir: Path, *, namespace_id: str = "default") -> None:
        root = Path(root_dir)
        self._store = JsonObjectStore(
            root / ".rebuild-data",
            legacy_root=root / "library",
            namespace_id=namespace_id,
            mutation_attribution=json_attribution(root, namespace_id),
        )

    def create(
        self, *, binding_id: str, project_id: str, capability_id: str,
        capability_revision: str, binding: ContextBinding,
        expected_revision: int,
    ) -> Mapping[str, object]:
        _identity(binding_id, "binding id")
        _identity(project_id, "project id")
        _identity(capability_id, "capability id")
        if binding.capability_revision != capability_revision:
            raise ContextBindingRegistryError("ContextBinding capability revision drifted")
        if expected_revision != 0:
            raise ContextBindingRegistryError("ContextBinding creation requires expected_revision=0")
        record = {
            "schema_version": "1.0.0",
            "binding_id": binding_id,
            "project_id": project_id,
            "capability_id": capability_id,
            "capability_revision": capability_revision,
            "binding": context_binding_to_payload(binding),
        }
        try:
            validate_external_agent_safe_projection(record)
        except ExternalAgentContextError as error:
            raise ContextBindingRegistryError(
                "ContextBinding contains unsafe content"
            ) from error
        try:
            revision = self._store.write(
                _COLLECTION, binding_id, record, expected_revision=0,
            )
        except ValueError as error:
            raise ContextBindingRegistryError("ContextBinding identity already exists") from error
        return {
            **record,
            "registry_revision": revision,
            "binding_ref": _binding_ref(project_id, binding_id),
        }

    def resolve(self, binding_ref: str, *, project_id: str) -> TurnContextBindingSnapshot:
        ref_project_id, binding_id = _parse_binding_ref(binding_ref)
        if ref_project_id != project_id:
            raise ContextBindingRegistryError("ContextBinding project scope drifted")
        record = self._store.read(_COLLECTION, binding_id)
        if not isinstance(record, Mapping) or set(record) != {
            "schema_version", "binding_id", "project_id", "capability_id",
            "capability_revision", "binding",
        }:
            raise ContextBindingRegistryError("ContextBinding record is unavailable")
        if (
            record.get("schema_version") != "1.0.0"
            or record.get("binding_id") != binding_id
            or record.get("project_id") != project_id
        ):
            raise ContextBindingRegistryError("ContextBinding registry identity drifted")
        try:
            validate_external_agent_safe_projection(record)
        except ExternalAgentContextError as error:
            raise ContextBindingRegistryError(
                "ContextBinding stored content is unsafe"
            ) from error
        return TurnContextBindingSnapshot(
            binding_id=binding_id,
            project_id=project_id,
            capability_id=_identity(record.get("capability_id"), "capability id"),
            capability_revision=str(record.get("capability_revision") or ""),
            registry_revision=self._store.revision(_COLLECTION, binding_id),
            payload_ref=binding_ref,
            binding=context_binding_from_payload(record.get("binding")),
        )


class TurnContextBindingSnapshotAuthority:
    """Copies one selected immutable binding into the accepted Turn scope."""

    def __init__(
        self, registry: ContextBindingRegistry, payloads: TurnPayloadStorePort,
        capability_revision_reader: Callable[[str], str | None],
        compilation_facts: ContextBindingFactReader,
    ) -> None:
        self._registry = registry
        self._payloads = payloads
        self._capability_revision_reader = capability_revision_reader
        self._compilation_facts = compilation_facts

    def acquire(
        self, request: Mapping[str, object], *, project_id: str,
    ) -> TurnContextBindingSnapshot | None:
        turn_input = request.get("input")
        refs = turn_input.get("refs") if isinstance(turn_input, Mapping) else None
        if not isinstance(refs, list):
            raise ContextBindingRegistryError("Turn input refs are unavailable")
        selected = [
            item for item in refs
            if isinstance(item, Mapping) and item.get("kind") == "context_binding"
        ]
        if not selected:
            return None
        if len(selected) != 1:
            raise ContextBindingRegistryError("Turn must select exactly one ContextBinding")
        value = selected[0]
        if set(value) != {"kind", "object_id", "uri"}:
            raise ContextBindingRegistryError("ContextBinding Turn ref shape is invalid")
        snapshot = self._registry.resolve(str(value.get("uri") or ""), project_id=project_id)
        if value.get("object_id") != snapshot.binding_id:
            raise ContextBindingRegistryError("ContextBinding Turn ref identity drifted")
        current_revision = self._capability_revision_reader(snapshot.capability_id)
        if (
            current_revision != snapshot.capability_revision
            or snapshot.binding.capability_revision != snapshot.capability_revision
        ):
            raise ContextBindingRegistryError("ContextBinding Capability revision drifted")
        binding = snapshot.binding
        try:
            recorded = self._compilation_facts.binding_fact(
                snapshot.project_id,
                binding.graph_id,
                binding.graph_revision,
                snapshot.binding_id,
                snapshot.registry_revision,
                FrozenContextRevisions(
                    capability_revision=binding.capability_revision,
                    boundary_revision=binding.boundary_revision,
                    provider_revision=binding.provider_revision,
                    model_route_revision=binding.model_route_revision,
                    compiler_revision=binding.compiler_revision,
                ),
            )
        except (TypeError, ValueError) as error:
            raise ContextBindingRegistryError(
                "ContextBinding compilation fact is invalid"
            ) from error
        if recorded is not True:
            raise ContextBindingRegistryError(
                "ContextBinding compilation fact is unavailable"
            )
        turn_id = str(request.get("turn_id") or "")
        payload_ref = self._payloads.get_or_create_immutable_payload(
            turn_id,
            "context-binding-v1",
            {
                "schema_version": "1.0.0",
                "binding_id": snapshot.binding_id,
                "project_id": snapshot.project_id,
                "capability_id": snapshot.capability_id,
                "capability_revision": snapshot.capability_revision,
                "registry_revision": snapshot.registry_revision,
                "binding": context_binding_to_payload(snapshot.binding),
            },
        )
        return TurnContextBindingSnapshot(
            snapshot.binding_id, snapshot.project_id, snapshot.capability_id,
            snapshot.capability_revision, snapshot.registry_revision, payload_ref,
            snapshot.binding,
        )


def _binding_ref(project_id: str, binding_id: str) -> str:
    return f"crp://context-bindings/{project_id}/{binding_id}"


def _parse_binding_ref(value: str) -> tuple[str, str]:
    prefix = "crp://context-bindings/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ContextBindingRegistryError("ContextBinding ref is invalid")
    parts = value[len(prefix):].split("/")
    if len(parts) != 2:
        raise ContextBindingRegistryError("ContextBinding ref is invalid")
    return _identity(parts[0], "project id"), _identity(parts[1], "binding id")


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContextBindingRegistryError(f"{label} is invalid")
    return value
