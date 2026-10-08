from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Protocol
from uuid import uuid4

from .context_manifest import (
    ContextManifestError,
    compacted_source_entry_ids,
    context_manifest_from_payload,
    validate_context_manifest_for_request,
)
from .state_store import TurnStateConflict


_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:file:/+(?:[a-z]:)?/|(?<![a-z0-9])[a-z]:[\\/]|[\\/]{2}[a-z0-9._-]+[\\/]"
    r"|(?<![:\w/])/(?!/)[a-z0-9._~-]+/)"
)
_SAFE_CRP_REF = re.compile(r"^crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}$")
_SECRET_VALUE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer|cookie\s*[:=]|api[_-]?key\s*[:=]|"
    r"(?:access|refresh|session)?[_-]?token\s*[:=]|(?:client[_-]?)?secret\s*[:=]|"
    r"password\s*[:=]|private[_-]?key\s*[:=]|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "cookies", "secret", "password", "api_key",
    "apikey", "access_token", "refresh_token", "provider_key",
})
_SUPPORTED_CONTEXT_KINDS = frozenset({
    "application_skill", "project_skill", "memory_r1", "memory_r2", "memory_r3",
})


class ExternalAgentContextError(ValueError):
    pass


class ExternalAgentContextConflict(ExternalAgentContextError):
    pass


class ExternalAgentCursorGap(ExternalAgentContextConflict):
    """A retained feed no longer contains the client's requested cursor range."""

    def __init__(
        self, *, project_id: str, after_cursor: int, retained_after_cursor: int,
        earliest_available_cursor: int, head_cursor: int,
    ) -> None:
        super().__init__("project event cursor falls before retained authority; rebase is required")
        self.project_id = project_id
        self.after_cursor = after_cursor
        self.retained_after_cursor = retained_after_cursor
        self.earliest_available_cursor = earliest_available_cursor
        self.head_cursor = head_cursor

    def public_payload(self) -> dict[str, object]:
        return {
            "code": "cursor_retention_gap",
            "project_id": self.project_id,
            "after_cursor": self.after_cursor,
            "retained_after_cursor": self.retained_after_cursor,
            "earliest_available_cursor": self.earliest_available_cursor,
            "head_cursor": self.head_cursor,
            "rebase_required": True,
            "rebase_action": "start_session",
        }


@dataclass(frozen=True, slots=True)
class AgentAdapterProfile:
    adapter_id: str
    revision: int
    template_revision: str
    maximum_context_bytes: int
    supported_purposes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _ADAPTER_ID.fullmatch(self.adapter_id):
            raise ExternalAgentContextError("adapter identity is invalid")
        if self.revision < 1 or not self.template_revision.strip():
            raise ExternalAgentContextError("adapter revision is invalid")
        if not 1024 <= self.maximum_context_bytes <= 1_048_576:
            raise ExternalAgentContextError("adapter context budget is invalid")
        if not self.supported_purposes or len(set(self.supported_purposes)) != len(self.supported_purposes):
            raise ExternalAgentContextError("adapter purposes are invalid")
        if any(not _PURPOSE.fullmatch(item) for item in self.supported_purposes):
            raise ExternalAgentContextError("adapter purpose is invalid")


@dataclass(frozen=True, slots=True)
class ExternalAgentAuthoritySnapshot:
    project_id: str
    project_profile_id: str
    project_profile_revision: int
    boundary_profile_id: str
    boundary_profile_revision: int


@dataclass(frozen=True, slots=True)
class ExternalAgentAdmissionSnapshot:
    admission_id: str
    project_id: str
    adapter_id: str
    outcome: str
    policy_revision: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not _OPERATION_ID.fullmatch(self.admission_id)
            or not self.project_id.strip()
            or not _ADAPTER_ID.fullmatch(self.adapter_id)
            or self.outcome != "allow"
            or self.policy_revision < 1
            or not self.reason_codes
            or any(not isinstance(item, str) or not item.strip() for item in self.reason_codes)
        ):
            raise ExternalAgentContextError("external agent admission is invalid")


class ExternalAgentSessionStorePort(Protocol):
    def get_request(self, turn_id: str) -> Mapping[str, object] | None: ...
    def events_after(self, turn_id: str, after_sequence: int = 0) -> Sequence[Mapping[str, object]]: ...
    def get(self, payload_ref: str) -> object: ...
    def project_event_cursor(self, project_id: str) -> int: ...
    def project_event_bounds(self, project_id: str) -> Mapping[str, int]: ...
    def project_events_after(
        self, project_id: str, after_cursor: int = 0, *, limit: int = 128,
        until_cursor: int | None = None,
    ) -> Sequence[Mapping[str, object]]: ...
    def create_external_agent_session(
        self, session_id: str, payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...
    def get_external_agent_start_receipt(
        self, operation_id: str, *, request: Mapping[str, object],
    ) -> Mapping[str, object] | None: ...
    def create_external_agent_session_with_receipt(
        self, session_id: str, payload: Mapping[str, object], *, operation_id: str,
        request: Mapping[str, object], receipt: Mapping[str, object],
    ) -> Mapping[str, object]: ...
    def get_external_agent_session(self, session_id: str) -> Mapping[str, object] | None: ...
    def reserve_external_agent_context_bytes(
        self, session_id: str, *, expected_resolved_bytes: int,
        additional_bytes: int, maximum_bytes: int,
    ) -> int: ...
    def get_external_agent_read_receipt(
        self, operation_id: str, *, session_id: str, operation: str,
        request: Mapping[str, object],
    ) -> Mapping[str, object] | None: ...
    def reserve_external_agent_context_bytes_with_receipt(
        self, session_id: str, *, operation_id: str, request: Mapping[str, object],
        expected_resolved_bytes: int, additional_bytes: int, maximum_bytes: int,
        receipt: Mapping[str, object],
    ) -> Mapping[str, object]: ...
    def record_external_agent_delivery_with_receipt(
        self, session_id: str, *, operation_id: str, request: Mapping[str, object],
        expected_delivered_cursor: int, delivered_cursor: int,
        receipt: Mapping[str, object],
    ) -> Mapping[str, object]: ...
    def acknowledge_external_agent_delivery_with_receipt(
        self, session_id: str, *, operation_id: str, request: Mapping[str, object],
        acknowledged_cursor: int, receipt: Mapping[str, object],
    ) -> Mapping[str, object]: ...
    def external_agent_resolved_context_refs(self, session_id: str) -> Sequence[str]: ...
    def reserve_external_agent_proposal_operation(
        self, session_id: str, *, operation_id: str, project_id: str,
        request: Mapping[str, object], created_at: str,
    ) -> Mapping[str, object]: ...
    def finalize_external_agent_proposal_operation(
        self, session_id: str, *, operation_id: str, project_id: str,
        request: Mapping[str, object], proposal_ref: str, proposal_revision: str,
        result: Mapping[str, object], occurred_at: str,
    ) -> Mapping[str, object]: ...
    def prepared_external_agent_proposal_operations(
        self, *, limit: int = 32,
    ) -> Sequence[Mapping[str, object]]: ...
    def defer_external_agent_proposal_operation(
        self, operation_id: str, *, updated_at: str,
    ) -> None: ...


class ExternalAgentContextBridge:
    """Read-only projection over existing Turn context and Session events."""

    def __init__(
        self,
        *,
        store: ExternalAgentSessionStorePort,
        adapters: Sequence[AgentAdapterProfile],
        authority: Callable[[str], ExternalAgentAuthoritySnapshot],
        proposal_sink: Callable[[Mapping[str, object], str], Mapping[str, object]] | None = None,
        clock: Callable[[], datetime] | None = None,
        session_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._store = store
        self._adapters = {item.adapter_id: item for item in adapters}
        if len(self._adapters) != len(tuple(adapters)):
            raise ExternalAgentContextError("adapter identities must be unique")
        self._authority = authority
        self._proposal_sink = proposal_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if not timedelta(seconds=1) <= session_ttl <= timedelta(hours=1):
            raise ExternalAgentContextError("external agent session ttl is invalid")
        self._session_ttl = session_ttl

    def start_session(
        self,
        *,
        operation_id: str,
        adapter_id: str,
        adapter_revision: int,
        template_revision: str,
        turn_id: str,
        project_id: str,
        purpose: str,
        requested_context_bytes: int,
        admission: ExternalAgentAdmissionSnapshot,
    ) -> Mapping[str, object]:
        adapter = self._adapters.get(adapter_id)
        if (
            adapter is None
            or adapter.revision != adapter_revision
            or adapter.template_revision != template_revision
        ):
            raise ExternalAgentContextError("adapter revision is not admitted")
        if purpose not in adapter.supported_purposes:
            raise ExternalAgentContextError("adapter purpose is not admitted")
        if not isinstance(requested_context_bytes, int) or isinstance(requested_context_bytes, bool):
            raise ExternalAgentContextError("requested context budget is invalid")
        if not 1024 <= requested_context_bytes <= adapter.maximum_context_bytes:
            raise ExternalAgentContextError("requested context budget is invalid")
        if (
            admission.project_id != project_id
            or admission.adapter_id != adapter_id
            or admission.policy_revision < 1
        ):
            raise ExternalAgentContextError("external agent admission is invalid")
        operation_request = _operation_request(
            operation_id, "start_session", adapter_id=adapter_id,
            adapter_revision=adapter_revision, template_revision=template_revision,
            turn_id=turn_id, project_id=project_id, purpose=purpose,
            requested_context_bytes=requested_context_bytes,
            admission_id=admission.admission_id,
            admission_policy_revision=admission.policy_revision,
            admission_reason_codes=admission.reason_codes,
        )
        try:
            start_replay = self._store.get_external_agent_start_receipt(
                operation_id, request=operation_request,
            )
        except TurnStateConflict as error:
            raise ExternalAgentContextConflict("external agent operation identity drifted") from error
        if start_replay is not None:
            replay_session = start_replay.get("session_id")
            if not isinstance(replay_session, str):
                raise ExternalAgentContextError("external agent start receipt is invalid")
            return _initial_context_map(self._require_session(replay_session, purpose=purpose))
        request = self._store.get_request(turn_id)
        if request is None or _request_project(request) != project_id:
            raise ExternalAgentContextError("external agent context target was not found")
        context_event = _latest_context_event(self._store.events_after(turn_id))
        try:
            manifest_ref = str(_event_data(context_event).get("payload_ref", ""))
            manifest = context_manifest_from_payload(self._store.get(manifest_ref))
            validate_context_manifest_for_request(manifest, request)
        except (ContextManifestError, KeyError, TypeError, ValueError) as error:
            raise ExternalAgentContextError("Turn context authority is unavailable") from error
        if manifest.project_id != project_id:
            raise ExternalAgentContextError("external agent context target was not found")
        snapshot = self._authority(project_id)
        _assert_authority(manifest, snapshot)
        if admission.policy_revision != snapshot.boundary_profile_revision:
            raise ExternalAgentContextConflict("external agent admission revision drifted")
        maximum = min(requested_context_bytes, manifest.max_context_bytes, adapter.maximum_context_bytes)
        compacted_sources = compacted_source_entry_ids(manifest)
        refs = []
        for entry in manifest.entries:
            if entry.entry_id in compacted_sources:
                continue
            if entry.disclosure != "model" or entry.payload_ref is None:
                continue
            if entry.kind not in _SUPPORTED_CONTEXT_KINDS:
                continue
            if entry.source_project_id != project_id:
                raise ExternalAgentContextError("context manifest crossed project scope")
            ref = {
                "context_ref": entry.payload_ref,
                "kind": entry.kind,
                "revision": entry.revision_identity,
                "maximum_bytes": entry.content_bytes,
            }
            if entry.kind != "application_skill":
                _validate_published_memory_context_entry(entry, project_id)
                ref.update({
                    "source_ref": entry.source_ref,
                    "provenance_refs": list(entry.provenance_refs),
                })
            refs.append(ref)
        now = _aware_utc(self._clock())
        event_bounds = self._store.project_event_bounds(project_id)
        event_cursor = _cursor_bound(event_bounds, "head_cursor")
        retained_after_cursor = _cursor_bound(event_bounds, "retained_after_cursor")
        earliest_available_cursor = _cursor_bound(event_bounds, "earliest_available_cursor")
        session_id = f"agent-session-{uuid4().hex}"
        payload = {
            "schema_version": "1.0.0",
            "session_id": session_id,
            "adapter_id": adapter.adapter_id,
            "adapter_revision": adapter.revision,
            "template_revision": adapter.template_revision,
            "turn_id": turn_id,
            "project_id": project_id,
            "project_profile_id": manifest.project_profile_id,
            "project_profile_revision": manifest.project_profile_revision,
            "boundary_profile_id": manifest.boundary_profile_id,
            "boundary_profile_revision": manifest.boundary_profile_revision,
            "context_manifest_ref": manifest_ref,
            "context_manifest_revision": manifest.manifest_id,
            "purpose": purpose,
            "admission_id": admission.admission_id,
            "admission_policy_revision": admission.policy_revision,
            "admission_reason_codes": list(admission.reason_codes),
            "maximum_context_bytes": maximum,
            "context_refs": refs,
            "event_cursor": event_cursor,
            "retained_after_cursor": retained_after_cursor,
            "earliest_available_cursor": earliest_available_cursor,
            "delivered_cursor": event_cursor,
            "acknowledged_cursor": event_cursor,
            "created_at": now.isoformat(),
            "expires_at": (now + self._session_ttl).isoformat(),
        }
        _reject_unsafe_projection(payload)
        receipt = _read_receipt(
            operation_id=operation_id, operation="start_session", session=payload,
            result="started", context_bytes=0,
            delivered_cursor=event_cursor, acknowledged_cursor=event_cursor,
        )
        try:
            committed = self._store.create_external_agent_session_with_receipt(
                session_id, payload, operation_id=operation_id, request=operation_request,
                receipt=receipt,
            )
        except TurnStateConflict as error:
            raise ExternalAgentContextConflict("external agent operation identity drifted") from error
        committed_session = committed.get("session_id")
        if not isinstance(committed_session, str):
            raise ExternalAgentContextError("external agent start receipt is invalid")
        return _initial_context_map(self._require_session(committed_session, purpose=purpose))

    def resolve_context(
        self,
        *,
        operation_id: str,
        session_id: str,
        context_refs: Sequence[str],
        expected_context_manifest_revision: str,
        purpose: str,
    ) -> Mapping[str, object]:
        session = self._require_session(session_id, purpose=purpose)
        request = _operation_request(
            operation_id, "resolve_context", purpose=purpose,
            context_manifest_revision=expected_context_manifest_revision,
            context_refs=context_refs,
        )
        replay = self._read_operation_receipt(
            operation_id, session_id=session_id, operation="resolve_context", request=request,
        )
        if session.get("context_manifest_revision") != expected_context_manifest_revision:
            raise ExternalAgentContextConflict("context manifest revision drifted")
        if not context_refs or len(context_refs) != len(set(context_refs)):
            raise ExternalAgentContextError("context refs are invalid")
        allowed_values = session.get("context_refs")
        if not isinstance(allowed_values, list):
            raise ExternalAgentContextError("external agent session is invalid")
        allowed = {
            str(item.get("context_ref")): item
            for item in allowed_values if isinstance(item, Mapping)
        }
        if any(ref not in allowed for ref in context_refs):
            raise ExternalAgentContextError("external agent context target was not found")
        slices = []
        total = 0
        for ref in context_refs:
            value = self._store.get(ref)
            declared = allowed[ref].get("maximum_bytes")
            if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
                raise ExternalAgentContextError("external agent context descriptor is invalid")
            content, size = _project_context_content(
                kind=str(allowed[ref].get("kind", "")),
                value=value,
                revision=allowed[ref].get("revision"),
                declared_bytes=declared,
                project_id=str(session["project_id"]),
                source_ref=allowed[ref].get("source_ref"),
                provenance_refs=allowed[ref].get("provenance_refs"),
            )
            _reject_unsafe_projection(content)
            total += size
            slices.append({
                "context_ref": ref,
                "kind": allowed[ref].get("kind"),
                "revision": allowed[ref].get("revision"),
                "content_bytes": size,
                "content": content,
            })
        resolved = session.get("resolved_bytes")
        maximum = session.get("maximum_context_bytes")
        if not isinstance(resolved, int) or not isinstance(maximum, int):
            raise ExternalAgentContextError("external agent session budget is invalid")
        self._assert_session_authority(session)
        if replay is not None:
            updated = _receipt_integer(replay, "resolved_bytes")
        else:
            receipt = _read_receipt(
                operation_id=operation_id, operation="resolve_context", session=session,
                result="resolved", context_bytes=total, resolved_bytes=resolved + total,
            )
            try:
                committed = self._store.reserve_external_agent_context_bytes_with_receipt(
                    session_id, operation_id=operation_id, request=request,
                    expected_resolved_bytes=resolved, additional_bytes=total,
                    maximum_bytes=maximum, receipt=receipt,
                )
            except TurnStateConflict as error:
                raise ExternalAgentContextConflict("external agent context budget revision drifted") from error
            updated = _receipt_integer(committed, "resolved_bytes")
        self._assert_session_authority(session)
        result = {
            "schema_version": "1.0.0",
            "session_id": session_id,
            "context_manifest_revision": expected_context_manifest_revision,
            "slices": slices,
            "resolved_bytes": updated,
            "remaining_bytes": maximum - updated,
        }
        _reject_unsafe_projection(result)
        return result

    def get_changes(
        self, *, operation_id: str, session_id: str, after_cursor: int,
        purpose: str, limit: int = 128,
    ) -> Mapping[str, object]:
        session = self._require_session(session_id, purpose=purpose)
        request = _operation_request(
            operation_id, "get_changes", purpose=purpose, after_cursor=after_cursor, limit=limit,
        )
        replay = self._read_operation_receipt(
            operation_id, session_id=session_id, operation="get_changes", request=request,
        )
        start = session.get("event_cursor")
        if (
            not isinstance(after_cursor, int) or isinstance(after_cursor, bool)
            or not isinstance(start, int) or after_cursor < start
        ):
            raise ExternalAgentContextError("project event cursor predates Bridge session")
        project_id = str(session["project_id"])
        bounds = self._store.project_event_bounds(project_id)
        retained_after_cursor = _cursor_bound(bounds, "retained_after_cursor")
        earliest_available_cursor = _cursor_bound(bounds, "earliest_available_cursor")
        head_cursor = _cursor_bound(bounds, "head_cursor")
        if after_cursor < retained_after_cursor:
            raise ExternalAgentCursorGap(
                project_id=project_id,
                after_cursor=after_cursor,
                retained_after_cursor=retained_after_cursor,
                earliest_available_cursor=earliest_available_cursor,
                head_cursor=head_cursor,
            )
        if replay is None:
            delivered = session.get("delivered_cursor")
            if (
                not isinstance(delivered, int) or isinstance(delivered, bool)
                or after_cursor > delivered
            ):
                raise ExternalAgentContextError("project event cursor exceeds delivered Bridge cursor")
            changes = self._store.project_events_after(project_id, after_cursor, limit=limit)
            head_cursor = self._store.project_event_cursor(project_id)
            next_cursor = int(changes[-1]["cursor"]) if changes else after_cursor
            receipt = _read_receipt(
                operation_id=operation_id, operation="get_changes", session=session,
                result="delivered", after_cursor=after_cursor, next_cursor=next_cursor,
                head_cursor=head_cursor, delivered_cursor=max(delivered, next_cursor),
                change_count=len(changes),
            )
            try:
                committed = self._store.record_external_agent_delivery_with_receipt(
                    session_id, operation_id=operation_id, request=request,
                    expected_delivered_cursor=delivered,
                    delivered_cursor=max(delivered, next_cursor), receipt=receipt,
                )
            except TurnStateConflict as error:
                raise ExternalAgentContextConflict("external agent delivery cursor drifted") from error
            next_cursor = _receipt_integer(committed, "next_cursor")
            head_cursor = _receipt_integer(committed, "head_cursor")
        else:
            next_cursor = _receipt_integer(replay, "next_cursor")
            head_cursor = _receipt_integer(replay, "head_cursor")
            changes = self._store.project_events_after(
                project_id, after_cursor, limit=limit, until_cursor=next_cursor,
            )
        result = {
            "schema_version": "1.0.0",
            "session_id": session_id,
            "after_cursor": after_cursor,
            "next_cursor": next_cursor,
            "head_cursor": head_cursor,
            "retained_after_cursor": retained_after_cursor,
            "earliest_available_cursor": earliest_available_cursor,
            "has_more": next_cursor < head_cursor,
            "changes": [dict(item) for item in changes],
        }
        _reject_unsafe_projection(result)
        return result

    def acknowledge_changes(
        self, *, operation_id: str, session_id: str, acknowledged_cursor: int,
        purpose: str,
    ) -> Mapping[str, object]:
        session = self._require_session(session_id, purpose=purpose)
        if (
            not isinstance(acknowledged_cursor, int)
            or isinstance(acknowledged_cursor, bool)
            or acknowledged_cursor < 0
        ):
            raise ExternalAgentContextError("external agent acknowledgement cursor is invalid")
        request = _operation_request(
            operation_id, "acknowledge_changes", purpose=purpose,
            acknowledged_cursor=acknowledged_cursor,
        )
        replay = self._read_operation_receipt(
            operation_id, session_id=session_id, operation="acknowledge_changes", request=request,
        )
        if replay is None:
            receipt = _read_receipt(
                operation_id=operation_id, operation="acknowledge_changes", session=session,
                result="acknowledged", acknowledged_cursor=acknowledged_cursor,
            )
            try:
                replay = self._store.acknowledge_external_agent_delivery_with_receipt(
                    session_id, operation_id=operation_id, request=request,
                    acknowledged_cursor=acknowledged_cursor, receipt=receipt,
                )
            except TurnStateConflict as error:
                raise ExternalAgentContextConflict("external agent acknowledgement cursor drifted") from error
        self._assert_session_authority(session)
        result = {
            "schema_version": "1.0.0", "session_id": session_id,
            "acknowledged_cursor": _receipt_integer(replay, "acknowledged_cursor"),
            "delivered_cursor": _receipt_integer(replay, "delivered_cursor"),
        }
        _reject_unsafe_projection(result)
        return result

    def submit_memory_proposal(
        self, *, operation_id: str, session_id: str,
        expected_context_manifest_revision: str, purpose: str,
        proposal: Mapping[str, object], admission: ExternalAgentAdmissionSnapshot,
    ) -> Mapping[str, object]:
        session = self._require_session(session_id, purpose=purpose)
        if self._proposal_sink is None:
            raise ExternalAgentContextError("external agent proposal sink is unavailable")
        if session.get("context_manifest_revision") != expected_context_manifest_revision:
            raise ExternalAgentContextConflict("context manifest revision drifted")
        project_id = str(session["project_id"])
        if (
            admission.project_id != project_id
            or admission.adapter_id != session.get("adapter_id")
            or admission.policy_revision != session.get("boundary_profile_revision")
        ):
            raise ExternalAgentContextConflict("external agent proposal admission drifted")
        normalized = _normalize_memory_proposal(
            proposal, session=session, operation_id=operation_id,
            resolved_refs=self._store.external_agent_resolved_context_refs(session_id),
        )
        request = _operation_request(
            operation_id, "submit_memory_proposal", purpose=purpose,
            context_manifest_revision=expected_context_manifest_revision,
            admission_id=admission.admission_id,
            admission_policy_revision=admission.policy_revision,
            proposal=normalized,
        )
        now = _aware_utc(self._clock()).isoformat()
        try:
            operation = self._store.reserve_external_agent_proposal_operation(
                session_id, operation_id=operation_id, project_id=project_id,
                request=request, created_at=now,
            )
        except TurnStateConflict as error:
            raise ExternalAgentContextConflict("external agent operation identity drifted") from error
        if operation.get("status") == "finalized":
            return _proposal_result(operation)
        if operation.get("replayed"):
            raise ExternalAgentContextConflict("external agent proposal operation is in progress")
        self._assert_session_authority(session)
        imported = self._proposal_sink(normalized, project_id)
        if imported.get("status") != "pending_review" or imported.get("project_id") != project_id:
            raise ExternalAgentContextError("external agent proposal sink returned invalid result")
        proposal_id = imported.get("proposal_id")
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ExternalAgentContextError("external agent proposal sink returned invalid result")
        proposal_ref = f"crp://proposals/{project_id}/{proposal_id}"
        self._assert_session_authority(session)
        try:
            finalized = self._store.finalize_external_agent_proposal_operation(
                session_id, operation_id=operation_id, project_id=project_id,
                request=request, proposal_ref=proposal_ref, proposal_revision="pending-review-r1",
                result=imported, occurred_at=now,
            )
        except TurnStateConflict as error:
            raise ExternalAgentContextConflict("external agent proposal operation drifted") from error
        return _proposal_result(finalized)

    def proposal_admission_subject(
        self, *, session_id: str, purpose: str,
    ) -> Mapping[str, str]:
        session = self._require_session(session_id, purpose=purpose)
        return {
            "turn_id": str(session["turn_id"]),
            "project_id": str(session["project_id"]),
            "adapter_id": str(session["adapter_id"]),
        }

    def recover_prepared_memory_proposals(self, *, limit: int = 32) -> Mapping[str, int]:
        completed = 0
        failed = 0
        for operation in self._store.prepared_external_agent_proposal_operations(limit=limit):
            try:
                session_id = str(operation["session_id"])
                project_id = str(operation["project_id"])
                operation_id = str(operation["operation_id"])
                request = operation.get("request")
                session = self._store.get_external_agent_session(session_id)
                if not isinstance(request, Mapping) or session is None:
                    raise ExternalAgentContextError("prepared proposal authority is unavailable")
                _validate_session_projection(session)
                if session.get("project_id") != project_id:
                    raise ExternalAgentContextConflict("prepared proposal project drifted")
                adapter = self._adapters.get(str(session["adapter_id"]))
                if adapter is None or adapter.revision != session.get("adapter_revision"):
                    raise ExternalAgentContextConflict("prepared proposal adapter drifted")
                self._assert_session_authority(session)
                if (
                    request.get("operation") != "submit_memory_proposal"
                    or request.get("context_manifest_revision") != session.get("context_manifest_revision")
                    or request.get("admission_policy_revision") != session.get("boundary_profile_revision")
                ):
                    raise ExternalAgentContextConflict("prepared proposal revision drifted")
                proposal = request.get("proposal")
                if not isinstance(proposal, Mapping) or self._proposal_sink is None:
                    raise ExternalAgentContextError("prepared proposal payload is unavailable")
                imported = self._proposal_sink(proposal, project_id)
                proposal_id = imported.get("proposal_id")
                if imported.get("status") != "pending_review" or not isinstance(proposal_id, str):
                    raise ExternalAgentContextError("prepared proposal sink result is invalid")
                occurred_at = str(operation["created_at"])
                self._store.finalize_external_agent_proposal_operation(
                    session_id, operation_id=operation_id, project_id=project_id,
                    request=request, proposal_ref=f"crp://proposals/{project_id}/{proposal_id}",
                    proposal_revision="pending-review-r1", result=imported,
                    occurred_at=occurred_at,
                )
                completed += 1
            except (ExternalAgentContextError, KeyError, TypeError, ValueError, OSError, RuntimeError):
                try:
                    self._store.defer_external_agent_proposal_operation(
                        str(operation.get("operation_id", "")),
                        updated_at=_aware_utc(self._clock()).isoformat(),
                    )
                except (KeyError, TypeError, ValueError, OSError, RuntimeError):
                    pass
                failed += 1
        return {"completed": completed, "failed": failed}

    def _require_session(self, session_id: str, *, purpose: str) -> Mapping[str, object]:
        session = self._store.get_external_agent_session(session_id)
        if session is None or session.get("purpose") != purpose:
            raise ExternalAgentContextError("external agent session was not found")
        _validate_session_projection(session)
        adapter = self._adapters.get(str(session["adapter_id"]))
        if adapter is None or (
            adapter.revision != session["adapter_revision"]
            or adapter.template_revision != session["template_revision"]
            or purpose not in adapter.supported_purposes
        ):
            raise ExternalAgentContextConflict("external agent adapter revision drifted")
        try:
            expires_at = datetime.fromisoformat(str(session["expires_at"]))
        except (KeyError, ValueError) as error:
            raise ExternalAgentContextError("external agent session expiry is invalid") from error
        if _aware_utc(self._clock()) >= _aware_utc(expires_at):
            raise ExternalAgentContextError("external agent session expired")
        self._assert_session_authority(session)
        return session

    def _assert_session_authority(self, session: Mapping[str, object]) -> None:
        snapshot = self._authority(str(session["project_id"]))
        expected = (
            session.get("project_profile_id"), session.get("project_profile_revision"),
            session.get("boundary_profile_id"), session.get("boundary_profile_revision"),
        )
        current = (
            snapshot.project_id, snapshot.project_profile_id, snapshot.project_profile_revision,
            snapshot.boundary_profile_id, snapshot.boundary_profile_revision,
        )
        expected = (session.get("project_id"), *expected)
        if expected != current:
            raise ExternalAgentContextConflict("external agent authority revision drifted")

    def _read_operation_receipt(
        self, operation_id: str, *, session_id: str, operation: str,
        request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        try:
            return self._store.get_external_agent_read_receipt(
                operation_id, session_id=session_id, operation=operation, request=request,
            )
        except TurnStateConflict as error:
            raise ExternalAgentContextConflict("external agent operation identity drifted") from error


def _latest_context_event(events: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    matches = [event for event in events if event.get("type") == "context.resolved"]
    if not matches:
        raise ExternalAgentContextError("Turn has no resolved context")
    return matches[-1]


def _event_data(event: Mapping[str, object]) -> Mapping[str, object]:
    value = event.get("data")
    if not isinstance(value, Mapping):
        raise ExternalAgentContextError("Turn context event is invalid")
    return value


def _request_project(request: Mapping[str, object]) -> str | None:
    scope = request.get("scope")
    value = scope.get("project_id") if isinstance(scope, Mapping) else None
    return value if isinstance(value, str) else None


def _assert_authority(manifest: object, snapshot: ExternalAgentAuthoritySnapshot) -> None:
    values = (
        getattr(manifest, "project_id"), getattr(manifest, "project_profile_id"),
        getattr(manifest, "project_profile_revision"), getattr(manifest, "boundary_profile_id"),
        getattr(manifest, "boundary_profile_revision"),
    )
    expected = (
        snapshot.project_id, snapshot.project_profile_id, snapshot.project_profile_revision,
        snapshot.boundary_profile_id, snapshot.boundary_profile_revision,
    )
    if values != expected:
        raise ExternalAgentContextConflict("context authority revision drifted")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ExternalAgentContextError("external agent clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_memory_proposal(
    proposal: Mapping[str, object], *, session: Mapping[str, object], operation_id: str,
    resolved_refs: Sequence[str],
) -> dict[str, object]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,99}", operation_id):
        raise ExternalAgentContextError("external agent proposal operation identity is invalid")
    fields = {
        "proposal_type", "summary", "source_refs", "evidence_refs",
        "suggested_changes", "requires_user_review",
    }
    if set(proposal) != fields or proposal.get("proposal_type") != "memory_candidate_proposal":
        raise ExternalAgentContextError("external agent memory proposal shape is invalid")
    if proposal.get("requires_user_review") is not True:
        raise ExternalAgentContextError("external agent memory proposal requires user review")
    summary = proposal.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise ExternalAgentContextError("external agent memory proposal summary is invalid")
    allowed_values = session.get("context_refs")
    mapped = {
        str(item.get("context_ref")) for item in allowed_values
        if isinstance(item, Mapping) and isinstance(item.get("context_ref"), str)
    } if isinstance(allowed_values, list) else set()
    allowed = mapped.intersection(resolved_refs)
    refs: dict[str, list[dict[str, str]]] = {}
    for field in ("source_refs", "evidence_refs"):
        values = proposal.get(field)
        if (
            not isinstance(values, list) or not values
            or any(not isinstance(item, str) or item not in allowed for item in values)
        ):
            raise ExternalAgentContextError("external agent proposal ref is not session-authorized")
        refs[field] = [{"locator": item} for item in values]
    suggested = proposal.get("suggested_changes")
    if not isinstance(suggested, Mapping):
        raise ExternalAgentContextError("external agent memory proposal changes are invalid")
    normalized = {
        "proposal_id": "bridge-" + operation_id,
        "proposal_type": "memory_candidate_proposal",
        "summary": summary.strip(),
        "source_refs": refs["source_refs"],
        "evidence_refs": refs["evidence_refs"],
        "suggested_changes": dict(suggested),
        "requires_user_review": True,
    }
    _reject_unsafe_projection(normalized)
    return normalized


def _proposal_result(operation: Mapping[str, object]) -> dict[str, object]:
    result = operation.get("result")
    if operation.get("status") != "finalized" or not isinstance(result, Mapping):
        raise ExternalAgentContextError("external agent proposal operation is incomplete")
    public = dict(result)
    public["operation_id"] = operation.get("operation_id")
    public["change_cursor"] = operation.get("change_cursor")
    public["replayed"] = bool(operation.get("replayed"))
    _reject_unsafe_projection(public)
    return public


def _validate_session_projection(value: Mapping[str, object]) -> None:
    fields = {
        "schema_version", "session_id", "adapter_id", "adapter_revision",
        "template_revision", "turn_id", "project_id", "project_profile_id",
        "project_profile_revision", "boundary_profile_id", "boundary_profile_revision",
        "context_manifest_ref", "context_manifest_revision", "purpose",
        "admission_id", "admission_policy_revision", "admission_reason_codes",
        "maximum_context_bytes", "context_refs", "event_cursor", "created_at",
        "expires_at", "resolved_bytes", "delivered_cursor", "acknowledged_cursor",
        "retained_after_cursor", "earliest_available_cursor",
    }
    if set(value) != fields or value.get("schema_version") != "1.0.0":
        raise ExternalAgentContextError("external agent session projection is invalid")
    for field in (
        "session_id", "adapter_id", "template_revision", "turn_id", "project_id",
        "project_profile_id", "boundary_profile_id", "context_manifest_ref",
        "context_manifest_revision", "purpose", "created_at", "expires_at",
        "admission_id",
    ):
        if not isinstance(value.get(field), str) or not str(value[field]).strip():
            raise ExternalAgentContextError("external agent session projection is invalid")
    for field in (
        "adapter_revision", "project_profile_revision", "boundary_profile_revision",
        "maximum_context_bytes", "event_cursor", "resolved_bytes", "delivered_cursor",
        "acknowledged_cursor", "admission_policy_revision", "retained_after_cursor",
        "earliest_available_cursor",
    ):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ExternalAgentContextError("external agent session projection is invalid")
    if not isinstance(value.get("context_refs"), list):
        raise ExternalAgentContextError("external agent session projection is invalid")
    reasons = value.get("admission_reason_codes")
    if not isinstance(reasons, list) or not reasons or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ):
        raise ExternalAgentContextError("external agent session projection is invalid")
    if value["acknowledged_cursor"] > value["delivered_cursor"]:
        raise ExternalAgentContextError("external agent session projection is invalid")
    if value["earliest_available_cursor"] != value["retained_after_cursor"] + 1:
        raise ExternalAgentContextError("external agent session projection is invalid")


def _operation_request(operation_id: object, operation: str, **values: object) -> dict[str, object]:
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ExternalAgentContextError("external agent operation identity is invalid")
    return {"operation_id": operation_id, "operation": operation, **values}


def _initial_context_map(session: Mapping[str, object]) -> dict[str, object]:
    result = dict(session)
    initial_cursor = result.get("event_cursor")
    if not isinstance(initial_cursor, int) or isinstance(initial_cursor, bool):
        raise ExternalAgentContextError("external agent session projection is invalid")
    result["resolved_bytes"] = 0
    result["delivered_cursor"] = initial_cursor
    result["acknowledged_cursor"] = initial_cursor
    _reject_unsafe_projection(result)
    return result


def _receipt_integer(receipt: Mapping[str, object], name: str) -> int:
    value = receipt.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExternalAgentContextError("external agent read receipt is invalid")
    return value


def _cursor_bound(bounds: Mapping[str, int], name: str) -> int:
    value = bounds.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExternalAgentContextError("project event retention bounds are invalid")
    return value


def _read_receipt(
    *, operation_id: str, operation: str, session: Mapping[str, object], result: str,
    **values: object,
) -> dict[str, object]:
    receipt = {
        "schema_version": "1.0.0", "operation_id": operation_id, "operation": operation,
        "session_id": session["session_id"], "adapter_id": session["adapter_id"],
        "adapter_revision": session["adapter_revision"], "project_id": session["project_id"],
        "project_profile_revision": session["project_profile_revision"],
        "boundary_profile_revision": session["boundary_profile_revision"], "result": result,
        **values,
    }
    _reject_unsafe_projection(receipt)
    return receipt


def _project_context_content(
    *, kind: str, value: object, revision: object, declared_bytes: int,
    project_id: str, source_ref: object, provenance_refs: object,
) -> tuple[object, int]:
    if kind == "application_skill":
        return _application_skill_context_content(
            value=value, revision=revision, declared_bytes=declared_bytes,
        )
    if kind not in {"project_skill", "memory_r1", "memory_r2", "memory_r3"}:
        raise ExternalAgentContextError("external agent context kind is unsupported")
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "kind", "project_id", "object_id", "revision", "trust_status", "markdown",
    }:
        raise ExternalAgentContextError("published memory context shape is invalid")
    if value.get("schema_version") != "1.0.0" or value.get("kind") != kind:
        raise ExternalAgentContextError("published memory context schema is unsupported")
    if value.get("project_id") != project_id or value.get("revision") != revision:
        raise ExternalAgentContextConflict("published memory context identity drifted")
    object_id = value.get("object_id")
    trust_status = value.get("trust_status")
    if not isinstance(object_id, str) or not object_id.strip() or trust_status not in {
        "trusted", "user_confirmed", "system_generated",
    }:
        raise ExternalAgentContextError("published memory context is invalid")
    markdown = value.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ExternalAgentContextError("published memory context is invalid")
    if not isinstance(source_ref, str) or not _is_project_scoped_ref(source_ref, project_id):
        raise ExternalAgentContextError("published memory source ref is invalid")
    if not isinstance(provenance_refs, list) or any(not isinstance(item, str) for item in provenance_refs):
        raise ExternalAgentContextError("published memory provenance refs are invalid")
    if _project_object_id(source_ref, project_id) != object_id:
        raise ExternalAgentContextConflict("published memory source identity drifted")
    _reject_unsafe_projection(value)
    content_bytes = len(markdown.encode("utf-8"))
    if content_bytes != declared_bytes:
        raise ExternalAgentContextConflict("context payload byte identity drifted")
    return {
        "kind": kind,
        "object_id": object_id,
        "revision": revision,
        "trust_status": trust_status,
        "markdown": markdown,
    }, content_bytes


def _application_skill_context_content(
    *, value: object, revision: object, declared_bytes: int,
) -> tuple[object, int]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "skill_id", "skill_fingerprint", "markdown",
    }:
        raise ExternalAgentContextError("external agent context kind is unsupported")
    if value.get("schema_version") != "1.0.0":
        raise ExternalAgentContextError("application skill context schema is unsupported")
    skill_id = value.get("skill_id")
    fingerprint = value.get("skill_fingerprint")
    markdown = value.get("markdown")
    if not all(isinstance(item, str) and item.strip() for item in (skill_id, fingerprint, markdown)):
        raise ExternalAgentContextError("application skill context is invalid")
    if not isinstance(revision, str) or not revision.strip():
        raise ExternalAgentContextError("application skill binding revision is invalid")
    _reject_unsafe_projection(markdown)
    content_bytes = len(markdown.encode("utf-8"))
    if content_bytes != declared_bytes:
        raise ExternalAgentContextConflict("context payload byte identity drifted")
    return {
        "skill_id": skill_id,
        "skill_fingerprint": fingerprint,
        "binding_revision": revision,
        "markdown": markdown,
    }, content_bytes


def _validate_published_memory_context_entry(entry: object, project_id: str) -> None:
    source_ref = getattr(entry, "source_ref", None)
    if not isinstance(source_ref, str) or not _is_project_scoped_ref(source_ref, project_id):
        raise ExternalAgentContextError("context manifest source ref crossed project scope")
    provenance_refs = getattr(entry, "provenance_refs", ())
    if any(not isinstance(item, str) or not _SAFE_CRP_REF.fullmatch(item) for item in provenance_refs):
        raise ExternalAgentContextError("context manifest provenance ref is invalid")


def _is_project_scoped_ref(ref: object, project_id: str) -> bool:
    if not isinstance(ref, str) or not _SAFE_CRP_REF.fullmatch(ref):
        return False
    prefix = "crp://"
    path = ref[len(prefix):].split("/", 1)
    return len(path) == 2 and path[1].split("/", 1)[0] == project_id


def _project_object_id(ref: object, project_id: str) -> str | None:
    if not _is_project_scoped_ref(ref, project_id):
        return None
    path = str(ref)[len("crp://"):].split("/", 1)[1].split("/")
    return path[-1] if len(path) >= 2 and path[-1] else None


def _reject_unsafe_projection(value: object) -> None:
    def walk(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in _SENSITIVE_KEYS:
                    raise ExternalAgentContextError("external agent projection contains unsafe content")
                walk(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                walk(nested)
            return
        if isinstance(item, str):
            if item.startswith("crp://"):
                if not _SAFE_CRP_REF.fullmatch(item):
                    raise ExternalAgentContextError("external agent projection contains unsafe content")
                return
            if _SECRET_VALUE.search(item) or _ABSOLUTE_PATH.search(item):
                raise ExternalAgentContextError("external agent projection contains unsafe content")

    walk(value)


def validate_external_agent_safe_projection(value: object) -> None:
    """Apply the Bridge path and credential canary policy before persistence."""

    _reject_unsafe_projection(value)
