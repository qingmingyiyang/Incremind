from __future__ import annotations

from collections.abc import Mapping

from .ports import CapabilityManifest, ContextCompaction, ContextEntry, ContextManifest


_KINDS = frozenset({
    "capability_manifest", "input_ref", "project_skill", "memory_r0", "memory_r1",
    "memory_r2", "memory_r3", "session_history", "source", "output_style",
    "application_skill_snapshot", "application_skill",
    "model_routing_snapshot",
    "context_binding",
    "runtime_self_manifest",
    "world_state_projection",
    "context_summary",
    "tool_artifact",
    "task_graph_change",
    "agent_message",
})
_DISCLOSURES = frozenset({"model", "tool_only", "audit_only", "reference_only"})


class ContextManifestError(ValueError):
    pass


class V1TurnContextManifestResolver:
    """Compatibility resolver: preserves V1 refs as provenance without reading their bodies."""

    resolver_id = "v1-turn-context"

    def resolve(
        self,
        request: Mapping[str, object],
        capability_manifest_ref: str,
        capability_manifest: CapabilityManifest,
    ) -> ContextManifest:
        scope = _mapping(request.get("scope"), "turn scope")
        privacy = _mapping(request.get("privacy"), "turn privacy")
        policy = _mapping(request.get("context_policy"), "context policy")
        input_value = _mapping(request.get("input"), "turn input")
        turn_id = _text(request.get("turn_id"), "turn id")
        entries: list[ContextEntry] = [
            ContextEntry(
                entry_id="context-entry-capability-manifest",
                kind="capability_manifest",
                source_ref=None,
                payload_ref=capability_manifest_ref,
                source_project_id=_optional_text(scope.get("project_id"), "project id"),
                revision_identity=str(capability_manifest.profile_revision),
                content_fingerprint=None,
                provenance_refs=(),
                disclosure="tool_only",
                selection_reason="kernel_execution_boundary",
                content_bytes=0,
            )
        ]
        refs = input_value.get("refs")
        if not isinstance(refs, list):
            raise ContextManifestError("turn input refs must be an array")
        for index, value in enumerate(refs):
            item = _mapping(value, "turn input ref")
            entries.append(
                ContextEntry(
                    entry_id=f"context-entry-input-{index + 1}",
                    kind="input_ref",
                    source_ref=_opaque_ref(item.get("uri"), "input source ref"),
                    payload_ref=None,
                    source_project_id=_optional_text(scope.get("project_id"), "project id"),
                    revision_identity=None,
                    content_fingerprint=None,
                    provenance_refs=(),
                    disclosure="reference_only",
                    selection_reason="explicit_turn_input",
                    content_bytes=0,
                )
            )
        manifest = ContextManifest(
            manifest_id=f"context-manifest-{turn_id}",
            turn_id=turn_id,
            resolver_id=self.resolver_id,
            project_id=_optional_text(scope.get("project_id"), "project id"),
            series_id=_optional_text(scope.get("series_id"), "series id"),
            project_profile_id=capability_manifest.profile_id,
            project_profile_revision=capability_manifest.profile_revision,
            boundary_profile_id=f"v1-{_text(privacy.get('mode'), 'privacy mode')}",
            boundary_profile_revision=1,
            capability_manifest_ref=capability_manifest_ref,
            entries=tuple(entries),
            compactions=(),
            excluded_reason_counts=(),
            max_context_bytes=_integer(policy.get("max_context_bytes"), "maximum context bytes", minimum=1024),
            selected_context_bytes=0,
        )
        validate_context_manifest_for_request(manifest, request)
        return manifest


def context_manifest_to_payload(manifest: ContextManifest) -> dict[str, object]:
    _validate_manifest(manifest)
    return {
        "schema_version": "1.0.0",
        "manifest_id": manifest.manifest_id,
        "turn_id": manifest.turn_id,
        "resolver_id": manifest.resolver_id,
        "project_id": manifest.project_id,
        "series_id": manifest.series_id,
        "project_profile_id": manifest.project_profile_id,
        "project_profile_revision": manifest.project_profile_revision,
        "boundary_profile_id": manifest.boundary_profile_id,
        "boundary_profile_revision": manifest.boundary_profile_revision,
        "capability_manifest_ref": manifest.capability_manifest_ref,
        "entries": [_entry_to_payload(item) for item in manifest.entries],
        "compactions": [_compaction_to_payload(item) for item in manifest.compactions],
        "excluded_reason_counts": [
            {"reason": reason, "count": count}
            for reason, count in manifest.excluded_reason_counts
        ],
        "max_context_bytes": manifest.max_context_bytes,
        "selected_context_bytes": manifest.selected_context_bytes,
    }


def context_manifest_from_payload(value: object) -> ContextManifest:
    data = _mapping(value, "context manifest")
    fields = {
        "schema_version", "manifest_id", "turn_id", "resolver_id", "project_id", "series_id",
        "project_profile_id", "project_profile_revision", "boundary_profile_id",
        "boundary_profile_revision", "capability_manifest_ref", "entries", "compactions",
        "excluded_reason_counts", "max_context_bytes", "selected_context_bytes",
    }
    if {str(key) for key in data} != fields or data.get("schema_version") != "1.0.0":
        raise ContextManifestError("context manifest shape is invalid")
    entries_value = data.get("entries")
    compactions_value = data.get("compactions")
    if not isinstance(entries_value, list) or not isinstance(compactions_value, list):
        raise ContextManifestError("context manifest collections must be arrays")
    manifest = ContextManifest(
        manifest_id=_text(data.get("manifest_id"), "manifest id"),
        turn_id=_text(data.get("turn_id"), "turn id"),
        resolver_id=_text(data.get("resolver_id"), "resolver id"),
        project_id=_optional_text(data.get("project_id"), "project id"),
        series_id=_optional_text(data.get("series_id"), "series id"),
        project_profile_id=_text(data.get("project_profile_id"), "project profile id"),
        project_profile_revision=_integer(data.get("project_profile_revision"), "project profile revision", minimum=1),
        boundary_profile_id=_text(data.get("boundary_profile_id"), "boundary profile id"),
        boundary_profile_revision=_integer(data.get("boundary_profile_revision"), "boundary profile revision", minimum=1),
        capability_manifest_ref=_session_ref(data.get("capability_manifest_ref"), "capability manifest ref"),
        entries=tuple(_entry_from_payload(item) for item in entries_value),
        compactions=tuple(_compaction_from_payload(item) for item in compactions_value),
        excluded_reason_counts=_reason_counts(data.get("excluded_reason_counts")),
        max_context_bytes=_integer(data.get("max_context_bytes"), "maximum context bytes", minimum=1024),
        selected_context_bytes=_integer(data.get("selected_context_bytes"), "selected context bytes", minimum=0),
    )
    _validate_manifest(manifest)
    return manifest


def validate_context_manifest_for_request(
    manifest: ContextManifest,
    request: Mapping[str, object],
    *,
    capability_manifest: CapabilityManifest | None = None,
) -> ContextManifest:
    _validate_manifest(manifest)
    if manifest.turn_id != request.get("turn_id"):
        raise ContextManifestError("context manifest turn identity drifted")
    scope = _mapping(request.get("scope"), "turn scope")
    if manifest.project_id != scope.get("project_id") or manifest.series_id != scope.get("series_id"):
        raise ContextManifestError("context manifest scope drifted")
    policy = _mapping(request.get("context_policy"), "context policy")
    if manifest.max_context_bytes != policy.get("max_context_bytes"):
        raise ContextManifestError("context manifest budget drifted")
    if capability_manifest is not None:
        if (
            manifest.project_profile_id != capability_manifest.profile_id
            or manifest.project_profile_revision != capability_manifest.profile_revision
        ):
            raise ContextManifestError("context manifest capability profile binding drifted")
        if (
            capability_manifest.boundary_profile_id is not None
            and (
                manifest.boundary_profile_id != capability_manifest.boundary_profile_id
                or manifest.boundary_profile_revision != capability_manifest.boundary_profile_revision
            )
        ):
            raise ContextManifestError("context manifest Boundary binding drifted")
    prefix = f"crp://session/{manifest.turn_id}/"
    payload_refs = [manifest.capability_manifest_ref, *(item.payload_ref for item in manifest.entries if item.payload_ref)]
    if any(not ref.startswith(prefix) for ref in payload_refs):
        raise ContextManifestError("context manifest payload crossed Turn identity")
    return manifest


def context_payload_refs(manifest: ContextManifest) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        [manifest.capability_manifest_ref, *(item.payload_ref for item in manifest.entries if item.payload_ref)]
    ))


def compacted_source_entry_ids(manifest: ContextManifest) -> frozenset[str]:
    """Return entries replaced by a later manifest compaction output.

    A compaction remains part of immutable Turn provenance, but an external
    context map must only expose the terminal output.  This also handles
    chained compactions: an intermediate output becomes hidden once it is used
    as a source for a later output.
    """
    _validate_manifest(manifest)
    return frozenset(
        entry_id
        for compaction in manifest.compactions
        for entry_id in compaction.source_entry_ids
    )


def _validate_manifest(manifest: ContextManifest) -> None:
    for value, label in (
        (manifest.manifest_id, "manifest id"), (manifest.turn_id, "turn id"),
        (manifest.resolver_id, "resolver id"), (manifest.project_profile_id, "project profile id"),
        (manifest.boundary_profile_id, "boundary profile id"),
    ):
        _text(value, label)
    _session_ref(manifest.capability_manifest_ref, "capability manifest ref")
    if manifest.project_profile_revision < 1 or manifest.boundary_profile_revision < 1:
        raise ContextManifestError("context manifest profile revision is invalid")
    if manifest.max_context_bytes < 1024 or manifest.max_context_bytes > 1_048_576:
        raise ContextManifestError("context manifest maximum bytes is invalid")
    if manifest.selected_context_bytes < 0 or manifest.selected_context_bytes > manifest.max_context_bytes:
        raise ContextManifestError("context manifest exceeds byte budget")
    if sum(item.content_bytes for item in manifest.entries if item.disclosure == "model") != manifest.selected_context_bytes:
        raise ContextManifestError("context manifest selected byte total drifted")
    entry_ids = tuple(item.entry_id for item in manifest.entries)
    if len(entry_ids) != len(set(entry_ids)):
        raise ContextManifestError("context manifest entry identities must be unique")
    for entry in manifest.entries:
        _validate_entry(entry)
    entries_by_id = {entry.entry_id: entry for entry in manifest.entries}
    compaction_ids: set[str] = set()
    for compaction in manifest.compactions:
        if compaction.compaction_id in compaction_ids:
            raise ContextManifestError("context compaction identities must be unique")
        compaction_ids.add(compaction.compaction_id)
        _validate_compaction(compaction, entries_by_id)
    _validate_reason_counts(manifest.excluded_reason_counts)


def _validate_entry(entry: ContextEntry) -> None:
    _text(entry.entry_id, "context entry id")
    if entry.kind not in _KINDS:
        raise ContextManifestError("context entry kind is unsupported")
    if entry.disclosure not in _DISCLOSURES:
        raise ContextManifestError("context entry disclosure is unsupported")
    _text(entry.selection_reason, "context selection reason")
    if entry.source_ref is not None:
        _opaque_ref(entry.source_ref, "context source ref")
    if entry.payload_ref is not None:
        _session_ref(entry.payload_ref, "context payload ref")
    if entry.source_project_id is not None:
        _text(entry.source_project_id, "context source project id")
    if entry.revision_identity is not None:
        _text(entry.revision_identity, "context revision identity")
    if entry.content_fingerprint is not None:
        _text(entry.content_fingerprint, "context content fingerprint")
    _unique_refs(entry.provenance_refs, "context provenance refs")
    if entry.content_bytes < 0:
        raise ContextManifestError("context entry bytes must be non-negative")
    if entry.disclosure == "model" and entry.payload_ref is None:
        raise ContextManifestError("model context entry requires a payload ref")


def _validate_compaction(
    compaction: ContextCompaction,
    entries_by_id: Mapping[str, ContextEntry],
) -> None:
    _text(compaction.compaction_id, "context compaction id")
    _text(compaction.strategy, "context compaction strategy")
    if not compaction.source_entry_ids or len(compaction.source_entry_ids) != len(set(compaction.source_entry_ids)):
        raise ContextManifestError("context compaction sources are invalid")
    entry_ids = set(entries_by_id)
    if not set(compaction.source_entry_ids) <= entry_ids or compaction.output_entry_id not in entry_ids:
        raise ContextManifestError("context compaction lineage references unknown entries")
    if compaction.output_entry_id in compaction.source_entry_ids:
        raise ContextManifestError("context compaction output cannot be its own source")
    if compaction.input_bytes < 1 or compaction.output_bytes < 0 or compaction.output_bytes >= compaction.input_bytes:
        raise ContextManifestError("context compaction byte lineage is invalid")
    input_bytes = sum(entries_by_id[entry_id].content_bytes for entry_id in compaction.source_entry_ids)
    output = entries_by_id[compaction.output_entry_id]
    if input_bytes != compaction.input_bytes or output.content_bytes != compaction.output_bytes:
        raise ContextManifestError("context compaction byte accounting drifted")
    if output.kind != "context_summary" or output.disclosure != "model":
        raise ContextManifestError("context compaction output is invalid")


def _entry_to_payload(entry: ContextEntry) -> dict[str, object]:
    return {
        "entry_id": entry.entry_id, "kind": entry.kind, "source_ref": entry.source_ref,
        "payload_ref": entry.payload_ref, "source_project_id": entry.source_project_id,
        "revision_identity": entry.revision_identity, "content_fingerprint": entry.content_fingerprint,
        "provenance_refs": list(entry.provenance_refs), "disclosure": entry.disclosure,
        "selection_reason": entry.selection_reason, "content_bytes": entry.content_bytes,
    }


def _entry_from_payload(value: object) -> ContextEntry:
    data = _exact_mapping(value, {
        "entry_id", "kind", "source_ref", "payload_ref", "source_project_id",
        "revision_identity", "content_fingerprint",
        "provenance_refs", "disclosure", "selection_reason", "content_bytes",
    }, "context entry")
    provenance = data.get("provenance_refs")
    if not isinstance(provenance, list):
        raise ContextManifestError("context provenance refs must be an array")
    return ContextEntry(
        entry_id=_text(data.get("entry_id"), "context entry id"),
        kind=_text(data.get("kind"), "context entry kind"),
        source_ref=_optional_ref(data.get("source_ref"), "context source ref", session_only=False),
        payload_ref=_optional_ref(data.get("payload_ref"), "context payload ref", session_only=True),
        source_project_id=_optional_text(data.get("source_project_id"), "context source project id"),
        revision_identity=_optional_text(data.get("revision_identity"), "context revision identity"),
        content_fingerprint=_optional_text(data.get("content_fingerprint"), "context content fingerprint"),
        provenance_refs=tuple(_opaque_ref(item, "context provenance ref") for item in provenance),
        disclosure=_text(data.get("disclosure"), "context disclosure"),
        selection_reason=_text(data.get("selection_reason"), "context selection reason"),
        content_bytes=_integer(data.get("content_bytes"), "context content bytes", minimum=0),
    )


def _compaction_to_payload(item: ContextCompaction) -> dict[str, object]:
    return {
        "compaction_id": item.compaction_id, "strategy": item.strategy,
        "source_entry_ids": list(item.source_entry_ids), "output_entry_id": item.output_entry_id,
        "input_bytes": item.input_bytes, "output_bytes": item.output_bytes,
    }


def _compaction_from_payload(value: object) -> ContextCompaction:
    data = _exact_mapping(value, {
        "compaction_id", "strategy", "source_entry_ids", "output_entry_id", "input_bytes", "output_bytes",
    }, "context compaction")
    sources = data.get("source_entry_ids")
    if not isinstance(sources, list):
        raise ContextManifestError("context compaction sources must be an array")
    return ContextCompaction(
        compaction_id=_text(data.get("compaction_id"), "context compaction id"),
        strategy=_text(data.get("strategy"), "context compaction strategy"),
        source_entry_ids=tuple(_text(item, "context compaction source") for item in sources),
        output_entry_id=_text(data.get("output_entry_id"), "context compaction output"),
        input_bytes=_integer(data.get("input_bytes"), "context compaction input bytes", minimum=1),
        output_bytes=_integer(data.get("output_bytes"), "context compaction output bytes", minimum=0),
    )


def _reason_counts(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, list):
        raise ContextManifestError("context exclusion reasons must be an array")
    result = []
    for item in value:
        data = _exact_mapping(item, {"reason", "count"}, "context exclusion reason")
        result.append((_text(data.get("reason"), "context exclusion reason"), _integer(data.get("count"), "context exclusion count", minimum=1)))
    output = tuple(result)
    _validate_reason_counts(output)
    return output


def _validate_reason_counts(value: tuple[tuple[str, int], ...]) -> None:
    reasons = tuple(reason for reason, _ in value)
    if len(reasons) != len(set(reasons)):
        raise ContextManifestError("context exclusion reasons must be unique")
    for reason, count in value:
        _text(reason, "context exclusion reason")
        if count < 1:
            raise ContextManifestError("context exclusion count must be positive")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContextManifestError(f"{label} must be an object")
    return value


def _exact_mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    data = _mapping(value, label)
    if {str(key) for key in data} != fields:
        raise ContextManifestError(f"{label} shape is invalid")
    return data


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextManifestError(f"{label} must be non-empty")
    return value.strip()


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _integer(value: object, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContextManifestError(f"{label} is invalid")
    return value


def _opaque_ref(value: object, label: str) -> str:
    ref = _text(value, label)
    if not ref.startswith("crp://") or "/" not in ref[6:]:
        raise ContextManifestError(f"{label} must be an opaque crp ref")
    return ref


def _session_ref(value: object, label: str) -> str:
    ref = _opaque_ref(value, label)
    if not ref.startswith("crp://session/"):
        raise ContextManifestError(f"{label} must be a session ref")
    return ref


def _optional_ref(value: object, label: str, *, session_only: bool) -> str | None:
    if value is None:
        return None
    return _session_ref(value, label) if session_only else _opaque_ref(value, label)


def _unique_refs(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ContextManifestError(f"{label} must be unique")
    for value in values:
        _opaque_ref(value, label)
