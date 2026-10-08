from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from .models import ContextBinding


class ProposalRouteError(ValueError):
    pass


@runtime_checkable
class ProposalAdapter(Protocol):
    def create(self, binding: ContextBinding, *, project_id: str, title: str, content: str, **options: object) -> object: ...


class ProposalRouter:
    """Format-neutral proposal adapter registry; never writes authorities."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProposalAdapter] = {}

    def register(self, output_type: str, adapter: ProposalAdapter) -> None:
        if not output_type or output_type in self._adapters or not isinstance(adapter, ProposalAdapter):
            raise ProposalRouteError("invalid_or_duplicate_proposal_adapter")
        self._adapters[output_type] = adapter

    def route(self, output_type: str, binding: ContextBinding, *, project_id: str, title: str, content: str, options: Mapping[str, object] | None = None) -> object:
        adapter = self._adapters.get(output_type)
        if adapter is None:
            raise ProposalRouteError("proposal_adapter_not_registered")
        return adapter.create(binding, project_id=project_id, title=title, content=content, **dict(options or {}))

    def registered_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
