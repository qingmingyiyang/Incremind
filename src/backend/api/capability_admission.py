"""Governed admission for capabilities composed into the AI runtime.

The registry intentionally remains the one dispatch registry.  This module
answers a different question: where a *new* registration is allowed to come
from.  The static Core set is a reviewed compatibility inventory; extensions
must prove one active CapabilityPackage contribution before they can enter the
same registry.  It does not create another runner, gate, secret, memory, or
recovery authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Protocol

from core.ai_kernel import CapabilityDefinition
from core.ai_kernel.ports import (
    CapabilityProviderPort,
    CapabilityRegistrationPort,
    CapabilityRegistryPort,
)


class CapabilityAdmissionError(ValueError):
    """Raised before an unreviewed capability can reach the dispatch registry."""


@dataclass(frozen=True, slots=True)
class CoreCapabilityInventoryItem:
    capability_id: str
    version: int
    review_revision: str
    registration_kind: str = "core"


# This is deliberately an explicit review boundary, rather than a discovery
# scan.  Adding a manually-wired capability changes this inventory and its
# architecture test in the same review.
CORE_CAPABILITY_INVENTORY = (
    CoreCapabilityInventoryItem("memory.recall", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("recognition.task.execute", 1, "2026.09.15"),
    CoreCapabilityInventoryItem("recognition.task.execute.local", 1, "2026.09.24"),
    CoreCapabilityInventoryItem("presentation.pptx.fixed", 1, "2026.09.01", "host_managed"),
    CoreCapabilityInventoryItem("analyze_source", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("image.generate", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("memory.candidate.evidence.read", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("series.intake.organize.commit", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("memory.candidate.propose.write", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("workbench.input.classification.context.read", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("developer_studio.test_lab.execute", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("workbench.input.classification.enhance.write", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("workbench.question.answer", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("workbench.answer.execute", 1, "2026.10.02"),
    CoreCapabilityInventoryItem("companion.vision.context.read", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("companion.vision.analyze.write", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("project_skill.evidence.read", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("project_skill.draft.propose", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("source.evidence.read", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("document.draft.propose", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("companion.chat.context.read", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("companion.chat.message.write", 1, "2026.09.01"),
    CoreCapabilityInventoryItem("agent.spawn", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("agent.message", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("agent.interrupt", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("agent.wait", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("agent.fan_in", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("agent.list", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("agent.plan", 1, "2026.09.02"),
    CoreCapabilityInventoryItem("external.context.execute", 1, "2026.10.05"),
    CoreCapabilityInventoryItem("external.task.execute", 1, "2026.10.06"),
)
_CORE_BY_ID = {item.capability_id: item for item in CORE_CAPABILITY_INVENTORY}
if len(_CORE_BY_ID) != len(CORE_CAPABILITY_INVENTORY):  # pragma: no cover - import-time authoring guard
    raise RuntimeError("duplicate Core capability inventory identity")


class ActiveCapabilityPackageCatalog(Protocol):
    def active(self) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class CapabilityPackageContributions:
    tools: Mapping[str, object]


class RuntimeCapabilityAdmission:
    """Admits Core inventory and verified package tools to one existing registry."""

    def __init__(
        self,
        registry: CapabilityRegistryPort,
        *,
        packages: ActiveCapabilityPackageCatalog | None = None,
        contributions: CapabilityPackageContributions | None = None,
    ) -> None:
        self._registry = registry
        self._packages = packages
        self._contributions = contributions

    def register_core(
        self, definition: CapabilityDefinition, provider: CapabilityProviderPort,
    ) -> CapabilityRegistrationPort:
        item = _CORE_BY_ID.get(definition.capability_id)
        if item is None:
            raise CapabilityAdmissionError("capability_not_in_reviewed_core_inventory")
        if definition.version != item.version:
            raise CapabilityAdmissionError("core_capability_version_drift")
        return self._registry.register(definition, provider)

    def register_package_tool(
        self,
        definition: CapabilityDefinition,
        provider: CapabilityProviderPort,
        *,
        package_id: str,
        package_revision: str,
        contribution_id: str | None = None,
    ) -> CapabilityRegistrationPort:
        """Admit one package-declared tool, or fail closed before registration.

        The package loader has already checked that the package only contributes
        through Core-owned Effect, recovery, secret, memory, document and Skill
        boundaries.  This method additionally binds this registration to its
        durable active revision and compiled contribution.
        """
        if definition.capability_id in _CORE_BY_ID:
            raise CapabilityAdmissionError("core_capability_must_use_core_admission")
        if not package_id or not package_revision:
            raise CapabilityAdmissionError("capability_package_identity_required")
        if self._packages is None or self._contributions is None:
            raise CapabilityAdmissionError("capability_package_catalog_unavailable")
        active = tuple(self._packages.active())
        matches = [
            item for item in active
            if getattr(item, "capability_id", None) == package_id
            and getattr(item, "capability_revision", None) == package_revision
        ]
        if len(matches) != 1:
            raise CapabilityAdmissionError("capability_package_revision_not_active")
        contribution = contribution_id
        declared_tools = getattr(matches[0], "tools", ())
        matching_declarations = tuple(
            item for item in declared_tools
            if isinstance(item, Mapping) and item.get("id") == definition.capability_id
        )
        if len(matching_declarations) != 1:
            raise CapabilityAdmissionError("capability_package_tool_not_declared")
        declared_contribution = matching_declarations[0].get("contributes")
        if contribution is not None and contribution != declared_contribution:
            raise CapabilityAdmissionError("capability_package_contribution_mismatch")
        if definition.capability_id not in self._contributions.tools:
            raise CapabilityAdmissionError("capability_package_contribution_unavailable")
        return self._registry.register(definition, provider)


class ReviewedCoreCapabilityRegistry:
    """Narrow registration view used by static AI-runtime composition only.

    It deliberately exposes no resolver or dispatch methods.  Existing dynamic
    MCP and reviewed-plugin managers continue to receive the original registry
    and keep their already-established activation authorities; this view stops
    future static ``ai_runtime`` additions from silently becoming extensions.
    """

    def __init__(self, admission: RuntimeCapabilityAdmission) -> None:
        self._admission = admission

    def register(
        self, definition: CapabilityDefinition, provider: CapabilityProviderPort,
    ) -> CapabilityRegistrationPort:
        return self.register_core(definition, provider)

    def register_core(
        self, definition: CapabilityDefinition, provider: CapabilityProviderPort,
    ) -> CapabilityRegistrationPort:
        return self._admission.register_core(definition, provider)


def reviewed_core_capability_ids() -> tuple[str, ...]:
    return tuple(item.capability_id for item in CORE_CAPABILITY_INVENTORY)
