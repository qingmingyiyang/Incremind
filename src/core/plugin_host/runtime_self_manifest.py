"""Host-owned, metadata-only description of the local plugin runtime.

This is a diagnostic compatibility contract. It is deliberately not an
authorization decision, installation plan, process inventory, or environment
dump. All locations are represented only as ``present`` or ``unavailable``.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, Protocol


RUNTIME_SELF_MANIFEST_SCHEMA_VERSION = "1.0.0"

_FIELDS = frozenset({"schema_version", "manifest_revision", "host", "runtime", "contracts", "probes", "compatibility"})
_HOST_FIELDS = frozenset({"id", "revision"})
_RUNTIME_FIELDS = frozenset({"deployment_mode", "platform", "architecture", "roots", "interpreter", "sidecar", "features"})
_ROOT_FIELDS = frozenset({"repository", "resources", "app_data"})
_INTERPRETER_FIELDS = frozenset({"source", "available", "version_major_minor"})
_SIDECAR_FIELDS = frozenset({"protocol_version", "session_authenticated", "health"})
_CONTRACT_NAMES = frozenset({"application_skill", "plugin_hands", "effect_log"})
_CONTRACT_FIELDS = frozenset({"schema_version", "revision"})
_PROBE_FIELDS = frozenset({"id", "status"})
_COMPATIBILITY_FIELDS = frozenset({"ppt-master"})
_COMPATIBILITY_ENTRY_FIELDS = frozenset({"status", "requirements", "reasons"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+$")
_ABSOLUTE_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^/|^\\\\|(?:^|\s)file:)", re.IGNORECASE)
_FORBIDDEN_SEMANTICS = re.compile(r"(?:authori[sz]|permission|allowed|grant|privilege|access[_-]?control)", re.IGNORECASE)
_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|credential|cookie|api[_-]?key|nonce|pid|process|origin|port)", re.IGNORECASE)
_SENSITIVE_TEXT = re.compile(r"(?:token|secret|password|credential|cookie|api[_-]?key|bearer|nonce|\bpid\b|\borigin\b|\bport\b)", re.IGNORECASE)
_DEPLOYMENT_MODES = frozenset({"development", "packaged"})
_PLATFORMS = frozenset({"win32", "darwin", "linux"})
_ROOT_STATES = frozenset({"present", "unavailable"})
_INTERPRETER_SOURCES = frozenset({"override", "bundled", "system"})
_HEALTH_STATES = frozenset({"ready", "degraded"})
_PROBE_STATUSES = frozenset({"pass", "fail", "unavailable"})
_PPT_MASTER_REQUIREMENTS = ("interpreter_available", "isolated-python-artifact")


class RuntimeSelfManifestError(ValueError):
    """Raised when a runtime self-manifest is malformed, unsafe, or noncanonical."""


@dataclass(frozen=True)
class RuntimeRoots:
    repository: Literal["present", "unavailable"]
    resources: Literal["present", "unavailable"]
    app_data: Literal["present", "unavailable"]


@dataclass(frozen=True)
class RuntimeInterpreter:
    source: Literal["override", "bundled", "system"]
    available: bool
    version_major_minor: str | None


@dataclass(frozen=True)
class RuntimeSidecar:
    protocol_version: str
    session_authenticated: Literal[True]
    health: Literal["ready", "degraded"]


@dataclass(frozen=True)
class RuntimeContract:
    schema_version: str
    revision: str


@dataclass(frozen=True)
class RuntimeProbeResult:
    id: str
    status: Literal["pass", "fail", "unavailable"]


@dataclass(frozen=True)
class RuntimeFacts:
    """Narrow host-collected facts suitable for diagnostics.

    This type has no path, command, process, listener, credential, permission
    or authorization field. A host can report that a root exists, never where
    it is located.
    """

    host_id: str
    host_revision: str
    deployment_mode: Literal["development", "packaged"]
    platform: Literal["win32", "darwin", "linux"]
    architecture: str
    roots: RuntimeRoots
    interpreter: RuntimeInterpreter
    sidecar: RuntimeSidecar
    contracts: Mapping[str, RuntimeContract]
    features: tuple[str, ...] = ()
    probes: tuple[RuntimeProbeResult, ...] = ()


class RuntimeProbe(Protocol):
    """Host-side probe boundary; it may only publish :class:`RuntimeFacts`."""

    def collect_runtime_facts(self) -> RuntimeFacts:
        """Return the intentionally limited facts visible to consumers."""


@dataclass(frozen=True)
class RuntimeSelfManifest:
    """Immutable view over a canonical runtime compatibility manifest."""

    payload: Mapping[str, object]

    @property
    def revision(self) -> str:
        return str(self.payload["manifest_revision"])

    def to_payload(self) -> dict[str, object]:
        return deepcopy(dict(self.payload))

    @classmethod
    def from_probe(cls, probe: RuntimeProbe) -> "RuntimeSelfManifest":
        if not hasattr(probe, "collect_runtime_facts"):
            raise RuntimeSelfManifestError("runtime probe must collect RuntimeFacts")
        return cls(build_runtime_self_manifest(probe.collect_runtime_facts()))


def build_runtime_self_manifest(facts: RuntimeFacts) -> dict[str, object]:
    """Build the sole canonical manifest from host-collected diagnostic facts."""
    if not isinstance(facts, RuntimeFacts):
        raise RuntimeSelfManifestError("facts must be RuntimeFacts collected by the host")
    payload: dict[str, object] = {
        "schema_version": RUNTIME_SELF_MANIFEST_SCHEMA_VERSION,
        "host": {"id": _identifier(facts.host_id, "host_id"), "revision": _identifier(facts.host_revision, "host_revision")},
        "runtime": _runtime_payload(facts),
        "contracts": _contracts(facts.contracts),
        "probes": _probes(facts.probes),
    }
    payload["compatibility"] = {"ppt-master": _ppt_master_compatibility(payload["runtime"])}
    payload["manifest_revision"] = _canonical_revision(payload)
    return validate_runtime_self_manifest(payload)


def probe_runtime_self_manifest(probe: RuntimeProbe) -> dict[str, object]:
    """Collect host facts and return a canonical manifest without side effects."""
    return RuntimeSelfManifest.from_probe(probe).to_payload()


def validate_runtime_self_manifest(manifest: Mapping[str, object]) -> dict[str, object]:
    """Fail closed for unsafe fields and compatibility not derived from facts."""
    if not isinstance(manifest, Mapping):
        raise RuntimeSelfManifestError("runtime self-manifest must be a mapping")
    _reject_forbidden_material(manifest)
    _exact_fields(manifest, _FIELDS, "runtime self-manifest")
    if manifest.get("schema_version") != RUNTIME_SELF_MANIFEST_SCHEMA_VERSION:
        raise RuntimeSelfManifestError("runtime self-manifest schema_version is invalid")
    host = _mapping(manifest.get("host"), "host")
    _exact_fields(host, _HOST_FIELDS, "host")
    canonical: dict[str, object] = {
        "schema_version": RUNTIME_SELF_MANIFEST_SCHEMA_VERSION,
        "host": {"id": _identifier(host.get("id"), "host.id"), "revision": _identifier(host.get("revision"), "host.revision")},
        "runtime": _validate_runtime(manifest.get("runtime")),
        "contracts": _contracts(manifest.get("contracts")),
        "probes": _probes(manifest.get("probes")),
    }
    canonical["compatibility"] = {"ppt-master": _validate_ppt_master(manifest.get("compatibility"), canonical["runtime"])}
    expected_revision = _canonical_revision(canonical)
    if manifest.get("manifest_revision") != expected_revision:
        raise RuntimeSelfManifestError("manifest_revision does not match canonical runtime self-manifest")
    canonical["manifest_revision"] = expected_revision
    return canonical


def _runtime_payload(facts: RuntimeFacts) -> dict[str, object]:
    if not isinstance(facts.roots, RuntimeRoots) or not isinstance(facts.interpreter, RuntimeInterpreter) or not isinstance(facts.sidecar, RuntimeSidecar):
        raise RuntimeSelfManifestError("runtime facts require typed roots, interpreter, and sidecar")
    return {
        "deployment_mode": _state(facts.deployment_mode, _DEPLOYMENT_MODES, "deployment_mode"),
        "platform": _state(facts.platform, _PLATFORMS, "platform"),
        "architecture": _identifier(facts.architecture, "architecture"),
        "roots": {key: _state(getattr(facts.roots, key), _ROOT_STATES, f"roots.{key}") for key in sorted(_ROOT_FIELDS)},
        "interpreter": _interpreter(facts.interpreter),
        "sidecar": _sidecar(facts.sidecar),
        "features": list(_features(facts.features)),
    }


def _validate_runtime(value: object) -> dict[str, object]:
    runtime = _mapping(value, "runtime")
    _exact_fields(runtime, _RUNTIME_FIELDS, "runtime")
    roots = _mapping(runtime.get("roots"), "runtime.roots")
    _exact_fields(roots, _ROOT_FIELDS, "runtime.roots")
    return {
        "deployment_mode": _state(runtime.get("deployment_mode"), _DEPLOYMENT_MODES, "runtime.deployment_mode"),
        "platform": _state(runtime.get("platform"), _PLATFORMS, "runtime.platform"),
        "architecture": _identifier(runtime.get("architecture"), "runtime.architecture"),
        "roots": {key: _state(roots.get(key), _ROOT_STATES, f"runtime.roots.{key}") for key in sorted(_ROOT_FIELDS)},
        "interpreter": _interpreter(runtime.get("interpreter")),
        "sidecar": _sidecar(runtime.get("sidecar")),
        "features": list(_features(runtime.get("features"))),
    }


def _interpreter(value: object) -> dict[str, object]:
    if isinstance(value, RuntimeInterpreter):
        source, available, version = value.source, value.available, value.version_major_minor
    else:
        raw = _mapping(value, "interpreter")
        _exact_fields(raw, _INTERPRETER_FIELDS, "interpreter")
        source, available, version = raw.get("source"), raw.get("available"), raw.get("version_major_minor")
    source = _state(source, _INTERPRETER_SOURCES, "interpreter.source")
    if not isinstance(available, bool):
        raise RuntimeSelfManifestError("interpreter.available must be boolean")
    if available:
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise RuntimeSelfManifestError("available interpreter requires major.minor version")
    elif version is not None:
        raise RuntimeSelfManifestError("unavailable interpreter must not report a version")
    return {"source": source, "available": available, "version_major_minor": version}


def _sidecar(value: object) -> dict[str, object]:
    if isinstance(value, RuntimeSidecar):
        protocol, authenticated, health = value.protocol_version, value.session_authenticated, value.health
    else:
        raw = _mapping(value, "sidecar")
        _exact_fields(raw, _SIDECAR_FIELDS, "sidecar")
        protocol, authenticated, health = raw.get("protocol_version"), raw.get("session_authenticated"), raw.get("health")
    if authenticated is not True:
        raise RuntimeSelfManifestError("sidecar.session_authenticated must be true")
    return {"protocol_version": _identifier(protocol, "sidecar.protocol_version"), "session_authenticated": True, "health": _state(health, _HEALTH_STATES, "sidecar.health")}


def _contracts(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeSelfManifestError("contracts must be a mapping")
    _exact_fields(value, _CONTRACT_NAMES, "contracts")
    result: dict[str, object] = {}
    for name in sorted(_CONTRACT_NAMES):
        item = value.get(name)
        if isinstance(item, RuntimeContract):
            schema_version, revision = item.schema_version, item.revision
        else:
            raw = _mapping(item, f"contracts.{name}")
            _exact_fields(raw, _CONTRACT_FIELDS, f"contracts.{name}")
            schema_version, revision = raw.get("schema_version"), raw.get("revision")
        result[name] = {"schema_version": _identifier(schema_version, f"contracts.{name}.schema_version"), "revision": _identifier(revision, f"contracts.{name}.revision")}
    return result


def _probes(value: object) -> list[dict[str, str]]:
    if not isinstance(value, (tuple, list)):
        raise RuntimeSelfManifestError("probes must be an array")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, RuntimeProbeResult):
            probe_id, status = item.id, item.status
        else:
            raw = _mapping(item, f"probes[{index}]")
            _exact_fields(raw, _PROBE_FIELDS, f"probes[{index}]")
            probe_id, status = raw.get("id"), raw.get("status")
        result.append({"id": _identifier(probe_id, f"probes[{index}].id"), "status": _state(status, _PROBE_STATUSES, f"probes[{index}].status")})
    if [item["id"] for item in result] != sorted(item["id"] for item in result) or len({item["id"] for item in result}) != len(result):
        raise RuntimeSelfManifestError("probes must be sorted and unique by id")
    return result


def _features(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise RuntimeSelfManifestError("features must be an array of safe identifiers")
    features = tuple(_identifier(item, f"features[{index}]") for index, item in enumerate(value))
    if tuple(sorted(features)) != features or len(set(features)) != len(features):
        raise RuntimeSelfManifestError("features must be sorted and unique")
    return features


def _ppt_master_compatibility(runtime: object) -> dict[str, object]:
    facts = _validate_runtime(runtime)
    reasons: list[str] = []
    if facts["interpreter"]["available"] is not True:
        reasons.append("interpreter_unavailable")
    if "isolated-python-artifact" not in facts["features"]:
        reasons.append("isolated-python-artifact_required")
    return {"status": "compatible" if not reasons else "incompatible", "requirements": list(_PPT_MASTER_REQUIREMENTS), "reasons": reasons}


def _validate_ppt_master(value: object, runtime: object) -> dict[str, object]:
    compatibility = _mapping(value, "compatibility")
    _exact_fields(compatibility, _COMPATIBILITY_FIELDS, "compatibility")
    entry = _mapping(compatibility.get("ppt-master"), "compatibility.ppt-master")
    _exact_fields(entry, _COMPATIBILITY_ENTRY_FIELDS, "compatibility.ppt-master")
    expected = _ppt_master_compatibility(runtime)
    if entry != expected:
        raise RuntimeSelfManifestError("ppt-master compatibility must be derived from runtime facts")
    return expected


def _canonical_revision(payload: Mapping[str, object]) -> str:
    identity = {key: payload[key] for key in sorted(_FIELDS - {"manifest_revision"})}
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "rsm-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeSelfManifestError(f"{label} must be a mapping")
    return value


def _exact_fields(value: Mapping[str, object], fields: frozenset[str], label: str) -> None:
    keys = {str(key) for key in value}
    if keys != fields:
        unknown, missing = sorted(keys - fields), sorted(fields - keys)
        detail = f"unknown fields: {unknown}" if unknown else f"missing fields: {missing}"
        raise RuntimeSelfManifestError(f"{label} shape is invalid ({detail})")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise RuntimeSelfManifestError(f"{label} must be a safe opaque identifier")
    return value


def _state(value: object, states: frozenset[str], label: str) -> str:
    if value not in states:
        raise RuntimeSelfManifestError(f"{label} must be one of {sorted(states)}")
    return str(value)


def _reject_forbidden_material(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _FORBIDDEN_SEMANTICS.search(key) or _SENSITIVE_KEY.search(key):
                raise RuntimeSelfManifestError("runtime self-manifest contains a forbidden field")
            _reject_forbidden_material(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_material(item)
    elif isinstance(value, str):
        if _ABSOLUTE_PATH.search(value):
            raise RuntimeSelfManifestError("runtime self-manifest must not contain an absolute path")
        if _FORBIDDEN_SEMANTICS.search(value) or _SENSITIVE_TEXT.search(value):
            raise RuntimeSelfManifestError("runtime self-manifest contains forbidden material")
