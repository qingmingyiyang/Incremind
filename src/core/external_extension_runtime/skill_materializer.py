"""Safe materialization of reviewed external Application Skills.

This executor deliberately has no database or EffectLog of its own.  The
installation projection is the authority for what may be materialized and the
ApplicationSkillBindingRegistry remains the authority for project enablement.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillError,
    ApplicationSkillPackage,
    ApplicationSkillSource,
    ApplicationSkillVerifiedContent,
)
from core.effect_log import Effect, EffectState

from .fact_store import ExternalExtensionFactStore
from .installation import (
    ExternalExtensionActivationPlan,
    ExternalExtensionInstallationStore,
    ExternalSkillActivationBinding,
)
from .terminal_receipts import (
    ExternalExtensionLifecycleIntent,
    LifecycleOutcome,
    LifecycleProbeOutcome,
)
from .windows_handle_io import WindowsHandleIoError, WindowsHandleTreeIo


class ExternalExtensionSkillMaterializationError(ValueError):
    """The reviewed artifact cannot safely become an Application Skill."""


_RESOURCE_GROUPS = frozenset({"agents", "references", "scripts", "assets"})
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *{f"COM{index}" for index in range(1, 10)}, *{f"LPT{index}" for index in range(1, 10)}}
)


class ExternalExtensionApplicationSkillMaterializer:
    """Lifecycle executor for pure reviewed Application Skill extensions.

    ``binding_registry_factory`` is useful for dependency injection.  The
    registry owns the mutation timestamp; the Effect creation time is only a
    causal identity and is never reused as the execution timestamp.
    """

    def __init__(
        self,
        facts: ExternalExtensionFactStore,
        installations: ExternalExtensionInstallationStore,
        managed_root: str | Path,
        *,
        binding_store: object | None = None,
        binding_registry_factory: Callable[..., ApplicationSkillBindingRegistry] | None = None,
    ) -> None:
        if not isinstance(facts, ExternalExtensionFactStore):
            raise TypeError("facts must be an ExternalExtensionFactStore")
        if not isinstance(installations, ExternalExtensionInstallationStore):
            raise TypeError("installations must be an ExternalExtensionInstallationStore")
        root = Path(managed_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ExternalExtensionSkillMaterializationError("managed root is unsafe")
        if root.exists() or root.is_symlink():
            _reject_reparse_path(root)
            if not root.is_dir():
                raise ExternalExtensionSkillMaterializationError("managed root is unsafe")
        self._facts = facts
        self._installations = installations
        self._root = root
        self._binding_store = binding_store
        self._registry_factory = binding_registry_factory or ApplicationSkillBindingRegistry
        if binding_store is None and binding_registry_factory is None:
            raise TypeError("binding_store or binding_registry_factory is required")

    def execute(
        self, intent: ExternalExtensionLifecycleIntent, effect: Effect,
    ) -> LifecycleOutcome:
        if intent.action == "uninstall":
            self._execute_uninstall(intent, effect)
            return LifecycleOutcome()
        plan, packages = self._materialize(intent)
        if intent.action == "health":
            return LifecycleOutcome(passed=True, observed_checks=intent.health_checks)
        registry = self._registry()
        if intent.action in {"activation", "rollback"}:
            self._activate(registry, plan, packages, intent, effect)
        elif intent.action == "disable":
            self._disable(registry, plan, packages, intent, effect)
        else:  # intent itself validates this, retained for defensive callers.
            raise ExternalExtensionSkillMaterializationError("unsupported lifecycle action")
        return LifecycleOutcome()

    def probe(
        self, intent: ExternalExtensionLifecycleIntent, effect: Effect,
    ) -> LifecycleProbeOutcome:
        evidence = self._evidence_ref(effect)
        if intent.action == "uninstall":
            return self._probe_uninstall(intent, effect, evidence)
        try:
            plan = self._validated_plan(intent)
            packages = self._read_materialized(plan, intent)
        except FileNotFoundError:
            return LifecycleProbeOutcome(EffectState.PLANNED, evidence)
        except (ExternalExtensionSkillMaterializationError, ApplicationSkillError, OSError, ValueError):
            return LifecycleProbeOutcome(EffectState.UNKNOWN, "error:external-extension-skill-materialization-drift")
        if intent.action == "health":
            return LifecycleProbeOutcome(EffectState.SETTLED_OK, evidence, True, intent.health_checks)
        try:
            registry = self._registry()
            status = registry.status(self._snapshot(packages))
            bindings = self._exact_bindings(status, plan, packages, intent.root_id)
            if intent.action in {"activation", "rollback"}:
                if all(item["status"] == "active" and item["effective_status"] == "active" for item in bindings):
                    return LifecycleProbeOutcome(EffectState.SETTLED_OK, evidence)
                if any(item["status"] == "active" for item in bindings):
                    return LifecycleProbeOutcome(EffectState.UNKNOWN, "error:external-extension-skill-binding-ambiguous")
                return LifecycleProbeOutcome(EffectState.PLANNED, evidence)
            if all(item["status"] == "inactive" for item in bindings):
                return LifecycleProbeOutcome(EffectState.SETTLED_OK, evidence)
            if any(item["status"] == "inactive" for item in bindings):
                return LifecycleProbeOutcome(EffectState.UNKNOWN, "error:external-extension-skill-binding-ambiguous")
            return LifecycleProbeOutcome(EffectState.PLANNED, evidence)
        except ExternalExtensionSkillMaterializationError as error:
            if str(error) == "expected external Skill binding is missing":
                return LifecycleProbeOutcome(EffectState.PLANNED, evidence)
            return LifecycleProbeOutcome(EffectState.UNKNOWN, "error:external-extension-skill-binding-drift")
        except (ApplicationSkillError, ValueError):
            return LifecycleProbeOutcome(EffectState.UNKNOWN, "error:external-extension-skill-binding-drift")

    def active_sources(self, project_id: str) -> tuple[ApplicationSkillSource, ...]:
        """Return only active, catalog-valid external sources for one project."""
        if not isinstance(project_id, str) or not project_id:
            raise ExternalExtensionSkillMaterializationError("project id is invalid")
        sources: list[ApplicationSkillSource] = []
        for revision in self._installations.active_revisions(root_id=project_id):
            try:
                plan = self._installations.load_activation_plan(revision.revision_ref)
                if not plan.is_pure_application_skill:
                    continue
                packages = self._read_materialized(plan, self._intent_like(revision, plan))
                registry = self._registry()
                bindings = self._exact_bindings(registry.status(self._snapshot(packages)), plan, packages, project_id)
                if not all(item["status"] == "active" and item["effective_status"] == "active" for item in bindings):
                    continue
                sources.append(ApplicationSkillSource(
                    self._source_id(project_id, revision.extension_id, revision.revision),
                    self._version_root(project_id, revision.extension_id, revision.revision),
                    "external",
                ))
            except (ApplicationSkillError, ExternalExtensionSkillMaterializationError, OSError, ValueError):
                continue
        return tuple(sorted(sources, key=lambda source: source.source_id))

    def active_packages(self, project_id: str) -> tuple[ApplicationSkillPackage, ...]:
        """Freeze the current active Skills into verified, path-independent packages.

        A call always rereads the managed tree through the exact-byte verifier,
        so a subsequent on-disk change is fail-closed.  Returned packages carry
        their verified bytes and therefore remain loadable after that path is
        later removed.  Only packages whose reviewed activation metadata permits
        model invocation are exposed to model-facing consumers; user-only Skills
        require a future explicit-invocation entry point.
        """
        if not isinstance(project_id, str) or not project_id:
            raise ExternalExtensionSkillMaterializationError("project id is invalid")
        packages: list[ApplicationSkillPackage] = []
        for revision in self._installations.active_revisions(root_id=project_id):
            plan = self._installations.load_activation_plan(revision.revision_ref)
            if not plan.is_pure_application_skill:
                continue
            verified = self._read_materialized(plan, self._intent_like(revision, plan))
            registry = self._registry()
            bindings = self._exact_bindings(
                registry.status(self._snapshot(verified)), plan, verified, project_id,
            )
            if not all(
                item["status"] == "active" and item["effective_status"] == "active"
                for item in bindings
            ):
                continue
            model_invocable_ids = {
                binding.skill_id
                for binding in plan.skill_bindings
                if binding.model_invocable
            }
            packages.extend(
                package
                for package in verified
                if package.skill_id in model_invocable_ids
            )
        snapshot = ApplicationSkillCatalog().snapshot_from_packages(packages)
        if snapshot.issues:
            raise ExternalExtensionSkillMaterializationError(
                "active external Skills have duplicate identities"
            )
        return tuple(sorted(packages, key=lambda package: (package.source_id, package.skill_id)))

    def _materialize(self, intent: ExternalExtensionLifecycleIntent) -> tuple[ExternalExtensionActivationPlan, tuple[ApplicationSkillPackage, ...]]:
        plan = self._validated_plan(intent)
        target = self._version_root_from_intent(intent)
        if os.name == "nt":
            # Do not resolve a target name after the trusted root has been
            # acquired.  A verified handle-relative read is the existence
            # check on Windows and rejects a swapped junction fail closed.
            try:
                return plan, self._read_materialized(plan, intent)
            except FileNotFoundError:
                pass
        elif target.exists():
            return plan, self._read_materialized(plan, intent)
        evidence = self._verified_evidence(intent)
        files = {path: evidence.inventory.read_bytes(path) for path in evidence.inventory.paths}
        self._write_version(target, plan, files)
        return plan, self._read_materialized(plan, intent)

    def _verified_evidence(self, intent: ExternalExtensionLifecycleIntent):
        evidence = self._facts.load_verified_intake_artifact(intent.intake_ref)
        if (evidence.artifact_ref, evidence.artifact_receipt_ref, evidence.content_sha256) != (
            intent.artifact_ref, intent.artifact_receipt_ref, intent.artifact_content_sha256,
        ):
            raise ExternalExtensionSkillMaterializationError(
                "verified artifact does not bind lifecycle intent"
            )
        return evidence

    def _validated_plan(self, intent: ExternalExtensionLifecycleIntent) -> ExternalExtensionActivationPlan:
        revision = self._installations.load_revision(intent.revision_ref)
        plan = self._installations.load_activation_plan(intent.revision_ref)
        expected = (
            revision.root_id, revision.intake_ref, revision.artifact_ref, revision.artifact_receipt_ref,
            revision.artifact_content_sha256, revision.manifest_identity, revision.review_plan_identity,
            revision.activation_plan_identity, revision.health_plan_identity,
        )
        actual = (
            intent.root_id, intent.intake_ref, intent.artifact_ref, intent.artifact_receipt_ref,
            intent.artifact_content_sha256, intent.manifest_identity, intent.review_plan_identity,
            intent.activation_plan_identity, intent.health_plan_identity,
        )
        if expected != actual or not plan.is_pure_application_skill:
            raise ExternalExtensionSkillMaterializationError("lifecycle intent activation plan drifted")
        return plan

    def _write_version(self, target: Path, plan: ExternalExtensionActivationPlan, files: Mapping[str, bytes]) -> None:
        self._ensure_root()
        staged_files: dict[str, bytes] = {}
        for binding in plan.skill_bindings:
            for relative, content in self._package_files(binding, files).items():
                staged_files[f"{binding.skill_id}/{relative}"] = content
        if os.name == "nt":
            try:
                target_parts = target.relative_to(self._root).parts
                created = WindowsHandleTreeIo().write_new_tree(
                    self._root, target_parts, staged_files,
                )
                if not created:
                    return
                # The exact handle-relative verification in
                # _read_materialized freezes the bytes for all consumers.
                return
            except (WindowsHandleIoError, ValueError) as error:
                raise ExternalExtensionSkillMaterializationError(
                    "external Skill materialization failed"
                ) from error
        self._assert_managed_chain(target.parent)
        if target.exists():
            return
        staging_root = self._root / ".staging"
        staging_root.mkdir(mode=0o700, exist_ok=True)
        _reject_reparse_path(staging_root)
        stage = staging_root / uuid4().hex
        try:
            stage.mkdir(mode=0o700)
            _reject_reparse_path(stage)
            for relative, content in staged_files.items():
                output = stage.joinpath(*PurePosixPath(relative).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._packages_from_verified_tree(
                stage, plan, staged_files, source_id="external-staged",
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            self._assert_managed_chain(target.parent)
            _reject_reparse_path(target.parent)
            if target.exists():
                return
            os.replace(stage, target)
            _sync_directory(target.parent)
        except (OSError, ApplicationSkillError, ValueError) as error:
            raise ExternalExtensionSkillMaterializationError("external Skill materialization failed") from error
        finally:
            if stage.exists() and stage.parent == staging_root:
                shutil.rmtree(stage, ignore_errors=True)

    def _package_files(self, binding: ExternalSkillActivationBinding, files: Mapping[str, bytes]) -> dict[str, bytes]:
        source = _safe_artifact_path(binding.source_path)
        if binding.package_layout == "flat_markdown":
            raw = _required_file(files, source)
            return {"SKILL.md": _normalized_skill(binding, raw)}
        if source != "SKILL.md" and not source.endswith("/SKILL.md"):
            raise ExternalExtensionSkillMaterializationError("package Skill source must name SKILL.md")
        prefix = "" if source == "SKILL.md" else source.removesuffix("SKILL.md")
        selected: dict[str, bytes] = {}
        for path, content in files.items():
            if not path.startswith(prefix):
                continue
            tail = path.removeprefix(prefix)
            if not _is_allowed_package_path(tail):
                continue
            selected[tail] = content
        raw = _required_file(selected, "SKILL.md")
        selected["SKILL.md"] = _normalized_skill(binding, raw)
        return selected

    def _read_materialized(self, plan: ExternalExtensionActivationPlan, intent: ExternalExtensionLifecycleIntent) -> tuple[ApplicationSkillPackage, ...]:
        root = self._version_root_from_intent(intent)
        return self._read_verified_root(root, plan, intent)

    def _read_verified_root(
        self,
        root: Path,
        plan: ExternalExtensionActivationPlan,
        intent: ExternalExtensionLifecycleIntent,
    ) -> tuple[ApplicationSkillPackage, ...]:
        evidence = self._verified_evidence(intent)
        artifact_files = {
            path: evidence.inventory.read_bytes(path) for path in evidence.inventory.paths
        }
        expected: dict[str, bytes] = {}
        for binding in plan.skill_bindings:
            for relative, content in self._package_files(binding, artifact_files).items():
                expected[f"{binding.skill_id}/{relative}"] = content
        if os.name == "nt":
            try:
                actual = WindowsHandleTreeIo().read_exact_tree(
                    self._root,
                    root.relative_to(self._root).parts,
                    tuple(expected),
                    expected_sizes={path: len(content) for path, content in expected.items()},
                )
            except WindowsHandleIoError as error:
                raise ExternalExtensionSkillMaterializationError(
                    "materialized external Skill bytes cannot be safely read"
                ) from error
        else:
            self._assert_managed_chain(root.parent)
            if not root.is_dir() or root.is_symlink():
                raise FileNotFoundError(root)
            _reject_reparse_path(root)
            actual = _read_exact_materialized(root, tuple(expected))
        if actual != expected:
            raise ExternalExtensionSkillMaterializationError(
                "materialized external Skill bytes drifted"
            )
        revision = self._installations.load_revision(intent.revision_ref)
        return self._packages_from_verified_tree(
            root,
            plan,
            actual,
            source_id=self._source_id(revision.root_id, revision.extension_id, revision.revision),
        )

    def _execute_uninstall(
        self, intent: ExternalExtensionLifecycleIntent, effect: Effect,
    ) -> None:
        self._require_uninstall_tombstone(intent, effect)
        plan = self._validated_plan(intent)
        source = self._version_root_from_intent(intent)
        quarantine = self._uninstall_quarantine_root(effect)
        try:
            packages = self._read_verified_root(source, plan, intent)
            source_present = True
        except FileNotFoundError:
            packages = self._read_verified_root(quarantine, plan, intent)
            source_present = False
        registry = self._registry()
        self._disable(registry, plan, packages, intent, effect)
        if source_present:
            self._quarantine_uninstalled_tree(source, quarantine)
        self._read_verified_root(quarantine, plan, intent)

    def _probe_uninstall(
        self,
        intent: ExternalExtensionLifecycleIntent,
        effect: Effect,
        evidence: str,
    ) -> LifecycleProbeOutcome:
        try:
            self._require_uninstall_tombstone(intent, effect)
            plan = self._validated_plan(intent)
            source = self._version_root_from_intent(intent)
            quarantine = self._uninstall_quarantine_root(effect)
            try:
                packages = self._read_verified_root(source, plan, intent)
                registry = self._registry()
                bindings = self._exact_bindings(
                    registry.status(self._snapshot(packages)), plan, packages, intent.root_id,
                )
                if any(item["status"] == "inactive" for item in bindings) and not all(
                    item["status"] == "inactive" for item in bindings
                ):
                    return LifecycleProbeOutcome(
                        EffectState.UNKNOWN,
                        "error:external-extension-uninstall-binding-ambiguous",
                    )
                return LifecycleProbeOutcome(EffectState.PLANNED, evidence)
            except FileNotFoundError:
                pass
            packages = self._read_verified_root(quarantine, plan, intent)
            registry = self._registry()
            bindings = self._exact_bindings(
                registry.status(self._snapshot(packages)), plan, packages, intent.root_id,
            )
            if all(item["status"] == "inactive" for item in bindings):
                return LifecycleProbeOutcome(EffectState.SETTLED_OK, evidence)
            return LifecycleProbeOutcome(
                EffectState.UNKNOWN,
                "error:external-extension-uninstall-binding-active-after-removal",
            )
        except FileNotFoundError:
            return LifecycleProbeOutcome(
                EffectState.UNKNOWN,
                "error:external-extension-uninstall-owned-tree-missing",
            )
        except (ExternalExtensionSkillMaterializationError, ApplicationSkillError, OSError, ValueError):
            return LifecycleProbeOutcome(
                EffectState.UNKNOWN,
                "error:external-extension-uninstall-drift",
            )

    def _require_uninstall_tombstone(
        self, intent: ExternalExtensionLifecycleIntent, effect: Effect,
    ) -> None:
        tombstone = self._installations.load_uninstall_tombstone(effect.operation_id)
        revision = self._installations.load_revision(intent.revision_ref)
        expected = {
            "root_id": intent.root_id,
            "extension_id": revision.extension_id,
            "revision_ref": intent.revision_ref,
            "managed_revision": revision.revision,
        }
        if any(tombstone.get(key) != value for key, value in expected.items()):
            raise ExternalExtensionSkillMaterializationError(
                "uninstall tombstone does not bind the managed revision"
            )

    def _uninstall_quarantine_root(self, effect: Effect) -> Path:
        import hashlib

        identity = hashlib.sha256(effect.operation_id.encode("utf-8")).hexdigest()
        return self._root / ".uninstalled" / identity

    def _quarantine_uninstalled_tree(self, source: Path, target: Path) -> None:
        if os.name == "nt":
            try:
                io = WindowsHandleTreeIo()
                io.ensure_directory_chain(self._root, (".uninstalled",))
                io.move_dir_no_replace(
                    self._root,
                    source.relative_to(self._root).parts,
                    (".uninstalled",),
                    target.name,
                )
                return
            except (WindowsHandleIoError, ValueError) as error:
                raise ExternalExtensionSkillMaterializationError(
                    "uninstall managed tree cannot be quarantined safely"
                ) from error
        if target.exists() or target.is_symlink():
            raise ExternalExtensionSkillMaterializationError(
                "uninstall quarantine target already exists"
            )
        target.parent.mkdir(mode=0o700, exist_ok=True)
        self._assert_managed_chain(source.parent)
        _reject_reparse_path(target.parent)
        source.rename(target)
        _sync_directory(target.parent)

    @staticmethod
    def _packages_from_verified_tree(
        root: Path,
        plan: ExternalExtensionActivationPlan,
        tree: Mapping[str, bytes],
        *,
        source_id: str,
    ) -> tuple[ApplicationSkillPackage, ...]:
        """Build packages from an already exact-verified tree, never from paths."""
        catalog = ApplicationSkillCatalog()
        packages: list[ApplicationSkillPackage] = []
        for binding in plan.skill_bindings:
            prefix = f"{binding.skill_id}/"
            files = {
                path.removeprefix(prefix): content
                for path, content in tree.items()
                if path.startswith(prefix)
            }
            try:
                package = catalog.package_from_verified_content(
                    ApplicationSkillVerifiedContent.from_mapping(files),
                    source_id=source_id,
                    source_kind="external",
                    package_root=root / binding.skill_id,
                )
            except ApplicationSkillError as error:
                raise ExternalExtensionSkillMaterializationError(
                    "materialized Skills fail verified catalog validation"
                ) from error
            if package.skill_id != binding.skill_id:
                raise ExternalExtensionSkillMaterializationError(
                    "materialized Skill identity drifted from activation plan"
                )
            packages.append(package)
        return tuple(sorted(packages, key=lambda package: package.skill_id))

    def _activate(self, registry: ApplicationSkillBindingRegistry, plan: ExternalExtensionActivationPlan, packages: Sequence[ApplicationSkillPackage], intent: ExternalExtensionLifecycleIntent, effect: Effect) -> None:
        requests = [
            {
                "package": package,
                "project_id": intent.root_id,
                "allowed_consumers": binding.allowed_consumers,
                "priority": binding.priority,
                "trigger_terms": binding.trigger_terms,
            }
            for binding, package in zip(plan.skill_bindings, packages, strict=True)
        ]
        self._reject_active_identity_collisions(
            registry, requests, plan=plan, intent=intent,
        )
        preview = registry.preview_bind_batch(requests)
        registry.activate_batch(
            requests,
            expected_registry_revision=int(preview["registry_revision"]),
            preview_token=str(preview["preview_token"]),
            confirm=True,
            reason=self._reason(effect),
        )

    def _reject_active_identity_collisions(
        self,
        registry: ApplicationSkillBindingRegistry,
        requests: Sequence[Mapping[str, object]],
        *,
        plan: ExternalExtensionActivationPlan,
        intent: ExternalExtensionLifecycleIntent,
    ) -> None:
        """Do not let an external Skill replace another active source identity.

        Registry bindings predate provenance storage, so an old record has
        ``unknown`` provenance.  It remains readable for ordinary consumers,
        but external activation must treat it as an unsafe collision rather
        than infer that equal bytes came from this reviewed revision.
        """
        status = registry.status()
        raw = status.get("bindings")
        if not isinstance(raw, list):
            raise ExternalExtensionSkillMaterializationError("binding registry is invalid")
        governed_source_ids = {
            self._source_id(intent.root_id, plan.extension_id, item.revision)
            for item in self._installations.revision_history(
                plan.extension_id, root_id=intent.root_id,
            )
        }
        for request in requests:
            package = request.get("package")
            project_id = request.get("project_id")
            if not isinstance(package, ApplicationSkillPackage) or not isinstance(project_id, str):
                raise ExternalExtensionSkillMaterializationError("external Skill activation request is invalid")
            existing = [
                item for item in raw
                if isinstance(item, Mapping)
                and item.get("project_id") == project_id
                and item.get("skill_id") == package.skill_id
                and item.get("status") == "active"
            ]
            if len(existing) > 1:
                raise ExternalExtensionSkillMaterializationError("external Skill binding identity is ambiguous")
            if existing and not (
                existing[0].get("source_kind") == package.source_kind == "external"
                and existing[0].get("source_id") in governed_source_ids
            ):
                raise ExternalExtensionSkillMaterializationError(
                    "active Application Skill identity belongs to another source"
                )

    def _disable(self, registry: ApplicationSkillBindingRegistry, plan: ExternalExtensionActivationPlan, packages: Sequence[ApplicationSkillPackage], intent: ExternalExtensionLifecycleIntent, effect: Effect) -> None:
        status = registry.status(self._snapshot(packages))
        bindings = self._exact_bindings(status, plan, packages, intent.root_id)
        requests: list[dict[str, str]] = []
        for item in bindings:
            package = _package_for(packages, str(item["skill_id"]))
            if item["skill_fingerprint"] != package.fingerprint:
                raise ExternalExtensionSkillMaterializationError(
                    "refusing to disable a different Skill fingerprint"
                )
            requests.append(
                {
                    "project_id": intent.root_id,
                    "skill_id": package.skill_id,
                    "skill_fingerprint": package.fingerprint,
                }
            )
        registry.deactivate_batch(
            requests,
            expected_registry_revision=int(status["registry_revision"]),
            confirm=True,
            reason=self._reason(effect),
        )

    @staticmethod
    def _snapshot(packages: Sequence[ApplicationSkillPackage]):
        from core.application_skill import ApplicationSkillCatalogSnapshot
        return ApplicationSkillCatalogSnapshot(tuple(packages), (), 1)

    def _exact_bindings(self, status: Mapping[str, object], plan: ExternalExtensionActivationPlan, packages: Sequence[ApplicationSkillPackage], project_id: str) -> list[dict[str, object]]:
        raw = status.get("bindings")
        if not isinstance(raw, list):
            raise ExternalExtensionSkillMaterializationError("binding registry is invalid")
        by_skill = {package.skill_id: package for package in packages}
        found: list[dict[str, object]] = []
        for expected in plan.skill_bindings:
            matches = [dict(item) for item in raw if isinstance(item, Mapping) and item.get("project_id") == project_id and item.get("skill_id") == expected.skill_id]
            if not matches:
                raise ExternalExtensionSkillMaterializationError("expected external Skill binding is missing")
            if len(matches) != 1:
                raise ExternalExtensionSkillMaterializationError("expected external Skill binding is ambiguous")
            item = matches[0]
            package = by_skill.get(expected.skill_id)
            if package is None or item.get("skill_fingerprint") != package.fingerprint:
                raise ExternalExtensionSkillMaterializationError("external Skill binding fingerprint drifted")
            # A matching fingerprint and policy are not sufficient authority to
            # settle, disable, or expose a Skill.  They could describe the
            # same bytes from a different external revision (or a forged
            # legacy record).  The binding must identify this exact immutable
            # package provenance as well.
            if (
                item.get("source_id") != package.source_id
                or item.get("source_kind") != package.source_kind
            ):
                raise ExternalExtensionSkillMaterializationError(
                    "external Skill binding provenance drifted"
                )
            if tuple(item.get("allowed_consumers", ())) != expected.allowed_consumers or item.get("priority") != expected.priority or tuple(item.get("trigger_terms", ())) != expected.trigger_terms:
                raise ExternalExtensionSkillMaterializationError("external Skill binding policy drifted")
            found.append(item)
        return found

    def _registry(self) -> ApplicationSkillBindingRegistry:
        if self._binding_store is None:
            return self._registry_factory()
        return self._registry_factory(self._binding_store)

    def _ensure_root(self) -> None:
        if os.name == "nt":
            try:
                WindowsHandleTreeIo().ensure_directory_chain(
                    self._root.parent, (self._root.name,),
                )
            except (WindowsHandleIoError, FileNotFoundError) as error:
                raise ExternalExtensionSkillMaterializationError(
                    "managed external Skill root cannot be created safely"
                ) from error
            return
        self._root.mkdir(parents=True, exist_ok=True)
        _reject_reparse_path(self._root)

    def _assert_managed_chain(self, path: Path) -> None:
        """Reject a reparse point anywhere below the configured owned root."""
        try:
            relative = path.relative_to(self._root)
        except ValueError as error:
            raise ExternalExtensionSkillMaterializationError("managed path escaped its root") from error
        current = self._root
        _reject_reparse_path(current)
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                _reject_reparse_path(current)
                if not current.is_dir():
                    raise ExternalExtensionSkillMaterializationError(
                        "managed version path is unsafe"
                    )

    def _version_root_from_intent(self, intent: ExternalExtensionLifecycleIntent) -> Path:
        revision = self._installations.load_revision(intent.revision_ref)
        return self._version_root(intent.root_id, revision.extension_id, revision.revision)

    def _version_root(self, root_id: str, extension_id: str, revision: int) -> Path:
        return (
            self._root
            / _managed_path_part(root_id)
            / _managed_path_part(extension_id)
            / f"revision-{revision}"
        )

    @staticmethod
    def _source_id(root_id: str, extension_id: str, revision: int) -> str:
        import hashlib

        digest = hashlib.sha256(
            f"{root_id}\0{extension_id}\0{revision}".encode("utf-8")
        ).hexdigest()[:16]
        prefix = re.sub(r"[^a-z0-9._-]+", "-", extension_id.lower()).strip("-._")
        revision_text = str(revision)
        # Binding source IDs are limited to 64 characters, while installation
        # revisions have no fixed upper bound.  Preserve the human-readable
        # revision whenever it fits, shortening only the cosmetic extension
        # prefix.  For an extreme revision that leaves no prefix room, the
        # digest still binds root, extension, and revision deterministically.
        prefix_budget = 64 - len("external-") - len(revision_text) - 2 - len(digest)
        if prefix_budget < 1:
            return f"external-{digest}"
        return f"external-{(prefix or 'skill')[:prefix_budget]}-{revision_text}-{digest}"

    @staticmethod
    def _evidence_ref(effect: Effect) -> str:
        return f"facts:external-extension-skill-materializer/{effect.operation_id}"

    @staticmethod
    def _reason(effect: Effect) -> str:
        return f"external extension lifecycle {effect.operation_id}"

    @staticmethod
    def _intent_like(revision, plan):
        # Rebuild the narrow immutable evidence view required by
        # ``_read_materialized``.  Active-source discovery must re-verify the
        # exact quarantine artifact rather than trusting files that merely
        # remain below the managed root.
        return SimpleNamespace(
            revision_ref=revision.revision_ref,
            root_id=revision.root_id,
            intake_ref=revision.intake_ref,
            artifact_ref=revision.artifact_ref,
            artifact_receipt_ref=revision.artifact_receipt_ref,
            artifact_content_sha256=revision.artifact_content_sha256,
        )


# A short alias keeps composition code readable while retaining the explicit name.
ExternalExtensionSkillMaterializer = ExternalExtensionApplicationSkillMaterializer


def _safe_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or "\\" in value or ":" in value or ".." in path.parts:
        raise ExternalExtensionSkillMaterializationError("artifact Skill path is unsafe")
    if any(
        not part
        or part in {".", ".."}
        or part.rstrip(" .") != part
        or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED
        for part in path.parts
    ):
        raise ExternalExtensionSkillMaterializationError("artifact Skill path is unsafe")
    return path.as_posix()


def _is_allowed_package_path(path: str) -> bool:
    safe = _safe_artifact_path(path)
    parts = PurePosixPath(safe).parts
    if safe == "SKILL.md":
        return True
    return len(parts) >= 2 and parts[0] in _RESOURCE_GROUPS


def _managed_path_part(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExternalExtensionSkillMaterializationError("managed version identity is invalid")
    if (
        _SAFE_PATH_PART.fullmatch(value)
        and value.rstrip(" .") == value
        and value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED
        and value not in {".", ".."}
    ):
        return value
    import hashlib

    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")[:48]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix or 'identity'}-{digest}"


def _reject_reparse_path(path: Path) -> None:
    current = path
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for candidate in existing:
        try:
            info = candidate.lstat()
        except OSError as error:
            raise ExternalExtensionSkillMaterializationError(
                "managed external Skill path is unavailable"
            ) from error
        if candidate.is_symlink() or bool(
            getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            raise ExternalExtensionSkillMaterializationError(
                "managed external Skill path cannot cross a reparse point"
            )


def _read_exact_materialized(root: Path, expected_paths: tuple[str, ...]) -> dict[str, bytes]:
    expected = frozenset(expected_paths)
    expected_directories = {
        PurePosixPath(*PurePosixPath(path).parts[:index]).as_posix()
        for path in expected
        for index in range(1, len(PurePosixPath(path).parts))
    }
    actual: dict[str, bytes] = {}
    entry_limit = len(expected) + len(expected_directories) + 8
    entries = 0
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for path in root.rglob("*"):
        entries += 1
        if entries > entry_limit:
            raise ExternalExtensionSkillMaterializationError(
                "materialized external Skill tree is unbounded"
            )
        try:
            info = path.lstat()
        except OSError as error:
            raise ExternalExtensionSkillMaterializationError(
                "materialized external Skill path is unavailable"
            ) from error
        if path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse_flag):
            raise ExternalExtensionSkillMaterializationError(
                "materialized external Skill cannot contain links"
            )
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            if relative not in expected_directories:
                raise ExternalExtensionSkillMaterializationError(
                    "materialized external Skill contains an unexpected directory"
                )
            continue
        if not stat.S_ISREG(info.st_mode) or relative not in expected:
            raise ExternalExtensionSkillMaterializationError(
                "materialized external Skill contains an unexpected file"
            )
        actual[relative] = path.read_bytes()
    return actual


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _required_file(files: Mapping[str, bytes], path: str) -> bytes:
    value = files.get(path)
    if not isinstance(value, bytes):
        raise ExternalExtensionSkillMaterializationError("external Skill source is missing")
    return value


def _normalized_skill(binding: ExternalSkillActivationBinding, raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExternalExtensionSkillMaterializationError("external Skill markdown must be UTF-8") from error
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines(keepends=True)
    body = text
    if lines and lines[0].strip() == "---":
        closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if closing is None:
            raise ExternalExtensionSkillMaterializationError("external Skill frontmatter is not closed")
        body = "".join(lines[closing + 1:]).lstrip("\r\n")
    if not body.strip():
        raise ExternalExtensionSkillMaterializationError("external Skill instructions are empty")
    import json

    description = binding.description
    if binding.trigger_boundary != description:
        combined = f"{description} {binding.trigger_boundary}"
        if len(combined) <= 2048:
            description = combined
    frontmatter = (
        "---\n"
        f"name: {binding.skill_id}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"trigger_boundary: {json.dumps(binding.trigger_boundary, ensure_ascii=False)}\n"
        "validation: reviewed-external-extension\n"
        "maturity: verified\n"
        "---\n\n"
    )
    return (frontmatter + body).encode("utf-8")


def _package_for(packages: Sequence[ApplicationSkillPackage], skill_id: str) -> ApplicationSkillPackage:
    package = next((item for item in packages if item.skill_id == skill_id), None)
    if package is None:
        raise ExternalExtensionSkillMaterializationError("materialized Skill package is missing")
    return package
