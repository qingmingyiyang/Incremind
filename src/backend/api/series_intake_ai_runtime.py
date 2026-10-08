"""Canonical, recoverable AI Turn capability for Replay Intake organization.

The legacy API remains responsible for translating its request into the
immutable ``series.intake.organize.snapshot.v1`` Turn input.  This module
never rebuilds that snapshot from the current workspace: the only current
read after a provider result is the fenced compare-and-apply materialization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
from pathlib import Path
import re

from backend.api.ai_execution_control import (
    begin_nested_model_call,
    execution_control_from,
)
from backend.api.turn_model_routing_binding import load_turn_model_routing_binding
from backend.replay.contracts import IntakeItem, local_now_iso
from backend.replay.library import _atomic_json
from backend.replay.prompts import (
    REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION,
    build_intake_organizer_messages,
)
from backend.replay.series_workspace import IntakeRevisionConflictError, SeriesWorkspace
from core.ai_kernel import (
    CapabilityDefinition,
    TurnPayloadStorePort,
    validate_turn_presentation_artifact,
)
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.model_gateway import ModelExecutionControlPort, ModelGatewayPort, ModelRequest


SERIES_INTAKE_ORGANIZE_OUTCOME = "series.intake.organize.commit"
SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY = "series.intake.organize.commit"
SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND = "series.intake.organize.snapshot.v1"
_RECEIPT_KIND = "series-intake-organize-receipt"
_OPERATION_VERSION = "1"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,159}$")
_AUTHORITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")
_SENSITIVE_KEY = re.compile(
    r"^(?:api[_-]?key|authorization|cookie|password|secret|token|base[_-]?url|endpoint|prompt_text|system_prompt|local_path|windows_path)$",
    re.I,
)
_CRP_REF = re.compile(r"^crp://[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")


class SeriesIntakeOrganizeTurnPlanner:
    """A deterministic planner: one approved write capability, then complete."""

    def plan(
        self,
        request: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition],
        payloads: TurnPayloadStorePort,
        execution_control: ModelExecutionControlPort | None = None,
    ) -> Mapping[str, object]:
        completed = _completed(events, SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY)
        if completed is not None:
            data = completed.get("data")
            return {
                "type": "complete",
                "summary": "Series Intake organization completed",
                "payload_ref": data.get("payload_ref") if isinstance(data, Mapping) else None,
                "evidence_refs": list(data.get("evidence_refs") or ()) if isinstance(data, Mapping) else [],
            }
        return {
            "type": "tool",
            "capability_id": SERIES_INTAKE_ORGANIZE_COMMIT_CAPABILITY,
            "arguments": {"snapshot": load_series_intake_organize_snapshot(request)},
        }


class SeriesIntakeOrganizeCommitCapability:
    """Call one frozen structured route and CAS-apply its reviewing draft."""

    def __init__(
        self,
        *,
        workspace: SeriesWorkspace,
        gateway: ModelGatewayPort | None,
        payloads: TurnPayloadStorePort,
        namespace_id: str,
    ) -> None:
        self._workspace = workspace
        self._gateway = gateway
        self._payloads = payloads
        self._namespace_id = _identity(namespace_id, "namespace id")

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn id")
        snapshot = _snapshot(_arguments(request).get("snapshot"))
        _validate_scope(request, snapshot)
        operation_id = _required(request.get("operation_id"), "operation id")
        routing = load_turn_model_routing_binding(
            self._payloads, request, required_capability="structured",
        )
        operation_path = self._operation_path(snapshot, operation_id)
        operation = _read_operation(operation_path)
        if operation is not None:
            _validate_operation(
                operation, operation_id, snapshot, turn_id,
                route_snapshot_ref=routing.snapshot_ref,
                route_snapshot_revision=routing.snapshot_revision,
            )
            item, replayed = self._materialize_prepared(snapshot, operation, operation_path)
            return self._result(
                turn_id, snapshot, operation_id, item, replayed,
                tuple(operation.get("model_evidence_refs") or ()),
            )

        if self._gateway is None:
            raise ValueError("Series Intake organizer model gateway is unavailable")
        privacy = request.get("privacy")
        if not isinstance(privacy, Mapping) or privacy.get("allow_remote") is not True:
            raise ValueError("Series Intake organizer requires remote consent")
        frozen = IntakeItem.model_validate(snapshot["intake"])
        messages = build_intake_organizer_messages(
            source_text=_source_text(frozen),
            existing_title=frozen.title,
            existing_tags=frozen.tags,
        )
        nested = begin_nested_model_call(
            request,
            invocation_key=operation_id,
            purpose="primary",
        )
        try:
            model = self._gateway.invoke(
                ModelRequest(
                    capability="structured",
                    input=messages[1]["content"],
                    parameters={
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": messages,
                        **routing.parameters(),
                    },
                    privacy_scope="remote_allowed",
                    execution_control=execution_control_from(request),
                    metadata_sink=nested,
                )
            )
            generated = _model_object(model.output)
            next_item = _organized_item(frozen, generated)
        except Exception as error:
            nested.finalize(error_code="ai.nested_model_failed")
            raise ToolProviderFailure(
                "series_intake.model_failed", effect_certainty="confirmed_none",
            ) from error
        evidence_refs = nested.finalize(error_code=None)

        prepared = _prepared_operation(
            operation_id=operation_id,
            turn_id=turn_id,
            snapshot=snapshot,
            item=next_item,
            model_evidence_refs=evidence_refs,
            route_snapshot_ref=routing.snapshot_ref,
            route_snapshot_revision=routing.snapshot_revision,
        )
        _atomic_json(operation_path, prepared)
        item, replayed = self._materialize_prepared(snapshot, prepared, operation_path)
        return self._result(turn_id, snapshot, operation_id, item, replayed, evidence_refs)

    def _materialize_prepared(
        self,
        snapshot: Mapping[str, object],
        operation: Mapping[str, object],
        operation_path: Path,
    ) -> tuple[IntakeItem, bool]:
        frozen = IntakeItem.model_validate(snapshot["intake"])
        next_item = IntakeItem.model_validate({
            **frozen.model_dump(mode="json"),
            **dict(operation["changes"]),
        })
        current = self._workspace.get_intake(str(snapshot["series_id"]), str(snapshot["intake_id"]))
        expected_revision = str(snapshot["expected_revision"])
        if current.revision == next_item.revision:
            if operation.get("state") != "finalized":
                _atomic_json(operation_path, {**operation, "state": "finalized", "finalized_at": local_now_iso()})
            return current, True
        if current.revision != expected_revision:
            raise ToolProviderFailure(
                "series_intake.stale_revision", effect_certainty="confirmed_none",
            ) from IntakeRevisionConflictError("待整理项已被更新，请刷新后重试。")
        try:
            saved = self._workspace._save_intake(  # noqa: SLF001 -- legacy authority owns its CAS.
                next_item,
                previous_status=current.status,
                expected_revision=expected_revision,
            )
        except IntakeRevisionConflictError as error:
            raise ToolProviderFailure(
                "series_intake.stale_revision", effect_certainty="confirmed_none",
            ) from error
        try:
            _atomic_json(operation_path, {**operation, "state": "finalized", "finalized_at": local_now_iso()})
        except Exception as error:
            raise ToolProviderFailure(
                "series_intake.finalize_unknown", effect_certainty="unknown",
            ) from error
        return saved, False

    def _operation_path(self, snapshot: Mapping[str, object], operation_id: str) -> Path:
        return self._workspace.series_path(str(snapshot["series_id"])) / "intake" / "operations" / f"{operation_id}.json"

    def _result(
        self,
        turn_id: str,
        snapshot: Mapping[str, object],
        operation_id: str,
        item: IntakeItem,
        replayed: bool,
        model_evidence_refs: Sequence[str],
    ) -> Mapping[str, object]:
        content = {
            "status": "reviewing",
            "series_id": snapshot["series_id"],
            "intake_id": snapshot["intake_id"],
            "intake_revision": item.revision,
            "operation_id": operation_id,
            "replayed": replayed,
        }
        artifact = validate_turn_presentation_artifact({
            "schema_version": "1.0.0", "kind": SERIES_INTAKE_ORGANIZE_OUTCOME,
            "content": content,
        })
        receipt_ref = self._payloads.put(turn_id, _RECEIPT_KIND, artifact)
        evidence = [
            f"crp://{self._namespace_id}/series/{snapshot['series_id']}/intake/{snapshot['intake_id']}",
            *[ref for ref in model_evidence_refs if isinstance(ref, str)],
        ]
        return {
            "summary": "Series Intake organization created a reviewing draft",
            "receipt_ref": receipt_ref,
            "payload_ref": None,
            "evidence_refs": evidence,
            "result": artifact,
        }


def load_series_intake_organize_snapshot(request: Mapping[str, object]) -> dict[str, object]:
    """Decode the immutable request input, without consulting a workspace."""
    input_value = request.get("input")
    if not isinstance(input_value, Mapping):
        raise ValueError("Series Intake Turn input is required")
    text = input_value.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Series Intake Turn snapshot is required")
    try:
        return _snapshot(json.loads(text))
    except json.JSONDecodeError as error:
        raise ValueError("Series Intake Turn snapshot is invalid JSON") from error


def _snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Series Intake snapshot is invalid")
    snapshot = deepcopy(dict(value))
    expected = {
        "kind", "series_id", "project_id", "authority", "intake_id", "intake",
        "expected_revision", "prompt_version",
    }
    if set(snapshot) != expected or snapshot.get("kind") != SERIES_INTAKE_ORGANIZE_SNAPSHOT_KIND:
        raise ValueError("Series Intake snapshot shape is invalid")
    for key in ("series_id", "project_id", "intake_id", "expected_revision"):
        snapshot[key] = _identity(snapshot.get(key), key)
    if snapshot["prompt_version"] != REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION:
        raise ValueError("Series Intake prompt version is invalid")
    authority = snapshot.get("authority")
    if not isinstance(authority, Mapping) or set(authority) != {
        "kind", "object_id", "payload_revision", "storage_revision", "authority_identity", "authority_ref",
    } or authority.get("kind") != "project_series_scope_v1":
        raise ValueError("Series Intake scope authority is invalid")
    _identity(authority.get("object_id"), "authority object_id")
    if not isinstance(authority.get("authority_identity"), str) or not _AUTHORITY_ID.fullmatch(authority["authority_identity"]):
        raise ValueError("Series Intake authority identity is invalid")
    if not isinstance(authority.get("authority_ref"), str) or not _CRP_REF.fullmatch(authority["authority_ref"]):
        raise ValueError("Series Intake authority ref is invalid")
    for key in ("payload_revision", "storage_revision"):
        if not isinstance(authority.get(key), int) or isinstance(authority.get(key), bool) or authority[key] < 1:
            raise ValueError("Series Intake authority revision is invalid")
    intake = IntakeItem.model_validate(snapshot.get("intake"))
    if intake.series_id != snapshot["series_id"] or intake.intake_id != snapshot["intake_id"]:
        raise ValueError("Series Intake snapshot identity drifted")
    # ``organization_snapshot`` may attach extracted asset text for the model.
    # That model-visible projection has a different business fingerprint from
    # the persisted Intake baseline, so expected_revision remains a separate
    # compare-and-apply fact rather than being inferred from this payload.
    return snapshot


def _validate_scope(request: Mapping[str, object], snapshot: Mapping[str, object]) -> None:
    scope = request.get("scope")
    if not isinstance(scope, Mapping) or scope.get("kind") != "series":
        raise ValueError("Series Intake Turn scope is invalid")
    if scope.get("project_id") != snapshot["project_id"] or scope.get("series_id") != snapshot["series_id"]:
        raise ValueError("Series Intake Turn scope drifted")
    if scope.get("authority") != snapshot["authority"]:
        raise ValueError("Series Intake Turn authority drifted")


def _prepared_operation(
    *, operation_id: str, turn_id: str, snapshot: Mapping[str, object], item: IntakeItem,
    model_evidence_refs: Sequence[str], route_snapshot_ref: str, route_snapshot_revision: str,
) -> dict[str, object]:
    frozen = IntakeItem.model_validate(snapshot["intake"])
    changed_fields = (
        "title", "structured_text", "summary", "tags", "suggested_actions",
        "suggested_report_type", "status", "warnings", "updated_at",
    )
    item_payload = item.model_dump(mode="json")
    frozen_payload = frozen.model_dump(mode="json")
    result = {
        "version": _OPERATION_VERSION,
        "operation_id": operation_id,
        "turn_id": turn_id,
        "state": "prepared",
        "series_id": snapshot["series_id"],
        "intake_id": snapshot["intake_id"],
        "expected_revision": snapshot["expected_revision"],
        "new_revision": item.revision,
        "changes": {
            key: item_payload[key] for key in changed_fields
            if item_payload[key] != frozen_payload[key]
        },
        "model_evidence_refs": list(model_evidence_refs),
        "route_snapshot_ref": route_snapshot_ref,
        "route_snapshot_revision": route_snapshot_revision,
        "authority": dict(snapshot["authority"]),
        "prompt_version": snapshot["prompt_version"],
        "prepared_at": local_now_iso(),
    }
    _reject_sensitive_record(result)
    return result


def _validate_operation(
    operation: Mapping[str, object], operation_id: str, snapshot: Mapping[str, object], turn_id: str,
    *, route_snapshot_ref: str, route_snapshot_revision: str,
) -> None:
    expected = {
        "version", "operation_id", "turn_id", "state", "series_id", "intake_id",
        "expected_revision", "new_revision", "changes", "model_evidence_refs",
        "route_snapshot_ref", "route_snapshot_revision", "authority", "prompt_version",
        "prepared_at",
    }
    actual = set(operation) - {"finalized_at"}
    if actual != expected or operation.get("version") != _OPERATION_VERSION or operation.get("operation_id") != operation_id or operation.get("turn_id") != turn_id:
        raise ValueError("Series Intake operation is invalid")
    if operation.get("state") not in {"prepared", "finalized"} or any(operation.get(key) != snapshot[key] for key in ("series_id", "intake_id", "expected_revision")):
        raise ValueError("Series Intake operation drifted")
    if operation.get("authority") != snapshot["authority"] or operation.get("prompt_version") != snapshot["prompt_version"]:
        raise ValueError("Series Intake operation authority drifted")
    if (
        operation.get("route_snapshot_ref") != route_snapshot_ref
        or operation.get("route_snapshot_revision") != route_snapshot_revision
    ):
        raise ValueError("Series Intake operation model route drifted")
    refs = operation.get("model_evidence_refs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not _CRP_REF.fullmatch(ref) for ref in refs):
        raise ValueError("Series Intake operation evidence is invalid")
    changes = operation.get("changes")
    if not isinstance(changes, Mapping):
        raise ValueError("Series Intake operation changes are invalid")
    frozen = IntakeItem.model_validate(snapshot["intake"])
    item = IntakeItem.model_validate({**frozen.model_dump(mode="json"), **dict(changes)})
    if item.revision != operation.get("new_revision") or item.series_id != snapshot["series_id"] or item.intake_id != snapshot["intake_id"]:
        raise ValueError("Series Intake operation payload drifted")


def _read_operation(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Series Intake operation cannot be read") from error
    if not isinstance(value, Mapping):
        raise ValueError("Series Intake operation is invalid")
    return dict(value)


def _organized_item(current: IntakeItem, value: Mapping[str, object]) -> IntakeItem:
    report_type = value.get("suggested_report_type")
    if report_type not in {"daily", "weekly", "monthly", "yearly", "none"}:
        report_type = "none"
    source = _source_text(current)
    candidate = current.model_copy(update={
        "title": _text(value.get("title"), fallback=current.title or source[:40], limit=120),
        "structured_text": _text(value.get("structured_text"), fallback="", limit=60_000),
        "summary": _text(value.get("summary"), fallback="", limit=12_000),
        "tags": _strings(value.get("tags"), 20) or current.tags,
        "suggested_actions": _strings(value.get("suggested_actions"), 20),
        "suggested_report_type": report_type,
        "status": "reviewing",
        "warnings": [warning for warning in current.warnings if warning != "AI 整理失败，原始内容已保留。"],
        "updated_at": local_now_iso(),
    })
    # ``model_copy`` deliberately skips Pydantic validation, while Intake's
    # business revision is derived by its model validator. Rehydrate before
    # persisting the prepared operation so its advertised revision is exact.
    return IntakeItem.model_validate(candidate.model_dump(mode="json"))


def _model_object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise ValueError("Series Intake organizer output must be JSON")
    try:
        parsed = json.loads(_strip_fence(value))
    except json.JSONDecodeError as error:
        raise ValueError("Series Intake organizer output must be JSON") from error
    if not isinstance(parsed, Mapping):
        raise ValueError("Series Intake organizer output must be an object")
    return parsed


def _source_text(item: IntakeItem) -> str:
    raw_text, asset_text = item.raw_text.strip(), item.asset_text.strip()
    if raw_text and asset_text:
        return f"## 用户原文\n\n{raw_text}\n\n{asset_text}"
    return raw_text or asset_text or item.structured_text.strip()


def _strings(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip()[:400] for item in value if isinstance(item, str) and item.strip()][:limit]


def _text(value: object, *, fallback: str, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else fallback.strip()[:limit]


def _strip_fence(value: str) -> str:
    value = value.strip()
    if value.startswith("```") and value.endswith("```"):
        return value.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
    return value


def _completed(events: Sequence[Mapping[str, object]], capability_id: str) -> Mapping[str, object] | None:
    return next((event for event in reversed(events) if event.get("type") == "tool.completed" and isinstance(event.get("data"), Mapping) and event["data"].get("capability_id") == capability_id), None)


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    arguments = request.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("capability arguments are required")
    return arguments


def _required(value: object, label: str) -> str:
    return _identity(value, label)


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError(f"Series Intake {label} is invalid")
    return value


def _reject_sensitive_record(value: object) -> None:
    """Operations may retain user-owned Intake fields, but never execution secrets."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError("Series Intake operation contains sensitive execution data")
            _reject_sensitive_record(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_record(nested)
