from __future__ import annotations

from collections.abc import Mapping, Sequence

from .ports import CapabilityDefinition, CapabilityManifest


class CapabilityManifestError(ValueError):
    pass


class V1TurnPolicyCapabilityManifestResolver:
    """Compatibility resolver that applies the existing Turn policy before planning."""

    resolver_id = "v1-turn-policy"

    def resolve(
        self,
        request: Mapping[str, object],
        capabilities: Sequence[CapabilityDefinition],
    ) -> CapabilityManifest:
        policy = request.get("capability_policy")
        if not isinstance(policy, Mapping):
            raise CapabilityManifestError("turn capability policy is unavailable")
        allowed = _string_set(policy.get("allowed"), "allowed capabilities")
        denied = _string_set(policy.get("denied"), "denied capabilities")
        effective = tuple(
            sorted(
                definition.capability_id
                for definition in capabilities
                if definition.capability_id in allowed and definition.capability_id not in denied
            )
        )
        registered = {definition.capability_id for definition in capabilities}
        excluded = len(capabilities) - len(effective)
        unavailable = len((allowed - denied) - registered)
        reasons = []
        if excluded:
            reasons.append(("turn_restricted", excluded))
        if unavailable:
            reasons.append(("requested_unavailable", unavailable))
        selected = [item for item in capabilities if item.capability_id in effective]
        return CapabilityManifest(
            manifest_id=f"capability-manifest-{request['turn_id']}",
            turn_id=str(request["turn_id"]),
            resolver_id=self.resolver_id,
            profile_id="v1-turn-policy",
            profile_revision=1,
            capability_ids=effective,
            excluded_reason_counts=tuple(reasons),
            descriptor_bytes=sum(_descriptor_bytes(item) for item in selected),
            boundary_profile_id=f"v1-{_mapping(request.get('privacy'), 'turn privacy').get('mode')}",
            boundary_profile_revision=1,
        )


def manifest_to_payload(manifest: CapabilityManifest) -> dict[str, object]:
    _validate_manifest(manifest)
    payload = {
        "schema_version": "1.0.0",
        "manifest_id": manifest.manifest_id,
        "turn_id": manifest.turn_id,
        "resolver_id": manifest.resolver_id,
        "profile_id": manifest.profile_id,
        "profile_revision": manifest.profile_revision,
        "boundary_profile_id": manifest.boundary_profile_id,
        "boundary_profile_revision": manifest.boundary_profile_revision,
        "capability_ids": list(manifest.capability_ids),
        "excluded_reason_counts": [
            {"reason": reason, "count": count}
            for reason, count in manifest.excluded_reason_counts
        ],
        "descriptor_bytes": manifest.descriptor_bytes,
    }
    if manifest.application_skill_snapshot_ref is not None:
        payload["application_skill_snapshot_ref"] = manifest.application_skill_snapshot_ref
        payload["application_skill_snapshot_revision"] = manifest.application_skill_snapshot_revision
    if manifest.model_routing_snapshot_ref is not None:
        payload["model_routing_snapshot_ref"] = manifest.model_routing_snapshot_ref
        payload["model_routing_snapshot_revision"] = manifest.model_routing_snapshot_revision
    return payload


def manifest_from_payload(value: object) -> CapabilityManifest:
    if not isinstance(value, Mapping):
        raise CapabilityManifestError("capability manifest must be an object")
    fields = {
        "schema_version", "manifest_id", "turn_id", "resolver_id", "profile_id",
        "profile_revision", "capability_ids", "excluded_reason_counts", "descriptor_bytes",
        "boundary_profile_id", "boundary_profile_revision",
        "application_skill_snapshot_ref", "application_skill_snapshot_revision",
        "model_routing_snapshot_ref", "model_routing_snapshot_revision",
    }
    legacy_fields = fields - {
        "boundary_profile_id", "boundary_profile_revision",
        "application_skill_snapshot_ref", "application_skill_snapshot_revision",
        "model_routing_snapshot_ref", "model_routing_snapshot_revision",
    }
    boundary_only_fields = fields - {
        "application_skill_snapshot_ref", "application_skill_snapshot_revision",
        "model_routing_snapshot_ref", "model_routing_snapshot_revision",
    }
    boundary_and_skill_fields = fields - {
        "model_routing_snapshot_ref", "model_routing_snapshot_revision",
    }
    boundary_and_model_fields = fields - {
        "application_skill_snapshot_ref", "application_skill_snapshot_revision",
    }
    actual_fields = {str(key) for key in value}
    if actual_fields not in {
        frozenset(fields), frozenset(boundary_only_fields),
        frozenset(boundary_and_skill_fields), frozenset(boundary_and_model_fields),
        frozenset(legacy_fields),
    } or value.get("schema_version") != "1.0.0":
        raise CapabilityManifestError("capability manifest shape is invalid")
    reason_values = value.get("excluded_reason_counts")
    if not isinstance(reason_values, list):
        raise CapabilityManifestError("capability manifest reasons must be an array")
    reasons: list[tuple[str, int]] = []
    for item in reason_values:
        if not isinstance(item, Mapping) or set(item) != {"reason", "count"}:
            raise CapabilityManifestError("capability manifest reason is invalid")
        reason = _text(item.get("reason"), "manifest reason")
        count = item.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise CapabilityManifestError("capability manifest reason count is invalid")
        reasons.append((reason, count))
    manifest = CapabilityManifest(
        manifest_id=_text(value.get("manifest_id"), "manifest id"),
        turn_id=_text(value.get("turn_id"), "turn id"),
        resolver_id=_text(value.get("resolver_id"), "resolver id"),
        profile_id=_text(value.get("profile_id"), "profile id"),
        profile_revision=_positive_int(value.get("profile_revision"), "profile revision"),
        capability_ids=_string_tuple(value.get("capability_ids"), "capability ids"),
        excluded_reason_counts=tuple(reasons),
        descriptor_bytes=_non_negative_int(value.get("descriptor_bytes"), "descriptor bytes"),
        boundary_profile_id=(
            _text(value.get("boundary_profile_id"), "boundary profile id")
            if "boundary_profile_id" in value else None
        ),
        boundary_profile_revision=(
            _positive_int(value.get("boundary_profile_revision"), "boundary profile revision")
            if "boundary_profile_revision" in value else None
        ),
        application_skill_snapshot_ref=(
            _session_ref(value.get("application_skill_snapshot_ref"), "Application Skill snapshot ref")
            if "application_skill_snapshot_ref" in value else None
        ),
        application_skill_snapshot_revision=(
            _text(value.get("application_skill_snapshot_revision"), "Application Skill snapshot revision")
            if "application_skill_snapshot_revision" in value else None
        ),
        model_routing_snapshot_ref=(
            _session_ref(value.get("model_routing_snapshot_ref"), "model routing snapshot ref")
            if "model_routing_snapshot_ref" in value else None
        ),
        model_routing_snapshot_revision=(
            _text(value.get("model_routing_snapshot_revision"), "model routing snapshot revision")
            if "model_routing_snapshot_revision" in value else None
        ),
    )
    _validate_manifest(manifest)
    return manifest


def validate_manifest_for_request(
    manifest: CapabilityManifest,
    request: Mapping[str, object],
    registered: Sequence[CapabilityDefinition],
) -> CapabilityManifest:
    _validate_manifest(manifest)
    if manifest.turn_id != request.get("turn_id"):
        raise CapabilityManifestError("capability manifest turn identity drifted")
    policy = request.get("capability_policy")
    if not isinstance(policy, Mapping):
        raise CapabilityManifestError("turn capability policy is unavailable")
    allowed = _string_set(policy.get("allowed"), "allowed capabilities")
    denied = _string_set(policy.get("denied"), "denied capabilities")
    if not set(manifest.capability_ids) <= (allowed - denied):
        raise CapabilityManifestError("capability manifest expanded the Turn policy")
    registered_ids = {item.capability_id for item in registered}
    if not set(manifest.capability_ids) <= registered_ids:
        raise CapabilityManifestError("capability manifest contains unavailable capability")
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise CapabilityManifestError("turn scope is unavailable")
    if scope.get("kind") in {"project", "series"} and (
        manifest.boundary_profile_id is None or manifest.boundary_profile_revision is None
    ):
        raise CapabilityManifestError("project capability manifest lacks Boundary binding")
    return manifest


def _validate_manifest(manifest: CapabilityManifest) -> None:
    for value, label in (
        (manifest.manifest_id, "manifest id"),
        (manifest.turn_id, "turn id"),
        (manifest.resolver_id, "resolver id"),
        (manifest.profile_id, "profile id"),
    ):
        _text(value, label)
    if manifest.profile_revision < 1 or manifest.descriptor_bytes < 0:
        raise CapabilityManifestError("capability manifest numeric field is invalid")
    if (manifest.boundary_profile_id is None) != (manifest.boundary_profile_revision is None):
        raise CapabilityManifestError("capability manifest Boundary binding is incomplete")
    if manifest.boundary_profile_id is not None:
        _text(manifest.boundary_profile_id, "boundary profile id")
        if manifest.boundary_profile_revision is None or manifest.boundary_profile_revision < 1:
            raise CapabilityManifestError("capability manifest Boundary revision is invalid")
    if (manifest.application_skill_snapshot_ref is None) != (
        manifest.application_skill_snapshot_revision is None
    ):
        raise CapabilityManifestError("capability manifest Application Skill binding is incomplete")
    if manifest.application_skill_snapshot_ref is not None:
        _session_ref(manifest.application_skill_snapshot_ref, "Application Skill snapshot ref")
        _text(manifest.application_skill_snapshot_revision, "Application Skill snapshot revision")
    if (manifest.model_routing_snapshot_ref is None) != (
        manifest.model_routing_snapshot_revision is None
    ):
        raise CapabilityManifestError("capability manifest model routing binding is incomplete")
    if manifest.model_routing_snapshot_ref is not None:
        _session_ref(manifest.model_routing_snapshot_ref, "model routing snapshot ref")
        _text(manifest.model_routing_snapshot_revision, "model routing snapshot revision")
    if len(manifest.capability_ids) != len(set(manifest.capability_ids)):
        raise CapabilityManifestError("capability manifest identities must be unique")
    for capability_id in manifest.capability_ids:
        _text(capability_id, "capability id")


def _descriptor_bytes(item: CapabilityDefinition) -> int:
    return sum(
        len(value.encode("utf-8"))
        for value in (
            item.capability_id, str(item.version), item.mode,
            item.operation_semantics, item.input_schema_uri, item.output_schema_uri,
        )
    )


def _string_set(value: object, label: str) -> set[str]:
    return set(_string_tuple(value, label))


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CapabilityManifestError(f"{label} must be an object")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise CapabilityManifestError(f"{label} must be a string array")
    result = tuple(value)
    if len(result) != len(set(result)):
        raise CapabilityManifestError(f"{label} must be unique")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityManifestError(f"{label} must be non-empty")
    return value.strip()


def _session_ref(value: object, label: str) -> str:
    ref = _text(value, label)
    if not ref.startswith("crp://session/"):
        raise CapabilityManifestError(f"{label} must be a session ref")
    return ref


def _positive_int(value: object, label: str) -> int:
    result = _non_negative_int(value, label)
    if result < 1:
        raise CapabilityManifestError(f"{label} must be positive")
    return result


def _non_negative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CapabilityManifestError(f"{label} must be non-negative")
    return value
