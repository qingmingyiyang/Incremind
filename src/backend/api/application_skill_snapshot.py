from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json

from core.ai_kernel import TurnPayloadStorePort
from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillCatalogSnapshot,
    ApplicationSkillPackage,
    ApplicationSkillResolver,
    ApplicationSkillSource,
)


class TurnApplicationSkillSnapshotError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TurnApplicationSkillSnapshot:
    payload_ref: str
    revision: str
    payload: Mapping[str, object]


class TurnApplicationSkillSnapshotAuthority:
    """Freeze one project-approved Application Skill selection per AI Turn."""

    consumer = "turn.workbench-question"
    task_kind = "workbench.question.answer"
    agent_child_consumer = "turn.agent-child"
    agent_child_task_kind = "agent.child.execute"
    snapshot_kind = "application-skill-snapshot-v1"

    def __init__(
        self,
        *,
        catalog: ApplicationSkillCatalog,
        sources: tuple[ApplicationSkillSource, ...],
        plugin_sources: Callable[[tuple[str, ...]], tuple[ApplicationSkillSource, ...]] | None = None,
        plugin_claimed_skill_ids: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None,
        external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None,
        external_sources: Callable[[str], tuple[ApplicationSkillSource, ...]] | None = None,
        bindings: ApplicationSkillBindingRegistry,
        resolver: ApplicationSkillResolver,
        payloads: TurnPayloadStorePort,
        agent_binding_verifier: Callable[
            [Mapping[str, object], Mapping[str, object]], Mapping[str, object]
        ] | None = None,
    ) -> None:
        self._catalog = catalog
        self._sources = sources
        self._plugin_sources = plugin_sources
        self._plugin_claimed_skill_ids = plugin_claimed_skill_ids
        self._external_packages = external_packages
        self._external_sources = external_sources
        self._bindings = bindings
        self._resolver = resolver
        self._payloads = payloads
        self._agent_binding_verifier = agent_binding_verifier

    def acquire(
        self,
        request: Mapping[str, object],
        *,
        project_id: str,
        profile_id: str,
        profile_revision: int,
        enabled_skill_ids: tuple[str, ...],
        enabled_sources: tuple[str, ...] = (),
        enabled_plugin_ids: tuple[str, ...] = (),
    ) -> TurnApplicationSkillSnapshot | None:
        selection = self._selection_for_request(request)
        if selection is None:
            return None
        consumer, task_kind, requested_skill_ids = selection
        turn_id = _text(request.get("turn_id"), "turn id")
        existing = self._payloads.get_immutable_payload(turn_id, self.snapshot_kind)
        if existing is not None:
            payload = _validate_snapshot(existing[1])
            _validate_identity(
                payload,
                turn_id=turn_id,
                project_id=project_id,
                profile_id=profile_id,
                profile_revision=profile_revision,
            )
            if payload.get("consumer") != consumer or payload.get("task_kind") != task_kind:
                raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot task authority drifted")
            if requested_skill_ids is not None:
                self._require_requested_selected(payload, requested_skill_ids)
            return TurnApplicationSkillSnapshot(existing[0], str(payload["snapshot_revision"]), payload)

        input_value = request.get("input")
        context_policy = request.get("context_policy")
        if not isinstance(input_value, Mapping) or not isinstance(context_policy, Mapping):
            raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot input is unavailable")
        task_text = _text(input_value.get("text"), "Turn task text")
        context_budget = _integer(context_policy.get("max_context_bytes"), "Turn context budget")
        selected: list[dict[str, object]] = []
        exclusions: dict[str, str] = {}
        catalog_revision = "empty"
        registry_revision = 0
        resolution_revision = _empty_revision(turn_id, project_id, profile_revision, task_text)

        # External extension activation is already a reviewed, Effect-backed
        # project binding.  Requiring the same Skill id to be copied into the
        # capability profile would create a second activation authority and
        # make a successful natural-language install unusable until another
        # manual selection.  Treat the verified active-package provider as the
        # source of external enabled ids; the profile remains authoritative
        # for bundled, user, and plugin Skills.
        external_packages: tuple[ApplicationSkillPackage, ...] = ()
        external_source_count = 0
        if self._external_packages is not None:
            try:
                active = self._external_packages(project_id)
            except Exception as error:
                raise TurnApplicationSkillSnapshotError(
                    "external Application Skill package callback failed"
                ) from error
            external_packages = verified_external_application_skill_packages(active)
            external_source_count = len(
                {package.source_id for package in external_packages}
            )
        external_ids = tuple(package.skill_id for package in external_packages)
        effective_enabled_skill_ids = tuple(
            dict.fromkeys((*enabled_skill_ids, *external_ids))
        )
        if requested_skill_ids is not None:
            requested = set(requested_skill_ids)
            enabled = set(effective_enabled_skill_ids)
            if not requested.issubset(enabled):
                raise TurnApplicationSkillSnapshotError(
                    "agent child requested Skills must be a project-enabled subset"
                )
            effective_enabled_skill_ids = requested_skill_ids

        if effective_enabled_skill_ids:
            plugin_enabled = "plugin" in enabled_sources and bool(enabled_plugin_ids)
            claimed = set()
            if enabled_plugin_ids:
                claimed = set(
                    self._plugin_claimed_skill_ids(enabled_plugin_ids)
                    if self._plugin_claimed_skill_ids is not None
                    else (enabled_skill_ids if plugin_enabled else ())
                )
            profile_base_ids = tuple(
                skill_id for skill_id in enabled_skill_ids if skill_id not in claimed
            )
            # Keep external ids in the filesystem discovery selection too so
            # an equal bundled/user identity is detected as a duplicate rather
            # than silently shadowed.
            base_ids = tuple(dict.fromkeys((*profile_base_ids, *external_ids)))
            plugin_ids = tuple(skill_id for skill_id in enabled_skill_ids if skill_id in claimed)
            empty_catalog = ApplicationSkillCatalogSnapshot((), (), 0)
            external_sources: tuple[ApplicationSkillSource, ...] = ()
            if (
                base_ids
                and self._external_packages is None
                and self._external_sources is not None
            ):
                # Legacy callback retained for non-production callers.  The
                # API runtime uses immutable external_packages exclusively.
                external_sources = self._external_sources(project_id)
            base_catalog = (
                self._catalog.discover_selected(
                    (*self._sources, *external_sources), base_ids,
                )
                if base_ids
                else empty_catalog
            )
            plugin_catalog = (
                self._catalog.discover_selected(self._plugin_sources(enabled_plugin_ids), plugin_ids)
                if plugin_ids and self._plugin_sources is not None
                else empty_catalog
            )
            composed = self._catalog.snapshot_from_packages(
                (*base_catalog.packages, *external_packages, *plugin_catalog.packages),
                scanned_source_count=(
                    base_catalog.scanned_source_count
                    + external_source_count
                    + plugin_catalog.scanned_source_count
                ),
            )
            catalog = ApplicationSkillCatalogSnapshot(
                packages=composed.packages,
                issues=tuple(sorted(
                    (*base_catalog.issues, *plugin_catalog.issues, *composed.issues),
                    key=lambda item: (item.source_id, item.package_name, item.code),
                )),
                scanned_source_count=composed.scanned_source_count,
            )
            catalog_revision = _catalog_revision(catalog.packages)
            status = self._bindings.status(catalog)
            registry_revision = _integer(status.get("registry_revision"), "Skill registry revision", minimum=0)
            resolution = self._resolver.resolve(
                catalog,
                project_id=project_id,
                consumer=consumer,
                task_kind=task_kind,
                task_text=task_text,
                invocation_id=turn_id,
                enabled_skill_ids=effective_enabled_skill_ids,
                max_instruction_bytes=16 * 1024,
                max_context_bytes=min(context_budget, 32 * 1024),
            )
            resolution_revision = resolution.resolution_id
            selected_ids = {item.match.skill_id for item in resolution.selected}
            matched_ids = {item.skill_id for item in resolution.matched}
            budget_ids = set(resolution.budget_excluded_skill_ids)
            binding_status = {
                str(item.get("skill_id")): item
                for item in status.get("bindings", [])
                if isinstance(item, Mapping) and item.get("project_id") == project_id
            }
            for skill_id in effective_enabled_skill_ids:
                if skill_id in selected_ids:
                    continue
                binding = binding_status.get(skill_id)
                effective = binding.get("effective_status") if binding is not None else None
                consumers = binding.get("allowed_consumers") if binding is not None else None
                if binding is None:
                    reason = "missing_binding"
                elif effective == "missing":
                    reason = "missing_package"
                elif effective == "drifted":
                    reason = "fingerprint_drift"
                elif effective != "active" or not isinstance(consumers, list) or consumer not in consumers:
                    reason = "inactive_binding"
                elif skill_id in budget_ids:
                    reason = "budget"
                elif skill_id not in matched_ids:
                    reason = "unmatched"
                else:
                    reason = "not_selected"
                exclusions[skill_id] = reason
            for item in resolution.selected:
                instruction_payload = {
                    "schema_version": "1.0.0",
                    "skill_id": item.match.skill_id,
                    "skill_fingerprint": item.match.skill_fingerprint,
                    "markdown": item.instructions.markdown,
                }
                instruction_ref = self._payloads.get_or_create_immutable_payload(
                    turn_id,
                    f"application-skill-instructions-{item.match.skill_id}",
                    instruction_payload,
                )
                selected.append({
                    "skill_id": item.match.skill_id,
                    "source_id": next(
                        package.source_id for package in catalog.packages
                        if package.skill_id == item.match.skill_id
                    ),
                    "source_kind": next(
                        package.source_kind for package in catalog.packages
                        if package.skill_id == item.match.skill_id
                    ),
                    "skill_fingerprint": item.match.skill_fingerprint,
                    "binding_id": item.match.binding_id,
                    "binding_revision": item.match.binding_revision,
                    "instruction_bytes": item.instructions.size_bytes,
                    "instruction_payload_ref": instruction_ref,
                })

        payload = {
            "schema_version": "1.0.0",
            "turn_id": turn_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "consumer": consumer,
            "task_kind": task_kind,
            "task_fingerprint": hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
            "snapshot_revision": resolution_revision,
            "catalog_revision": catalog_revision,
            "binding_registry_revision": registry_revision,
            "selected": selected,
            "excluded": [
                {"skill_id": skill_id, "reason": exclusions[skill_id]}
                for skill_id in sorted(exclusions)
            ],
            "selected_instruction_bytes": sum(int(item["instruction_bytes"]) for item in selected),
            "context_budget_bytes": min(context_budget, 32 * 1024),
        }
        clean = _validate_snapshot(payload)
        if requested_skill_ids is not None:
            self._require_requested_selected(clean, requested_skill_ids)
        payload_ref = self._payloads.get_or_create_immutable_payload(
            turn_id, self.snapshot_kind, clean
        )
        return TurnApplicationSkillSnapshot(payload_ref, resolution_revision, clean)

    def _selection_for_request(
        self, request: Mapping[str, object],
    ) -> tuple[str, str, tuple[str, ...] | None] | None:
        desired_outcome = request.get("desired_outcome")
        if desired_outcome == self.task_kind:
            return self.consumer, self.task_kind, None
        if desired_outcome != self.agent_child_task_kind:
            return None
        expert_request = request.get("expert_request")
        if not isinstance(expert_request, Mapping) or "skill_ids" not in expert_request:
            return None
        skill_ids = expert_request.get("skill_ids")
        if (
            not isinstance(skill_ids, list)
            or not skill_ids
            or len(skill_ids) != len(set(skill_ids))
            or any(not isinstance(item, str) or not item.strip() for item in skill_ids)
        ):
            raise TurnApplicationSkillSnapshotError("agent child requested Skills are invalid")
        binding = request.get("agent_binding")
        if not isinstance(binding, Mapping) or self._agent_binding_verifier is None:
            raise TurnApplicationSkillSnapshotError("agent child Skill selection requires a verified agent binding")
        try:
            verified = self._agent_binding_verifier(request, binding)
        except Exception as error:
            raise TurnApplicationSkillSnapshotError(
                "agent child Skill binding verification failed"
            ) from error
        if not isinstance(verified, Mapping) or dict(verified) != dict(binding):
            raise TurnApplicationSkillSnapshotError("agent child Skill binding authority drifted")
        return self.agent_child_consumer, self.agent_child_task_kind, tuple(skill_ids)

    @staticmethod
    def _require_requested_selected(
        payload: Mapping[str, object], requested_skill_ids: tuple[str, ...],
    ) -> None:
        selected = payload.get("selected")
        selected_ids = {
            item.get("skill_id") for item in selected
            if isinstance(item, Mapping)
        } if isinstance(selected, list) else set()
        if selected_ids != set(requested_skill_ids) or len(selected_ids) != len(requested_skill_ids):
            raise TurnApplicationSkillSnapshotError(
                "agent child requested Skills were not fully selected"
            )


def _validate_snapshot(value: object) -> dict[str, object]:
    fields = {
        "schema_version", "turn_id", "project_id", "profile_id", "profile_revision",
        "consumer", "task_kind", "task_fingerprint", "snapshot_revision",
        "catalog_revision", "binding_registry_revision", "selected", "excluded",
        "selected_instruction_bytes", "context_budget_bytes",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != "1.0.0":
        raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot shape is invalid")
    payload = json.loads(json.dumps(dict(value), ensure_ascii=False))
    for field in ("turn_id", "project_id", "profile_id", "consumer", "task_kind", "task_fingerprint", "snapshot_revision", "catalog_revision"):
        _text(payload.get(field), f"SkillSnapshot {field}")
    _integer(payload.get("profile_revision"), "SkillSnapshot profile revision")
    _integer(payload.get("binding_registry_revision"), "SkillSnapshot registry revision", minimum=0)
    budget = _integer(payload.get("context_budget_bytes"), "SkillSnapshot context budget")
    selected = payload.get("selected")
    excluded = payload.get("excluded")
    if not isinstance(selected, list) or len(selected) > 3 or not isinstance(excluded, list):
        raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot collections are invalid")
    selected_ids: set[str] = set()
    selected_bytes = 0
    for item in selected:
        if not isinstance(item, Mapping) or set(item) != {
            "skill_id", "source_id", "source_kind", "skill_fingerprint", "binding_id",
            "binding_revision", "instruction_bytes", "instruction_payload_ref",
        }:
            raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot selection is invalid")
        skill_id = _text(item.get("skill_id"), "selected Skill id")
        if skill_id in selected_ids:
            raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot Skill identities drifted")
        selected_ids.add(skill_id)
        for field in ("source_id", "source_kind", "skill_fingerprint", "binding_id"):
            _text(item.get(field), f"selected Skill {field}")
        _integer(item.get("binding_revision"), "selected Skill binding revision")
        instruction_bytes = _integer(item.get("instruction_bytes"), "selected Skill bytes")
        selected_bytes += instruction_bytes
        ref = _text(item.get("instruction_payload_ref"), "selected Skill payload ref")
        if not ref.startswith(f"crp://session/{payload['turn_id']}/"):
            raise TurnApplicationSkillSnapshotError("selected Skill payload crossed Turn identity")
    excluded_ids: set[str] = set()
    for item in excluded:
        if not isinstance(item, Mapping) or set(item) != {"skill_id", "reason"}:
            raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot exclusion is invalid")
        skill_id = _text(item.get("skill_id"), "excluded Skill id")
        _text(item.get("reason"), "excluded Skill reason")
        if skill_id in excluded_ids or skill_id in selected_ids:
            raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot exclusions drifted")
        excluded_ids.add(skill_id)
    if selected_bytes != _integer(payload.get("selected_instruction_bytes"), "selected instruction bytes", minimum=0):
        raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot byte total drifted")
    if selected_bytes > 16 * 1024 or selected_bytes > budget:
        raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot exceeds its budget")
    return payload


def application_skill_snapshot_from_payload(value: object) -> dict[str, object]:
    """Validate an immutable SkillSnapshot before a model-facing consumer uses it."""

    return _validate_snapshot(value)


def _validate_identity(
    payload: Mapping[str, object], *, turn_id: str, project_id: str,
    profile_id: str, profile_revision: int,
) -> None:
    if (
        payload.get("turn_id") != turn_id
        or payload.get("project_id") != project_id
        or payload.get("profile_id") != profile_id
        or payload.get("profile_revision") != profile_revision
    ):
        raise TurnApplicationSkillSnapshotError("Turn SkillSnapshot authority drifted")


def verified_external_application_skill_packages(
    value: object,
) -> tuple[ApplicationSkillPackage, ...]:
    """Reject any external callback result that could reopen a mutable path."""

    if not isinstance(value, tuple):
        raise TurnApplicationSkillSnapshotError(
            "external Application Skill packages must be a tuple"
        )
    packages: list[ApplicationSkillPackage] = []
    for package in value:
        if not isinstance(package, ApplicationSkillPackage):
            raise TurnApplicationSkillSnapshotError(
                "external Application Skill package is invalid"
            )
        if package.source_kind != "external" or package.verified_content is None:
            raise TurnApplicationSkillSnapshotError(
                "external Application Skill package is not immutable"
            )
        packages.append(package)
    return tuple(packages)


def reviewed_external_application_skill_ids(value: object) -> tuple[str, ...]:
    """Return IDs only after the same immutable-package review used by Turns."""

    return tuple(
        package.skill_id
        for package in verified_external_application_skill_packages(value)
    )


def _catalog_revision(packages: object) -> str:
    identities = [
        [item.skill_id, item.source_id, item.source_kind, item.fingerprint]
        for item in packages  # type: ignore[union-attr]
    ]
    return hashlib.sha256(
        json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _empty_revision(turn_id: str, project_id: str, profile_revision: int, task_text: str) -> str:
    identity = f"{turn_id}\0{project_id}\0{profile_revision}\0{task_text}"
    return f"skill-resolution-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TurnApplicationSkillSnapshotError(f"{label} must be non-empty")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise TurnApplicationSkillSnapshotError(f"{label} is invalid")
    return value
