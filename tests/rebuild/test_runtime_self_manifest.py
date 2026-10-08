from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.plugin_host.runtime_self_manifest import (
    RuntimeContract,
    RuntimeFacts,
    RuntimeInterpreter,
    RuntimeProbeResult,
    RuntimeRoots,
    RuntimeSelfManifest,
    RuntimeSelfManifestError,
    RuntimeSidecar,
    build_runtime_self_manifest,
    probe_runtime_self_manifest,
    validate_runtime_self_manifest,
)


def _facts(**overrides: object) -> RuntimeFacts:
    values: dict[str, object] = {
        "host_id": "electron-plugin-host", "host_revision": "host-r7",
        "deployment_mode": "packaged", "platform": "win32", "architecture": "x86_64",
        "roots": RuntimeRoots("present", "present", "present"),
        "interpreter": RuntimeInterpreter("bundled", True, "3.12"),
        "sidecar": RuntimeSidecar("sidecar-v1", True, "ready"),
        "contracts": {
            "application_skill": RuntimeContract("v1", "r7"),
            "plugin_hands": RuntimeContract("v1", "r4"),
            "effect_log": RuntimeContract("v1", "r9"),
        },
        "features": ("isolated-python-artifact", "plugin-runtime"),
        "probes": (RuntimeProbeResult("interpreter", "pass"), RuntimeProbeResult("sidecar", "pass")),
    }
    values.update(overrides)
    return RuntimeFacts(**values)  # type: ignore[arg-type]


def test_manifest_is_canonical_deterministic_and_diagnostic_only() -> None:
    first = build_runtime_self_manifest(_facts())
    second = build_runtime_self_manifest(_facts())

    assert first == second
    assert first["manifest_revision"].startswith("rsm-")
    assert first["runtime"] == {
        "deployment_mode": "packaged", "platform": "win32", "architecture": "x86_64",
        "roots": {"app_data": "present", "repository": "present", "resources": "present"},
        "interpreter": {"source": "bundled", "available": True, "version_major_minor": "3.12"},
        "sidecar": {"protocol_version": "sidecar-v1", "session_authenticated": True, "health": "ready"},
        "features": ["isolated-python-artifact", "plugin-runtime"],
    }
    assert "capabilities" not in first["runtime"]
    assert validate_runtime_self_manifest(first) == first


def test_ppt_master_requires_available_interpreter_and_isolated_artifact_feature() -> None:
    unavailable = build_runtime_self_manifest(_facts(interpreter=RuntimeInterpreter("bundled", False, None), features=("plugin-runtime",)))
    assert unavailable["compatibility"]["ppt-master"] == {
        "status": "incompatible",
        "requirements": ["interpreter_available", "isolated-python-artifact"],
        "reasons": ["interpreter_unavailable", "isolated-python-artifact_required"],
    }
    assert build_runtime_self_manifest(_facts())["compatibility"]["ppt-master"]["status"] == "compatible"


@pytest.mark.parametrize("field", ["python_path", "access_token", "secret", "nonce", "pid", "origin", "port", "permission", "authorization", "allowed"])
def test_manifest_rejects_paths_sensitive_and_authorization_semantics(field: str) -> None:
    manifest = build_runtime_self_manifest(_facts())
    manifest[field] = "C:/private/runtime"
    with pytest.raises(RuntimeSelfManifestError, match="forbidden field|absolute path|shape"):
        validate_runtime_self_manifest(manifest)


def test_manifest_rejects_noncanonical_revision_and_underived_compatibility() -> None:
    manifest = build_runtime_self_manifest(_facts())
    manifest["manifest_revision"] = "rsm-replayed"
    with pytest.raises(RuntimeSelfManifestError, match="manifest_revision"):
        validate_runtime_self_manifest(manifest)
    manifest = build_runtime_self_manifest(_facts(features=("plugin-runtime",)))
    manifest["compatibility"]["ppt-master"]["status"] = "compatible"
    with pytest.raises(RuntimeSelfManifestError, match="derived from runtime facts"):
        validate_runtime_self_manifest(manifest)


def test_manifest_requires_sorted_unique_diagnostic_features_and_probes() -> None:
    with pytest.raises(RuntimeSelfManifestError, match="features must be sorted"):
        build_runtime_self_manifest(_facts(features=("plugin-runtime", "isolated-python-artifact")))
    with pytest.raises(RuntimeSelfManifestError, match="probes must be sorted"):
        build_runtime_self_manifest(_facts(probes=(RuntimeProbeResult("sidecar", "pass"), RuntimeProbeResult("interpreter", "pass"))))


@dataclass
class _Probe:
    facts: RuntimeFacts

    def collect_runtime_facts(self) -> RuntimeFacts:
        return self.facts


def test_probe_boundary_and_manifest_value_are_immutable_from_callers() -> None:
    probe = _Probe(_facts())
    manifest = RuntimeSelfManifest.from_probe(probe)
    copied = manifest.to_payload()
    copied["runtime"]["features"].append("tampered")
    assert probe_runtime_self_manifest(probe) == build_runtime_self_manifest(_facts())
    assert manifest.to_payload()["runtime"]["features"] == ["isolated-python-artifact", "plugin-runtime"]
