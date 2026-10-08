from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .protocols import ContextGraphExporter, ContextGraphImporter

if TYPE_CHECKING:
    from .capability_loader import CapabilityPackageLoader


class ContextGraphAdapterRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContextGraphImportRegistration:
    """Active package identity bound to one external source type."""

    source_type: str
    contribution_id: str
    capability_id: str
    capability_revision: str
    importer: ContextGraphImporter

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.source_type,
                self.contribution_id,
                self.capability_id,
                self.capability_revision,
            )
        ) or not isinstance(self.importer, ContextGraphImporter):
            raise ContextGraphAdapterRegistryError("invalid_importer_registration")


class ContextGraphAdapterRegistry:
    def __init__(self) -> None:
        self._importers: dict[str, ContextGraphImporter] = {}
        self._importer_registrations: dict[str, ContextGraphImportRegistration] = {}
        self._exporters: dict[str, ContextGraphExporter] = {}
        self._authority_loader: CapabilityPackageLoader | None = None

    def register_importer(
        self,
        source_type: str,
        importer: ContextGraphImporter,
        *,
        registration: ContextGraphImportRegistration | None = None,
    ) -> None:
        if not source_type or source_type in self._importers or not isinstance(importer, ContextGraphImporter):
            raise ContextGraphAdapterRegistryError("invalid_or_duplicate_importer")
        if registration is not None and (
            not isinstance(registration, ContextGraphImportRegistration)
            or registration.source_type != source_type
            or registration.importer is not importer
        ):
            raise ContextGraphAdapterRegistryError("invalid_importer_registration")
        self._importers[source_type] = importer
        if registration is not None:
            self._importer_registrations[source_type] = registration

    def register_exporter(self, source_type: str, exporter: ContextGraphExporter) -> None:
        if not source_type or source_type in self._exporters or not isinstance(exporter, ContextGraphExporter):
            raise ContextGraphAdapterRegistryError("invalid_or_duplicate_exporter")
        self._exporters[source_type] = exporter

    def importer(self, source_type: str) -> ContextGraphImporter:
        try:
            return self._importers[source_type]
        except KeyError as exc:
            raise ContextGraphAdapterRegistryError("importer_not_registered") from exc

    def resolve(self, source_type: str) -> ContextGraphImportRegistration | None:
        """Return active package authority for production import composition."""

        registration = self._importer_registrations.get(source_type)
        if registration is None:
            return None
        if self._authority_loader is not None:
            active = [
                manifest for manifest in self._authority_loader.active()
                if manifest.capability_id == registration.capability_id
                and manifest.capability_revision == registration.capability_revision
            ]
            if len(active) != 1:
                return None
        return registration

    def exporter(self, source_type: str) -> ContextGraphExporter:
        try:
            return self._exporters[source_type]
        except KeyError as exc:
            raise ContextGraphAdapterRegistryError("exporter_not_registered") from exc

    def registered(self) -> dict[str, tuple[str, ...]]:
        return {"importers": tuple(sorted(self._importers)), "exporters": tuple(sorted(self._exporters))}

    @classmethod
    def from_active_capability_contributions(
        cls,
        loader: CapabilityPackageLoader,
        context_contributions: Mapping[str, Callable[..., object]],
    ) -> ContextGraphAdapterRegistry:
        """Build the Core registry solely from active package declarations.

        Packages declare their external ``source_type``; Core only maps that
        declaration to a verified ``ContextGraphImporter`` contribution.  The
        method deliberately has no knowledge of package or file-format names.
        """

        registry = cls()
        registry._authority_loader = loader
        for manifest in loader.active():
            for declaration in manifest.context:
                if declaration["kind"] != "importer":
                    continue
                contribution_id = declaration["id"]
                source_type = declaration.get("source_type")
                if source_type is None:
                    # Persisted pre-registry revisions remain readable so Core
                    # can reconcile them, but they never gain an inferred
                    # format identity or a production import entrypoint.
                    continue
                if not isinstance(contribution_id, str) or not isinstance(source_type, str):
                    raise ContextGraphAdapterRegistryError("invalid_importer_declaration")
                factory = context_contributions.get(contribution_id)
                if factory is None:
                    raise ContextGraphAdapterRegistryError("importer_contribution_missing")
                try:
                    importer = factory()
                except Exception as exc:
                    raise ContextGraphAdapterRegistryError("importer_contribution_unavailable") from exc
                registration = ContextGraphImportRegistration(
                    source_type=source_type,
                    contribution_id=contribution_id,
                    capability_id=manifest.capability_id,
                    capability_revision=manifest.capability_revision,
                    importer=importer,  # type: ignore[arg-type]
                )
                registry.register_importer(
                    source_type, registration.importer, registration=registration,
                )
        return registry
