from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import re
from typing import Protocol

from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort
from core.ai_tooling import ToolDefinition, ToolRetryPolicy
from core.media_hands import MediaHandsAdmission, SourcePermissionSnapshot
from core.source_processing import (
    ManifestPermission,
    MediaRouter,
    PlatformResolution,
    PlatformResolver,
    SourceManifest,
    SourceManifestArtifactRepository,
    SourceManifestCodec,
    SourcePermissionAuthority,
    SourcePermissionError,
)
from backend.api.xiaohongshu_platform_provider import XiaohongshuMetadataProviderError


ANALYZE_SOURCE_CAPABILITY = "analyze_source"
_INPUT_SCHEMA = "crp://default/contracts/ai/analyze-source-request.schema.json"
_OUTPUT_SCHEMA = "crp://default/contracts/ai/analyze-source-result.schema.json"
_RECEIPT_SCHEMA = "crp://default/contracts/ai/analyze-source-receipt.schema.json"
_CONTROLLED_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~/-]*$")


@dataclass(frozen=True, slots=True)
class ResolvedSourceManifest:
    manifest: SourceManifest | None
    manifest_ref: str | None
    manifest_revision: str | None
    evidence_refs: tuple[str, ...]
    terminal_reason: str | None = None
    permission_snapshot: SourcePermissionSnapshot | None = None


@dataclass(frozen=True, slots=True)
class AnalyzeSourceReadiness:
    enabled: bool
    worker_ready: bool
    provider_ready: bool

    @property
    def ready(self) -> bool:
        return self.enabled and self.worker_ready and self.provider_ready


class SourceManifestResolverPort(Protocol):
    def resolve(self, arguments: Mapping[str, object], scope: Mapping[str, object]) -> ResolvedSourceManifest: ...


class ControlledXiaohongshuProviderPort(Protocol):
    def provide_controlled(
        self, text: str, *, project_id: str, credential_subject_id: str,
    ) -> SourceManifest: ...


class MediaHandsProvisionerPort(Protocol):
    def provision(
        self,
        *,
        manifest: SourceManifest,
        manifest_ref: str,
        manifest_revision: str,
        operation: str,
        idempotency_key: str,
        created_at: str,
        permission_snapshot: SourcePermissionSnapshot,
    ) -> MediaHandsAdmission: ...


@dataclass(frozen=True, slots=True)
class ArtifactSourceManifestResolver:
    """Resolve only a previously persisted manifest artifact in the Turn project scope."""

    repository: SourceManifestArtifactRepository

    def resolve(self, arguments: Mapping[str, object], scope: Mapping[str, object]) -> ResolvedSourceManifest:
        input_value = arguments.get("input")
        project_id = _required(scope.get("project_id"), "project id")
        if not isinstance(input_value, Mapping) or input_value.get("kind") != "source_ref":
            raise ValueError("persisted manifest resolver requires a source_ref input")
        source_ref = _required(input_value.get("source_ref"), "source manifest ref")
        artifact = self.repository.resolve_source_ref(
            source_ref=source_ref,
            project_id=project_id,
        )
        manifest = artifact.manifest
        evidence_refs = _refs(
            (
                *manifest.provenance_refs,
                *manifest.permission.evidence_refs,
                *(ref for asset in manifest.assets for ref in asset.evidence_refs),
            )
        )
        return ResolvedSourceManifest(
            manifest=manifest,
            manifest_ref=artifact.public_ref,
            manifest_revision=artifact.revision,
            evidence_refs=evidence_refs,
        )


@dataclass(frozen=True, slots=True)
class PlatformArtifactSourceManifestResolver:
    """Resolve text inside Hands, then persist the one canonical manifest artifact."""

    artifacts: SourceManifestArtifactRepository
    platforms: PlatformResolver
    permissions: SourcePermissionAuthority
    executable_platforms: frozenset[str] | None = None
    controlled_xiaohongshu: ControlledXiaohongshuProviderPort | None = None

    def resolve(self, arguments: Mapping[str, object], scope: Mapping[str, object]) -> ResolvedSourceManifest:
        input_value = arguments.get("input")
        project_id = _required(scope.get("project_id"), "project id")
        if not isinstance(input_value, Mapping):
            raise ValueError("source input is required")
        if input_value.get("kind") == "source_ref":
            resolved = ArtifactSourceManifestResolver(self.artifacts).resolve(arguments, scope)
            return self._with_effective_permission(resolved, project_id=project_id)
        if input_value.get("kind") != "text":
            raise ValueError("platform resolver requires text or source_ref input")
        text = _required(input_value.get("text"), "source text")
        access_mode, credential_subject_id = _source_access(arguments)
        if access_mode == "controlled_credential":
            probe = self.platforms.probe(text)
            if probe.platform != "xiaohongshu":
                return ResolvedSourceManifest(
                    None, None, None, (), "unsupported_source",
                )
            if self.controlled_xiaohongshu is None:
                return ResolvedSourceManifest(
                    None, None, None, (), "provider_unavailable",
                )
            try:
                manifest = self.controlled_xiaohongshu.provide_controlled(
                    text,
                    project_id=project_id,
                    credential_subject_id=credential_subject_id or "",
                )
                manifest = SourceManifestCodec.decode(SourceManifestCodec.encode(manifest))
            except XiaohongshuMetadataProviderError as error:
                reason = str(error)
                if reason not in {
                    "controlled_credential_confirmed_none",
                    "controlled_credential_pre_wire_drift",
                    "controlled_credential_post_wire_unknown",
                }:
                    reason = "unsupported_source"
                return ResolvedSourceManifest(None, None, None, (), reason)
            except (ValueError, TypeError):
                return ResolvedSourceManifest(
                    None, None, None, (), "unsupported_source",
                )
            resolution = PlatformResolution(
                "resolved", "xiaohongshu", None, manifest,
            )
        else:
            resolution = self.platforms.resolve(text, project_id=project_id)
        manifest = resolution.manifest
        terminal_reason = _platform_terminal_reason(resolution.status, resolution.reason)
        if manifest is None:
            return ResolvedSourceManifest(None, None, None, (), terminal_reason)
        manifest_id = _manifest_artifact_id(manifest)
        artifact = self.artifacts.put(
            project_id=project_id,
            manifest_id=manifest_id,
            manifest=manifest,
        )
        resolved = ResolvedSourceManifest(
            artifact.manifest,
            artifact.public_ref,
            artifact.revision,
            _manifest_evidence_refs(manifest),
            terminal_reason,
        )
        return self._with_effective_permission(resolved, project_id=project_id)

    def _with_effective_permission(
        self, resolved: ResolvedSourceManifest, *, project_id: str
    ) -> ResolvedSourceManifest:
        manifest = resolved.manifest
        if manifest is None:
            return resolved
        metadata_refs = tuple(
            ref for ref in manifest.permission.evidence_refs
            if "/source-permissions/projects/" not in ref
        )
        if len(metadata_refs) != 1:
            return resolved
        try:
            permission = self.permissions.current_for_source(
                project_id=project_id,
                source_id=manifest.source_id,
                metadata_evidence_ref=metadata_refs[0],
            )
        except SourcePermissionError:
            return resolved
        if permission is None:
            return resolved
        decision = "granted" if permission.state == "granted" else "denied"
        if (
            manifest.permission.decision == decision
            and permission.public_ref in manifest.permission.evidence_refs
        ):
            snapshot = None
            if permission.state == "granted":
                snapshot = SourcePermissionSnapshot(
                    project_id=project_id,
                    manifest_ref=resolved.manifest_ref,
                    manifest_revision=resolved.manifest_revision,
                    grant_ref=permission.public_ref,
                    grant_revision=f"r{permission.revision}",
                    revocation_generation=permission.revocation_generation,
                )
            return self._with_executable_platform(
                replace(resolved, permission_snapshot=snapshot)
            )
        base_normalizer = re.sub(r"-permission-r[1-9][0-9]*$", "", manifest.normalizer_revision)
        derived = replace(
            manifest,
            normalizer_revision=f"{base_normalizer}-permission-r{permission.revision}",
            permission=ManifestPermission(
                decision=decision,
                evidence_refs=(metadata_refs[0], permission.public_ref),
            ),
        )
        artifact = self.artifacts.put(
            project_id=project_id,
            manifest_id=_manifest_artifact_id(derived),
            manifest=derived,
        )
        snapshot = None
        if permission.state == "granted":
            snapshot = SourcePermissionSnapshot(
                project_id=project_id,
                manifest_ref=artifact.public_ref,
                manifest_revision=artifact.revision,
                grant_ref=permission.public_ref,
                grant_revision=f"r{permission.revision}",
                revocation_generation=permission.revocation_generation,
            )
        result = ResolvedSourceManifest(
            manifest=artifact.manifest,
            manifest_ref=artifact.public_ref,
            manifest_revision=artifact.revision,
            evidence_refs=_manifest_evidence_refs(artifact.manifest),
            terminal_reason=resolved.terminal_reason,
            permission_snapshot=snapshot,
        )
        return self._with_executable_platform(result)

    def _with_executable_platform(
        self, resolved: ResolvedSourceManifest
    ) -> ResolvedSourceManifest:
        manifest = resolved.manifest
        if (
            manifest is not None
            and manifest.permission.decision == "granted"
            and self.executable_platforms is not None
            and manifest.platform not in self.executable_platforms
        ):
            return replace(resolved, terminal_reason="media_provider_unavailable")
        return resolved


class AnalyzeSourceCapability:
    """A single high-level Tool seam; platform and media routing stay internal."""

    def __init__(
        self,
        *,
        resolver: SourceManifestResolverPort,
        provisioner: MediaHandsProvisionerPort,
        payloads: TurnPayloadStorePort,
        readiness: Callable[[], AnalyzeSourceReadiness],
        created_at: Callable[[], str],
        namespace_id: str,
    ) -> None:
        self._resolver = resolver
        self._provisioner = provisioner
        self._payloads = payloads
        self._readiness = readiness
        self._created_at = created_at
        self._namespace_id = _required(namespace_id, "namespace id")
        self._router = MediaRouter()

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn id")
        tool_call_id = _required(request.get("tool_call_id"), "tool call id")
        operation_id = _required(request.get("operation_id"), "operation id")
        idempotency_key = _required(request.get("idempotency_key"), "idempotency key")
        if len(idempotency_key) < 16:
            raise ValueError("idempotency key is too short")
        arguments = _arguments(request)
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("analyze_source scope is required")

        receipt_kind = f"analyze-source-receipt:{tool_call_id}"
        existing = self._payloads.get_immutable_payload(turn_id, receipt_kind)
        if existing is not None:
            receipt_ref, receipt = existing
            expected_identity = {
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "operation_id": operation_id,
                "tool_name": ANALYZE_SOURCE_CAPABILITY,
                "tool_version": 1,
                "idempotency_key": idempotency_key,
            }
            if not isinstance(receipt, Mapping) or any(
                receipt.get(key) != value for key, value in expected_identity.items()
            ):
                raise ValueError("analyze_source receipt identity drift")
            outcome = receipt.get("outcome")
            evidence_refs = receipt.get("evidence_refs")
            if not isinstance(outcome, Mapping) or not isinstance(evidence_refs, list):
                raise ValueError("analyze_source replay receipt is invalid")
            refs = _refs(tuple(evidence_refs))
            return {
                "summary": "Source analysis replayed from its immutable receipt",
                "receipt_ref": receipt_ref,
                "payload_ref": None,
                "evidence_refs": list(refs),
                "result": dict(outcome),
            }

        readiness = self._readiness()
        if not isinstance(readiness, AnalyzeSourceReadiness):
            raise TypeError("analyze_source readiness is invalid")
        if not readiness.ready:
            return self._terminal(
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                reason="feature_disabled",
                resolved=None,
                evidence_refs=(),
            )

        resolved = self._resolver.resolve(arguments, scope)
        _validate_resolved(resolved)
        if resolved.terminal_reason is not None:
            return self._terminal(
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                reason=resolved.terminal_reason,
                resolved=resolved,
                evidence_refs=resolved.evidence_refs,
            )
        assert resolved.manifest is not None
        routing = self._router.route(resolved.manifest)
        evidence_refs = _refs((*resolved.evidence_refs, *routing.evidence_refs))
        if routing.status == "terminal":
            return self._terminal(
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                reason=_required(routing.reason, "terminal reason"),
                resolved=resolved,
                evidence_refs=evidence_refs,
            )

        admission = self._provisioner.provision(
            manifest=resolved.manifest,
            manifest_ref=resolved.manifest_ref,
            manifest_revision=resolved.manifest_revision,
            operation="analyze_source",
            idempotency_key=idempotency_key,
            created_at=self._created_at(),
            permission_snapshot=resolved.permission_snapshot,
        )
        job_id = _required(admission.record.payload.get("id"), "media job id")
        job_ref = f"crp://{self._namespace_id}/jobs/{job_id.replace(':', '/')}"
        canonical_job_ref = _canonical_expert_media_job_ref(resolved.manifest.source_id)
        outcome = {
            "status": "admitted",
            "reason": None,
            "manifest_ref": resolved.manifest_ref,
            "manifest_revision": resolved.manifest_revision,
            "source_manifest_ref": resolved.manifest_ref,
            "source_manifest_revision": resolved.manifest_revision,
            "job_id": job_id,
            "job_ref": job_ref,
            "canonical_job_ref": canonical_job_ref,
            "job_revision": admission.record.revision,
            "replayed": admission.replayed,
        }
        credential_use = _credential_use(resolved.manifest, result="admitted")
        if credential_use is not None:
            outcome["credential_use"] = credential_use
        return self._result(
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            outcome=outcome,
            evidence_refs=_refs((resolved.manifest_ref, job_ref, *evidence_refs)),
            summary="Source analysis admitted to Media Hands",
        )

    def _terminal(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        operation_id: str,
        idempotency_key: str,
        reason: str,
        resolved: ResolvedSourceManifest | None,
        evidence_refs: tuple[str, ...],
    ) -> Mapping[str, object]:
        outcome = {
            "status": "terminal",
            "reason": reason,
            "manifest_ref": resolved.manifest_ref if resolved is not None else None,
            "manifest_revision": resolved.manifest_revision if resolved is not None else None,
            "job_id": None,
            "job_ref": None,
            "job_revision": None,
            "replayed": False,
        }
        credential_use = _credential_use(
            resolved.manifest if resolved is not None else None, result="terminal"
        )
        if credential_use is not None:
            outcome["credential_use"] = credential_use
        refs = evidence_refs
        if resolved is not None and resolved.manifest_ref is not None:
            refs = _refs((resolved.manifest_ref, *refs))
        return self._result(
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            outcome=outcome,
            evidence_refs=refs,
            summary=f"Source analysis stopped: {reason}",
        )

    def _result(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        operation_id: str,
        idempotency_key: str,
        outcome: Mapping[str, object],
        evidence_refs: tuple[str, ...],
        summary: str,
    ) -> Mapping[str, object]:
        receipt = {
            "schema_version": "1.0.0",
            "receipt_id": f"analyze-source:{tool_call_id}",
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "operation_id": operation_id,
            "tool_name": ANALYZE_SOURCE_CAPABILITY,
            "tool_version": 1,
            "idempotency_key": idempotency_key,
            "outcome": dict(outcome),
            "evidence_refs": list(evidence_refs),
        }
        receipt_ref = self._payloads.get_or_create_immutable_payload(
            turn_id,
            f"analyze-source-receipt:{tool_call_id}",
            receipt,
        )
        return {
            "summary": summary,
            "receipt_ref": receipt_ref,
            "payload_ref": None,
            "evidence_refs": list(evidence_refs),
            "result": dict(outcome),
        }


class _UnavailableResolver:
    def resolve(self, arguments: Mapping[str, object], scope: Mapping[str, object]) -> ResolvedSourceManifest:
        del arguments, scope
        raise RuntimeError("analyze_source resolver is unavailable")


class _UnavailableProvisioner:
    def provision(self, **kwargs: object) -> MediaHandsAdmission:
        del kwargs
        raise RuntimeError("analyze_source provisioner is unavailable")


def disabled_analyze_source_capability(
    *,
    payloads: TurnPayloadStorePort,
    namespace_id: str,
    resolver: SourceManifestResolverPort | None = None,
) -> AnalyzeSourceCapability:
    """Register a receipt-producing fail-closed diagnostic without creating Jobs."""
    return AnalyzeSourceCapability(
        resolver=resolver or _UnavailableResolver(),
        provisioner=_UnavailableProvisioner(),
        payloads=payloads,
        readiness=lambda: AnalyzeSourceReadiness(False, False, False),
        created_at=lambda: "1970-01-01T00:00:00Z",
        namespace_id=namespace_id,
    )


def analyze_source_capability_definition(*, available: bool = False) -> CapabilityDefinition:
    tool = ToolDefinition(
        tool_id=ANALYZE_SOURCE_CAPABILITY,
        version=1,
        display_name="Analyze source",
        description="Resolve one source and admit its normalized content recipe.",
        source="core",
        owner_id="media-hands",
        effect="write",
        data_classes=("source_metadata", "media_assets"),
        destination="platform",
        input_schema_uri=_INPUT_SCHEMA,
        output_schema_uri=_OUTPUT_SCHEMA,
        receipt_schema_uri=_RECEIPT_SCHEMA,
        operation_semantics="receipt_required",
        execution_mode="exclusive",
        resource_locks=("source_manifest", "media_hands_admission"),
        idempotency="idempotent",
        retry_policy=ToolRetryPolicy(1, 0, ()),
        verification_tool_id=None,
        compensation_tool_id=None,
        mutability="reversible",
        egress_class="remote",
        network_scope=("platform_metadata_endpoint",),
        data_egress_scope=("source_url",),
        timeout_ms=30_000,
        required_scopes=("source_read", "job_write"),
        boundary_requirements=("project_grant", "user_approval"),
        available=available,
    )
    return CapabilityDefinition(
        ANALYZE_SOURCE_CAPABILITY,
        1,
        "write",
        True,
        "receipt_required",
        _INPUT_SCHEMA,
        _OUTPUT_SCHEMA,
        tool,
    )


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    arguments = request.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("analyze_source arguments are required")
    required = {"input", "intent", "output_profile", "resource_budget"}
    if frozenset(arguments) not in {frozenset(required), frozenset((*required, "access"))}:
        raise ValueError("analyze_source arguments do not match the Tool contract")
    _source_access(arguments)
    input_value = arguments.get("input")
    if not isinstance(input_value, Mapping) or set(input_value) != {"kind", "text", "source_ref"}:
        raise ValueError("analyze_source input is invalid")
    input_kind = input_value.get("kind")
    if input_kind == "text":
        if not isinstance(input_value.get("text"), str) or not input_value["text"] or input_value.get("source_ref") is not None:
            raise ValueError("analyze_source text input is invalid")
    elif input_kind == "source_ref":
        source_ref = input_value.get("source_ref")
        if input_value.get("text") is not None or not isinstance(source_ref, str) or _CONTROLLED_REF.fullmatch(source_ref) is None:
            raise ValueError("analyze_source source reference is invalid")
    else:
        raise ValueError("analyze_source input kind is invalid")
    if arguments.get("intent") not in {"organize", "summarize", "extract", "archive", "transcribe", "extract_images"}:
        raise ValueError("analyze_source intent is invalid")
    output_profile = arguments.get("output_profile")
    if not isinstance(output_profile, Mapping) or set(output_profile) != {"profile_id", "revision"}:
        raise ValueError("analyze_source output profile is invalid")
    _required(output_profile.get("profile_id"), "output profile id")
    _required(output_profile.get("revision"), "output profile revision")
    budget = arguments.get("resource_budget")
    if not isinstance(budget, Mapping) or set(budget) != {"max_assets", "max_bytes", "max_seconds"} or any(
        not isinstance(budget.get(key), int) or isinstance(budget.get(key), bool) or budget[key] < 1
        for key in ("max_assets", "max_bytes", "max_seconds")
    ):
        raise ValueError("analyze_source resource budget is invalid")
    return arguments


def _source_access(arguments: Mapping[str, object]) -> tuple[str, str | None]:
    access = arguments.get("access")
    if access is None:
        return "anonymous_public", None
    if not isinstance(access, Mapping) or set(access) != {
        "mode", "credential_subject_id",
    }:
        raise ValueError("analyze_source access is invalid")
    mode = access.get("mode")
    subject = access.get("credential_subject_id")
    if mode == "anonymous_public" and subject is None:
        return mode, None
    if (
        mode == "controlled_credential"
        and isinstance(subject, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", subject)
    ):
        return mode, subject
    raise ValueError("analyze_source access is invalid")


def _validate_resolved(value: ResolvedSourceManifest) -> None:
    if not isinstance(value, ResolvedSourceManifest):
        raise TypeError("source manifest resolver result is invalid")
    if value.terminal_reason is not None and value.terminal_reason not in {
        "unknown_platform", "ambiguous_platform", "provider_unavailable",
        "media_provider_unavailable", "unsupported_source",
        "controlled_credential_confirmed_none",
        "controlled_credential_pre_wire_drift",
        "controlled_credential_post_wire_unknown",
    }:
        raise ValueError("resolved source terminal reason is invalid")
    if value.manifest is None:
        if value.terminal_reason is None or value.manifest_ref is not None or value.manifest_revision is not None:
            raise ValueError("terminal source resolution is invalid")
        _refs(value.evidence_refs)
        return
    if not isinstance(value.manifest_ref, str) or _CONTROLLED_REF.fullmatch(value.manifest_ref) is None or "/source-manifests/" not in value.manifest_ref:
        raise ValueError("resolved manifest reference is invalid")
    _required(value.manifest_revision, "manifest revision")
    _refs(value.evidence_refs)
    if value.manifest.permission.decision == "granted" and value.permission_snapshot is None:
        raise ValueError("granted source manifest requires a permission snapshot")


def _manifest_evidence_refs(manifest: SourceManifest) -> tuple[str, ...]:
    return _refs((
        *manifest.provenance_refs,
        *manifest.permission.evidence_refs,
        *(ref for asset in manifest.assets for ref in asset.evidence_refs),
    ))


def _credential_use(
    manifest: SourceManifest | None, *, result: str
) -> dict[str, object] | None:
    if manifest is None or manifest.credential_binding is None:
        return None
    if result not in {"resolved", "admitted", "terminal"}:
        raise ValueError("credential use result is invalid")
    binding = manifest.credential_binding
    return {
        "authorization_revision": binding.authorization_revision,
        "secret_generation": binding.secret_generation,
        "result": result,
    }


def _manifest_artifact_id(manifest: SourceManifest) -> str:
    binding = manifest.credential_binding
    if binding is None:
        manifest_id = f"{manifest.source_id}--{manifest.resolver_revision}--{manifest.normalizer_revision}"
    else:
        authorization_id = binding.authorization_ref.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9._~-]{1,40}", authorization_id) is None:
            raise ValueError("controlled credential authorization identity is invalid")
        compact_authorization = (
            _base36(int(authorization_id, 16))
            if re.fullmatch(r"[0-9a-f]{32}", authorization_id)
            else authorization_id
        )
        manifest_id = (
            f"{manifest.source_id}--{manifest.resolver_revision}--{manifest.normalizer_revision}"
            f"--c{compact_authorization}-a{binding.authorization_revision}"
            f"-g{binding.secret_generation}-b{binding.boundary_profile_revision}"
        )
    if len(manifest_id) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", manifest_id) is None:
        raise ValueError("platform provider manifest identity is not portable")
    return manifest_id


def _canonical_expert_media_job_ref(source_id: str) -> str:
    """Map the governed source identity to the Kernel's opaque wait reference.

    The original Job id intentionally stays local to Media Hands.  Expert wait
    snapshots use this constrained reference so a model never supplies a Job
    id, path or provider argument.  Existing providers already publish a
    lower-case portable source id; an incompatible future provider fails before
    it can enter the resumable expert path.
    """
    if re.fullmatch(r"[a-z][a-z0-9-]{2,127}", source_id) is None:
        raise ValueError("source id is not eligible for an expert Media Job wait")
    return f"crp://jobs/{source_id}"


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result or "0"


def _platform_terminal_reason(status: str, reason: str | None) -> str | None:
    if status == "resolved":
        return None
    if reason == "unknown_platform":
        return "unknown_platform"
    if reason == "ambiguous_platform":
        return "ambiguous_platform"
    if reason == "provider_missing":
        return "provider_unavailable"
    return "unsupported_source"


def _refs(values: tuple[str, ...]) -> tuple[str, ...]:
    refs = tuple(dict.fromkeys(values))
    if any(not isinstance(ref, str) or _CONTROLLED_REF.fullmatch(ref) is None for ref in refs):
        raise ValueError("analyze_source evidence reference is invalid")
    return refs


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value
