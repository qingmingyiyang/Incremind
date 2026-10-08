"""Production adapters for the metadata-only runtime self-manifest contract.

This module deliberately accepts host facts as parameters.  It never reads an
environment mapping, process identifiers, listeners, origins, or credentials.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Literal

from core.ai_kernel import ContextEntry, TurnPayloadStorePort
from core.plugin_host.runtime_self_manifest import (
    RuntimeContract,
    RuntimeFacts,
    RuntimeInterpreter,
    RuntimeProbeResult,
    RuntimeRoots,
    RuntimeSelfManifestError,
    RuntimeSidecar,
    build_runtime_self_manifest,
    validate_runtime_self_manifest,
)


_PAYLOAD_KIND = "runtime-self-manifest-v1"
_CONTRACT_NAMES = ("application_skill", "plugin_hands", "effect_log")
_DISCLOSURES = frozenset({"model", "tool_only"})


@dataclass(frozen=True, slots=True)
class RuntimeSelfManifestTurnContext:
    """One immutable Turn payload and its matching ContextEntry."""

    payload_ref: str
    entry: ContextEntry
    manifest_revision: str


def build_runtime_self_manifest_for_app(
    root_dir: Path,
    *,
    packaged: bool,
    resources_root: Path | None = None,
    app_data_root: Path | None = None,
    runtime_source: Literal["override", "bundled", "system"] = "system",
    runtime_available: bool | None = None,
    sidecar_protocol_version: str = "desktop-loopback-1",
    sidecar_health: Literal["ready", "degraded"] = "ready",
    host_id: str = "desktop-electron",
    host_revision: str = "runtime-v1",
    application_skill_contract: tuple[str, str] = ("v1", "current"),
    plugin_hands_contract: tuple[str, str] = ("v1", "current"),
    effect_log_contract: tuple[str, str] = ("v1", "current"),
    features: Sequence[str] = (),
    probes: Sequence[RuntimeProbeResult] = (),
) -> dict[str, object]:
    """Build a host diagnostic manifest from explicit inputs and ``sys`` facts.

    Paths only influence the three presence states.  They are never serialized.
    ``sidecar_*`` values are the authenticated supervisor's already-sanitized
    state, not a request to inspect its environment or transport details.
    """
    repository = _root_state(root_dir)
    resources = _root_state(resources_root)
    app_data = _root_state(app_data_root)
    available = bool(runtime_available) if runtime_available is not None else True
    version = f"{sys.version_info.major}.{sys.version_info.minor}" if available else None
    contracts = _contracts(
        application_skill_contract, plugin_hands_contract, effect_log_contract
    )
    facts = RuntimeFacts(
        host_id=host_id,
        host_revision=host_revision,
        deployment_mode="packaged" if packaged else "development",
        platform=_platform(),
        architecture=_architecture(),
        roots=RuntimeRoots(repository=repository, resources=resources, app_data=app_data),
        interpreter=RuntimeInterpreter(
            source=runtime_source, available=available, version_major_minor=version,
        ),
        sidecar=RuntimeSidecar(
            protocol_version=sidecar_protocol_version,
            session_authenticated=True,
            health=sidecar_health,
        ),
        contracts=contracts,
        features=tuple(features),
        probes=tuple(probes),
    )
    return build_runtime_self_manifest(facts)


def freeze_runtime_self_manifest_for_turn(
    manifest: Mapping[str, object] | None,
    *,
    turn_id: str,
    payloads: TurnPayloadStorePort,
    disclosure: Literal["model", "tool_only"] = "tool_only",
    source_project_id: str | None = None,
) -> RuntimeSelfManifestTurnContext | None:
    """Freeze an optional manifest without changing Turn authorization state.

    A missing diagnostic manifest is a deliberate degradation: the caller can
    continue the Turn with no runtime context entry.
    """
    if manifest is None:
        return None
    if disclosure not in _DISCLOSURES:
        raise RuntimeSelfManifestError("runtime self-manifest disclosure is invalid")
    canonical = validate_runtime_self_manifest(manifest)
    payload_ref = payloads.get_or_create_immutable_payload(turn_id, _PAYLOAD_KIND, canonical)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    entry = ContextEntry(
        entry_id="context-entry-runtime-self-manifest",
        kind="runtime_self_manifest",
        source_ref=None,
        payload_ref=payload_ref,
        source_project_id=source_project_id,
        revision_identity=str(canonical["manifest_revision"]),
        content_fingerprint=None,
        provenance_refs=(),
        disclosure=disclosure,
        selection_reason="runtime_compatibility_diagnostics",
        content_bytes=len(encoded) if disclosure == "model" else 0,
    )
    return RuntimeSelfManifestTurnContext(payload_ref, entry, str(canonical["manifest_revision"]))


def authenticated_runtime_self_manifest_projection(
    manifest: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Return the safe authentication-API projection, or no diagnostic data."""
    if manifest is None:
        return None
    return validate_runtime_self_manifest(manifest)


def _root_state(value: Path | None) -> Literal["present", "unavailable"]:
    return "present" if value is not None and Path(value).exists() else "unavailable"


def _platform() -> Literal["win32", "darwin", "linux"]:
    values = {"win32": "win32", "darwin": "darwin", "linux": "linux"}
    try:
        return values[sys.platform]
    except KeyError as error:
        raise RuntimeSelfManifestError("unsupported runtime platform") from error


def _architecture() -> str:
    # sys.maxsize is a stable interpreter fact and does not expose host identity.
    return "x64" if sys.maxsize > 2**32 else "x86"


def _contracts(*values: tuple[str, str]) -> dict[str, RuntimeContract]:
    if len(values) != len(_CONTRACT_NAMES):
        raise RuntimeSelfManifestError("runtime contract inputs are invalid")
    result: dict[str, RuntimeContract] = {}
    for name, item in zip(_CONTRACT_NAMES, values, strict=True):
        if not isinstance(item, tuple) or len(item) != 2:
            raise RuntimeSelfManifestError("runtime contract input is invalid")
        result[name] = RuntimeContract(schema_version=item[0], revision=item[1])
    return result
