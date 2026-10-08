from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from .package_catalog import (
    ApplicationSkillCatalog,
    ApplicationSkillPackage,
    ApplicationSkillSource,
)
from .resolver import ApplicationSkillResolver


class ApplicationSkillConsumerRuntimeError(ValueError):
    """Raised when a consumer cannot obtain an auditable Skill context."""


class ApplicationSkillTraceStorePort(Protocol):
    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class ObjectStoreApplicationSkillTraceRepository:
    object_store: ApplicationSkillTraceStorePort
    collection: str = "application_skill_resolution_traces"

    def save_trace(self, trace: Mapping[str, object]) -> Mapping[str, object]:
        payload = _validate_trace(trace)
        resolution_id = str(payload["resolution_id"])
        existing = self.object_store.read(self.collection, resolution_id)
        if existing is not None:
            if not _same_trace_replay(existing, payload):
                raise ApplicationSkillConsumerRuntimeError(
                    "Application Skill resolution trace identity conflict"
                )
            return _validate_trace(existing)
        try:
            self.object_store.write(
                self.collection,
                resolution_id,
                payload,
                expected_revision=0,
            )
        except ValueError as error:
            raced = self.object_store.read(self.collection, resolution_id)
            if raced is None or not _same_trace_replay(raced, payload):
                raise ApplicationSkillConsumerRuntimeError(
                    "Application Skill resolution trace persistence conflict"
                ) from error
            return _validate_trace(raced)
        return payload

    def get_trace(self, resolution_id: str) -> Mapping[str, object] | None:
        stored = self.object_store.read(self.collection, resolution_id)
        return _validate_trace(stored) if stored is not None else None


@dataclass(frozen=True, slots=True)
class ApplicationSkillConsumerRuntime:
    catalog: ApplicationSkillCatalog
    sources: tuple[ApplicationSkillSource, ...]
    resolver: ApplicationSkillResolver
    external_sources: Callable[[str], tuple[ApplicationSkillSource, ...]] | None = None
    external_packages: Callable[[str], tuple[ApplicationSkillPackage, ...]] | None = None

    def resolve_context(
        self,
        *,
        project_id: str,
        consumer: str,
        task_kind: str,
        task_text: str,
        invocation_id: str,
        project_summary: str = "",
        enabled_skill_ids: Sequence[str] | None = None,
        max_instruction_bytes: int = 24 * 1024,
        max_context_bytes: int = 32 * 1024,
    ) -> Mapping[str, object]:
        if self.external_packages is not None:
            base = self.catalog.discover(self.sources)
            try:
                external = _verified_external_packages(
                    self.external_packages(project_id)
                )
            except ApplicationSkillConsumerRuntimeError:
                raise
            except Exception as error:
                raise ApplicationSkillConsumerRuntimeError(
                    "external Application Skill package callback failed"
                ) from error
            composed = self.catalog.snapshot_from_packages(
                (*base.packages, *external),
                scanned_source_count=(
                    base.scanned_source_count
                    + len({package.source_id for package in external})
                ),
            )
            snapshot = replace(
                composed,
                issues=tuple(
                    sorted(
                        (*base.issues, *composed.issues),
                        key=lambda item: (item.source_id, item.package_name, item.code),
                    )
                ),
            )
        else:
            dynamic = self.external_sources(project_id) if self.external_sources is not None else ()
            snapshot = self.catalog.discover((*self.sources, *dynamic))
        resolution = self.resolver.resolve(
            snapshot,
            project_id=project_id,
            consumer=consumer,
            task_kind=task_kind,
            task_text=task_text,
            invocation_id=invocation_id,
            project_summary=project_summary,
            enabled_skill_ids=enabled_skill_ids,
            max_instruction_bytes=max_instruction_bytes,
            max_context_bytes=max_context_bytes,
        )
        return {
            "resolution_id": resolution.resolution_id,
            "project_id": resolution.project_id,
            "consumer": resolution.consumer,
            "context_markdown": resolution.context_markdown,
            "selected": [
                {
                    "skill_id": item.match.skill_id,
                    "skill_fingerprint": item.match.skill_fingerprint,
                    "binding_id": item.match.binding_id,
                    "binding_revision": item.match.binding_revision,
                    "score": item.match.score,
                    "priority": item.match.priority,
                }
                for item in resolution.selected
            ],
            "fallback": resolution.trace["fallback"],
        }


_RESOLUTION_ID = re.compile(r"^skill-resolution-[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SKILL_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONSUMERS = frozenset({
    "answer.model-request", "document.generate", "turn.agent-child",
    "turn.workbench-question",
})
_MAX_MATCHES = 256
_MAX_TRACE_BYTES = 128 * 1024
_SECRET = re.compile(r"(?i)(?:authorization\s*:|cookie\s*:|\bsk-[A-Za-z0-9_-]{16,}\b|api[_-]?key\s*[:=])")


def _verified_external_packages(value: object) -> tuple[ApplicationSkillPackage, ...]:
    """Reject any callback output that could reopen a mutable source path."""

    if not isinstance(value, tuple):
        raise ApplicationSkillConsumerRuntimeError(
            "external Application Skill packages must be a tuple"
        )
    packages: list[ApplicationSkillPackage] = []
    for package in value:
        if not isinstance(package, ApplicationSkillPackage):
            raise ApplicationSkillConsumerRuntimeError(
                "external Application Skill package is invalid"
            )
        if package.source_kind != "external" or package.verified_content is None:
            raise ApplicationSkillConsumerRuntimeError(
                "external Application Skill package is not immutable"
            )
        packages.append(package)
    return tuple(packages)


def _validate_trace(value: Mapping[str, object]) -> dict[str, object]:
    expected = {
        "schema_version",
        "resolution_id",
        "invocation_id",
        "project_id",
        "consumer",
        "task_kind",
        "task_fingerprint",
        "matched",
        "selected",
        "budget_excluded_skill_ids",
        "loaded_instruction_bytes",
        "context_size_bytes",
        "fallback",
        "recorded_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill trace schema")
    payload = json.loads(json.dumps(dict(value), ensure_ascii=False))
    if payload.get("schema_version") != "1.0.0":
        raise ApplicationSkillConsumerRuntimeError("unsupported Application Skill trace schema")
    if not _RESOLUTION_ID.fullmatch(str(payload.get("resolution_id") or "")):
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill resolution id")
    for field in ("invocation_id", "project_id", "task_kind"):
        if not _SAFE_ID.fullmatch(str(payload.get(field) or "")):
            raise ApplicationSkillConsumerRuntimeError(f"invalid Application Skill trace {field}")
    if payload.get("consumer") not in _ALLOWED_CONSUMERS:
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill trace consumer")
    if not _SHA256.fullmatch(str(payload.get("task_fingerprint") or "")):
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill task fingerprint")
    matched = _array(payload.get("matched"), "matched", _MAX_MATCHES)
    selected = _array(payload.get("selected"), "selected", 3)
    excluded = _array(payload.get("budget_excluded_skill_ids"), "budget excluded", _MAX_MATCHES)
    clean_matched = [_match_trace(item) for item in matched]
    clean_selected = [_selected_trace(item) for item in selected]
    matched_by_id = {item["skill_id"]: item for item in clean_matched}
    if len(matched_by_id) != len(clean_matched):
        raise ApplicationSkillConsumerRuntimeError("duplicate Application Skill match trace")
    matched_ids = set(matched_by_id)
    selected_ids = [item["skill_id"] for item in clean_selected]
    if len(selected_ids) != len(set(selected_ids)) or any(item not in matched_ids for item in selected_ids):
        raise ApplicationSkillConsumerRuntimeError("Application Skill selected trace drifted")
    if {item["skill_id"] for item in clean_matched if item["selected"]} != set(selected_ids):
        raise ApplicationSkillConsumerRuntimeError("Application Skill match selection flags drifted")
    for item in clean_selected:
        matched_item = matched_by_id[item["skill_id"]]
        for field in (
            "skill_fingerprint",
            "binding_id",
            "binding_revision",
            "score",
            "priority",
        ):
            if item[field] != matched_item[field]:
                raise ApplicationSkillConsumerRuntimeError("Application Skill selected evidence drifted")
    clean_excluded = [_skill_id(item) for item in excluded]
    if (
        len(clean_excluded) != len(set(clean_excluded))
        or any(item not in matched_ids or item in selected_ids for item in clean_excluded)
    ):
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill budget exclusion")
    loaded = _bounded_int(payload.get("loaded_instruction_bytes"), 0, 24 * 1024, "loaded bytes")
    context = _bounded_int(payload.get("context_size_bytes"), 0, 32 * 1024, "context bytes")
    if loaded != sum(int(item["instruction_bytes"]) for item in clean_selected):
        raise ApplicationSkillConsumerRuntimeError("Application Skill loaded byte evidence drifted")
    if bool(clean_selected) != bool(context):
        raise ApplicationSkillConsumerRuntimeError("Application Skill context size evidence drifted")
    if payload.get("fallback") not in {"none", "default_consumer_flow"}:
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill trace fallback")
    if bool(clean_selected) == (payload["fallback"] != "none"):
        raise ApplicationSkillConsumerRuntimeError("Application Skill fallback and selection drifted")
    recorded_at = payload.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at.strip():
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill trace timestamp")
    payload.update(
        matched=clean_matched,
        selected=clean_selected,
        budget_excluded_skill_ids=clean_excluded,
        loaded_instruction_bytes=loaded,
        context_size_bytes=context,
    )
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_TRACE_BYTES:
        raise ApplicationSkillConsumerRuntimeError("Application Skill trace exceeds its hard budget")
    return payload


def _match_trace(value: object) -> dict[str, object]:
    expected = {
        "skill_id",
        "skill_fingerprint",
        "binding_id",
        "binding_revision",
        "score",
        "priority",
        "reasons",
        "selected",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill match trace")
    reasons = _array(value.get("reasons"), "match reasons", 64)
    clean_reasons: list[str] = []
    for reason in reasons:
        text = reason.strip() if isinstance(reason, str) else ""
        if not text or len(text) > 200 or _SECRET.search(text):
            raise ApplicationSkillConsumerRuntimeError("invalid Application Skill match reason")
        clean_reasons.append(text)
    selected = value.get("selected")
    if not isinstance(selected, bool):
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill match selection")
    return {
        "skill_id": _skill_id(value.get("skill_id")),
        "skill_fingerprint": _sha(value.get("skill_fingerprint"), "match fingerprint"),
        "binding_id": _safe_id(value.get("binding_id"), "binding id"),
        "binding_revision": _bounded_int(value.get("binding_revision"), 1, 1_000_000, "binding revision"),
        "score": _bounded_int(value.get("score"), 0, 100_000, "match score"),
        "priority": _bounded_int(value.get("priority"), 0, 1000, "match priority"),
        "reasons": clean_reasons,
        "selected": selected,
    }


def _selected_trace(value: object) -> dict[str, object]:
    expected = {
        "skill_id",
        "skill_fingerprint",
        "binding_id",
        "binding_revision",
        "score",
        "priority",
        "instruction_bytes",
        "resource_count",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill selected trace")
    return {
        "skill_id": _skill_id(value.get("skill_id")),
        "skill_fingerprint": _sha(value.get("skill_fingerprint"), "selected fingerprint"),
        "binding_id": _safe_id(value.get("binding_id"), "binding id"),
        "binding_revision": _bounded_int(value.get("binding_revision"), 1, 1_000_000, "binding revision"),
        "score": _bounded_int(value.get("score"), 0, 100_000, "selected score"),
        "priority": _bounded_int(value.get("priority"), 0, 1000, "selected priority"),
        "instruction_bytes": _bounded_int(value.get("instruction_bytes"), 1, 16 * 1024, "instruction bytes"),
        "resource_count": _bounded_int(value.get("resource_count"), 0, 127, "resource count"),
    }


def _array(value: object, label: str, maximum: int) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > maximum:
        raise ApplicationSkillConsumerRuntimeError(f"invalid or unbounded Application Skill {label}")
    return list(value)


def _skill_id(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SKILL_ID.fullmatch(text):
        raise ApplicationSkillConsumerRuntimeError("invalid Application Skill trace skill id")
    return text


def _safe_id(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SAFE_ID.fullmatch(text):
        raise ApplicationSkillConsumerRuntimeError(f"invalid Application Skill trace {label}")
    return text


def _sha(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SHA256.fullmatch(text):
        raise ApplicationSkillConsumerRuntimeError(f"invalid Application Skill trace {label}")
    return text


def _bounded_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ApplicationSkillConsumerRuntimeError(f"invalid Application Skill trace {label}")
    return value


def _same_trace_replay(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    left_value = dict(left)
    right_value = dict(right)
    left_value.pop("recorded_at", None)
    right_value.pop("recorded_at", None)
    return left_value == right_value
