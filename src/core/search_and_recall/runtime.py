from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from threading import Lock

from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError
from .ports import RecallHit, RecallIndexEntry, RecallQuery


class RecallRepositoryError(ValueError):
    """Raised when recall request/result persistence would violate isolation."""


DEFAULT_RECALL_LAYERS = (
    "l4_persona",
    "l3_project_skill",
    "l3_series_memory",
    "l2_scenario",
    "l1_atom",
    "l0_source",
)
DEFAULT_PER_LAYER_LIMITS = {
    "l4_persona": 1,
    "l3_project_skill": 1,
    "l3_series_memory": 2,
    "l2_scenario": 3,
    "l1_atom": 6,
    "l0_source": 3,
}
DEFAULT_TRUST_INCLUDE = ("trusted", "user_confirmed", "system_generated")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_RECALL_WRITE_LOCK_GUARD = Lock()


@dataclass(slots=True)
class _RecallWriteLock:
    lock: object
    users: int = 0


_RECALL_WRITE_LOCKS: dict[str, _RecallWriteLock] = {}


@dataclass(frozen=True, slots=True)
class _BudgetedAdapterHits:
    selected: list[dict[str, object]]
    dropped_hit_ids: list[str]
    reason: str


@dataclass(frozen=True, slots=True)
class InMemoryRecallIndexAdapter:
    """Deterministic Recall index adapter stub with no external index dependency."""

    entries: tuple[RecallIndexEntry, ...]

    def __init__(self, entries: Sequence[RecallIndexEntry | Mapping[str, object]]) -> None:
        object.__setattr__(self, "entries", tuple(_index_entry(entry) for entry in entries))

    def recall(self, query: RecallQuery) -> tuple[RecallHit, ...]:
        if query.limit <= 0:
            return ()
        ranked: list[RecallHit] = []
        layer_order = {layer: index for index, layer in enumerate(query.layers)}
        for entry in self.entries:
            if query.project_id is not None and entry.project_id != query.project_id:
                continue
            if entry.layer not in layer_order:
                continue
            if entry.trust_status not in query.allowed_trust_statuses:
                continue
            if not entry.source_refs:
                continue
            ranked.append(
                RecallHit(
                    object_id=entry.object_id,
                    layer=entry.layer,
                    content=entry.content,
                    source_refs=entry.source_refs,
                    trust_status=entry.trust_status,
                    score=_rank_score(query.text, entry),
                )
            )
        ranked.sort(
            key=lambda hit: (
                -hit.score,
                layer_order.get(hit.layer, len(layer_order)),
                hit.object_id,
            )
        )
        return tuple(ranked[: query.limit])


@dataclass(frozen=True, slots=True)
class ObjectStoreRecallIndex:
    """Persistent Recall index boundary backed by rebuild ObjectStore.

    R040 intentionally keeps ranking lexical and deterministic. The goal is a
    durable backend seam that can later be replaced by FTS/vector engines
    without weakening project, layer, trust and source-ref guards.
    """

    object_store: ObjectStorePort
    entry_collection: str = "recall_index_entries"
    manifest_collection: str = "recall_index_manifests"
    manifest_id: str = "active"
    backend_kind: str = "object_store_lexical"

    def rebuild(
        self,
        entries: Sequence[RecallIndexEntry | Mapping[str, object]],
        *,
        source: str,
        rebuilt_at: str | None = None,
        vector_enabled: bool = False,
    ) -> Mapping[str, object]:
        if vector_enabled:
            raise RecallRepositoryError("R040 persistent recall index does not enable vector search")
        normalized = tuple(_index_entry(entry) for entry in entries)
        seen: set[str] = set()
        for entry in normalized:
            _validate_persistent_index_entry(entry)
            if entry.object_id in seen:
                raise RecallRepositoryError("recall index entry object_id must be unique")
            seen.add(entry.object_id)
        for item in self.object_store.list(self.entry_collection):
            object_id = item.get("object_id")
            if isinstance(object_id, str) and object_id not in seen:
                self.object_store.delete(self.entry_collection, object_id)
        for entry in normalized:
            payload = _index_entry_payload(entry)
            self.object_store.write(
                self.entry_collection,
                entry.object_id,
                payload,
                expected_revision=None,
            )
        manifest = {
            "schema_version": "1.0.0",
            "id": self.manifest_id,
            "backend_kind": self.backend_kind,
            "source": source,
            "entry_count": len(normalized),
            "project_ids": _unique_strings(entry.project_id for entry in normalized),
            "layers": _unique_strings(entry.layer for entry in normalized),
            "trust_statuses": _unique_strings(entry.trust_status for entry in normalized),
            "vector": {
                "enabled": False,
                "provider": None,
                "dimension": None,
            },
            "rebuilt_at": rebuilt_at or _utc_now(),
        }
        self.object_store.write(
            self.manifest_collection,
            self.manifest_id,
            manifest,
            expected_revision=None,
        )
        return manifest

    def manifest(self) -> Mapping[str, object] | None:
        manifest = self.object_store.read(self.manifest_collection, self.manifest_id)
        return dict(manifest) if manifest is not None else None

    def entries(self) -> tuple[RecallIndexEntry, ...]:
        return tuple(
            _index_entry(item)
            for item in self.object_store.list(self.entry_collection)
        )

    def recall(self, query: RecallQuery) -> tuple[RecallHit, ...]:
        return InMemoryRecallIndexAdapter(self.entries()).recall(query)


@dataclass(frozen=True, slots=True)
class ObjectStoreRecallRepository:
    """Persist Recall Request/Result contracts without coupling to an index engine."""

    object_store: ObjectStorePort
    namespace_id: str = "default"
    request_collection: str = "recall_requests"
    result_collection: str = "recall_results"

    def create_project_default_request(
        self,
        *,
        project_id: str,
        query: str,
        project_skill_id: str,
        layers: Sequence[str] | None = None,
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Create and persist the isolated project-scope recall request.

        This is the Phase 5 entry point: recall must start from the current
        Project Skill and must not cross project boundaries by default.
        """

        selected_layers = tuple(layers or DEFAULT_RECALL_LAYERS)
        request_id = _stable_id(
            "recall-request",
            project_id,
            query,
            project_skill_id,
            ",".join(selected_layers),
        )
        required_context_refs = []
        if "l3_project_skill" in selected_layers:
            required_context_refs.append(
                {
                    "context_id": _stable_id(
                        "ctx-project-skill",
                        project_id,
                        project_skill_id,
                    ),
                    "kind": "project_skill",
                    "object_id": project_skill_id,
                    "uri": (
                        f"crp://{self.namespace_id}/projects/"
                        f"{project_id}/project-skill.json"
                    ),
                    "reason": "当前 query 计划要求读取项目 Skill。",
                }
            )
        request = {
            "schema_version": "1.0.0",
            "id": request_id,
            "query": query,
            "project_id": project_id,
            "scope": "project",
            "layers": list(selected_layers),
            "trust_filter": {
                "include": list(DEFAULT_TRUST_INCLUDE),
                "minimum_confidence": 0.6,
                "allow_imported_unverified": False,
            },
            "budget": {
                "max_hits": 12,
                "max_tokens": 12000,
                "per_layer_limits": dict(DEFAULT_PER_LAYER_LIMITS),
            },
            "cross_project": {
                "allowed": False,
                "grant_id": None,
                "project_ids": [],
            },
            "required_context_refs": required_context_refs,
            "created_at": created_at or _utc_now(),
        }
        return self.save_request(request)

    def save_request(self, request: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(request)
        _validate_request(payload)
        request_id = _required_string(payload, "id")
        with _recall_write_lock(self.object_store, self.request_collection, request_id):
            try:
                self.object_store.write(self.request_collection, request_id, payload, expected_revision=0)
            except ObjectStoreRevisionError as exc:
                existing = self.get_request(request_id)
                if existing is not None and _replay_payload(existing) == _replay_payload(payload):
                    return existing
                raise RecallRepositoryError(f"recall request conflicts with existing payload: {request_id}") from exc
        return payload

    def get_request(self, request_id: str) -> Mapping[str, object] | None:
        stored = self.object_store.read(self.request_collection, request_id)
        return dict(stored) if stored is not None else None

    def list_requests(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.request_collection)
            if item.get("project_id") == project_id
        )

    def save_result(self, result: Mapping[str, object]) -> Mapping[str, object]:
        payload = dict(result)
        _validate_result(payload, request=self.get_request(_required_string(payload, "request_id")))
        result_id = _required_string(payload, "id")
        with _recall_write_lock(self.object_store, self.result_collection, result_id):
            try:
                self.object_store.write(self.result_collection, result_id, payload, expected_revision=0)
            except ObjectStoreRevisionError as exc:
                existing = self.get_result(result_id)
                if existing is not None and _replay_payload(existing) == _replay_payload(payload):
                    return existing
                raise RecallRepositoryError(f"recall result conflicts with existing payload: {result_id}") from exc
        return payload

    def create_result_from_hits(
        self,
        *,
        request_id: str,
        hits: Sequence[RecallHit],
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Persist adapter-ranked hits as an evidence-only Recall Result."""

        request = self.get_request(request_id)
        if request is None:
            raise RecallRepositoryError(f"recall request not found: {request_id}")
        if not hits:
            raise RecallRepositoryError("recall result from adapter requires hits")
        project_id = _required_string(request, "project_id")
        layers = _required_string_list(request, "layers")
        candidate_hits = [
            _recall_hit_from_adapter_hit(
                request_id=request_id,
                project_id=project_id,
                hit=hit,
            )
            for hit in hits
        ]
        budgeted = _apply_adapter_budget(request=request, hits=candidate_hits)
        result_hits = budgeted.selected
        if not result_hits:
            return self.create_insufficient_evidence_result(
                request_id=request_id,
                message="召回证据超出当前上下文预算。",
                created_at=created_at,
            )
        covered_layers = _unique_strings(
            str(hit["layer"]) for hit in result_hits if isinstance(hit.get("layer"), str)
        )
        missing_layers = [layer for layer in layers if layer not in covered_layers]
        source_ref_count = sum(
            len(hit["source_refs"]) for hit in result_hits if isinstance(hit.get("source_refs"), list)
        )
        token_total = sum(_required_int(hit, "token_estimate") for hit in result_hits)
        result = {
            "schema_version": "1.0.0",
            "id": _compact_id(
                "recall-result-adapter",
                request_id,
                _payload_fingerprint(result_hits),
            ),
            "request_id": request_id,
            "project_id": project_id,
            "status": "evidence_found" if not missing_layers else "partial",
            "hits": result_hits,
            "coverage": {
                "status": "sufficient" if not missing_layers else "partial",
                "requested_layers": layers,
                "covered_layers": covered_layers,
                "missing_layers": missing_layers,
                "low_trust": False,
                "source_ref_count": source_ref_count,
            },
            "truncation": {
                "applied": bool(budgeted.dropped_hit_ids),
                "reason": budgeted.reason,
                "dropped_hit_ids": budgeted.dropped_hit_ids,
                "final_hit_count": len(result_hits),
                "final_token_estimate": token_total,
            },
            "explanation": {
                "summary": "已将 Recall adapter 排序后的证据保存为可追溯 Recall Result。",
                "layer_order": layers,
                "warnings": ["adapter_handoff_smoke"],
            },
            "cross_project": {
                "used": False,
                "grant_id": None,
                "project_ids": [],
            },
            "errors": [],
            "created_at": created_at or _utc_now(),
        }
        return self.save_result(result)

    def create_insufficient_evidence_result(
        self,
        *,
        request_id: str,
        message: str = "没有找到足够证据。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Persist a model-free insufficient-evidence Recall Result.

        This is the explicit no-evidence path: callers get a traceable Recall
        Result and must not fabricate an answer when no acceptable evidence is
        available.
        """

        request = self.get_request(request_id)
        if request is None:
            raise RecallRepositoryError(f"recall request not found: {request_id}")
        layers = _required_string_list(request, "layers")
        result = {
            "schema_version": "1.0.0",
            "id": _compact_id("recall-result-empty", request_id, message),
            "request_id": request_id,
            "project_id": _required_string(request, "project_id"),
            "status": "insufficient_evidence",
            "hits": [],
            "coverage": {
                "status": "insufficient",
                "requested_layers": layers,
                "covered_layers": [],
                "missing_layers": layers,
                "low_trust": False,
                "source_ref_count": 0,
            },
            "truncation": {
                "applied": False,
                "reason": "none",
                "dropped_hit_ids": [],
                "final_hit_count": 0,
                "final_token_estimate": 0,
            },
            "explanation": {
                "summary": "没有找到可引用证据，调用方应明确提示证据不足。",
                "layer_order": layers,
                "warnings": ["insufficient_evidence"],
            },
            "cross_project": {
                "used": False,
                "grant_id": None,
                "project_ids": [],
            },
            "errors": [
                {
                    "code": "insufficient_evidence",
                    "message": message,
                }
            ],
            "created_at": created_at or _utc_now(),
        }
        return self.save_result(result)

    def create_index_unavailable_result(
        self,
        *,
        request_id: str,
        message: str = "召回索引暂不可用，无法完成证据检索。",
        created_at: str | None = None,
    ) -> Mapping[str, object]:
        """Persist a model-free index-unavailable Recall Result.

        This is the explicit infrastructure failure path: callers get a
        traceable Recall Result and must not create a model answer request
        from an unavailable index result.
        """

        request = self.get_request(request_id)
        if request is None:
            raise RecallRepositoryError(f"recall request not found: {request_id}")
        layers = _required_string_list(request, "layers")
        result = {
            "schema_version": "1.0.0",
            "id": _compact_id("recall-result-index-unavailable", request_id, message),
            "request_id": request_id,
            "project_id": _required_string(request, "project_id"),
            "status": "index_unavailable",
            "hits": [],
            "coverage": {
                "status": "insufficient",
                "requested_layers": layers,
                "covered_layers": [],
                "missing_layers": layers,
                "low_trust": False,
                "source_ref_count": 0,
            },
            "truncation": {
                "applied": False,
                "reason": "none",
                "dropped_hit_ids": [],
                "final_hit_count": 0,
                "final_token_estimate": 0,
            },
            "explanation": {
                "summary": "召回索引不可用，调用方应提示稍后重试或降级到可用证据路径。",
                "layer_order": layers,
                "warnings": ["index_unavailable"],
            },
            "cross_project": {
                "used": False,
                "grant_id": None,
                "project_ids": [],
            },
            "errors": [
                {
                    "code": "index_unavailable",
                    "message": message,
                }
            ],
            "created_at": created_at or _utc_now(),
        }
        return self.save_result(result)

    def get_result(self, result_id: str) -> Mapping[str, object] | None:
        stored = self.object_store.read(self.result_collection, result_id)
        return dict(stored) if stored is not None else None

    def list_results(self, project_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.result_collection)
            if item.get("project_id") == project_id
        )

    def results_for_request(self, request_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            dict(item)
            for item in self.object_store.list(self.result_collection)
            if item.get("request_id") == request_id
        )


def _validate_request(request: Mapping[str, object]) -> None:
    if request.get("scope") != "project":
        cross_project = request.get("cross_project")
        if not isinstance(cross_project, Mapping) or cross_project.get("allowed") is not True:
            raise RecallRepositoryError("cross_project recall request requires an explicit grant")
        if not cross_project.get("grant_id") or not cross_project.get("project_ids"):
            raise RecallRepositoryError("cross_project recall request requires grant_id and project_ids")
    else:
        cross_project = request.get("cross_project")
        if not isinstance(cross_project, Mapping):
            raise RecallRepositoryError("recall request requires cross_project policy")
        if cross_project.get("allowed") is not False or cross_project.get("grant_id") is not None:
            raise RecallRepositoryError("project recall request must keep cross-project disabled")
        if cross_project.get("project_ids") != []:
            raise RecallRepositoryError("project recall request must not include foreign project ids")

    layers = request.get("layers")
    if not isinstance(layers, Sequence) or isinstance(layers, (str, bytes)) or not layers:
        raise RecallRepositoryError("recall request requires layers")
    if any(layer not in DEFAULT_RECALL_LAYERS for layer in layers):
        raise RecallRepositoryError("recall request contains an unsupported layer")
    expected_order = [
        layer
        for layer in DEFAULT_RECALL_LAYERS
        if layer in layers
    ]
    if list(layers) != expected_order:
        raise RecallRepositoryError("recall request layers are not in canonical order")

    context_refs = request.get("required_context_refs")
    if not isinstance(context_refs, Sequence) or isinstance(context_refs, (str, bytes)):
        raise RecallRepositoryError("recall request requires context refs")
    skill_refs = [
        item
        for item in context_refs
        if isinstance(item, Mapping) and item.get("kind") == "project_skill"
    ]
    if ("l3_project_skill" in layers) != bool(skill_refs):
        raise RecallRepositoryError(
            "Project Skill context must match the query-planned layer"
        )

    trust_filter = request.get("trust_filter")
    if not isinstance(trust_filter, Mapping):
        raise RecallRepositoryError("recall request requires trust_filter")
    include = trust_filter.get("include")
    if not isinstance(include, Sequence) or isinstance(include, (str, bytes)):
        raise RecallRepositoryError("recall request trust_filter.include must be a sequence")
    if trust_filter.get("allow_imported_unverified") is False and "imported_unverified" in include:
        raise RecallRepositoryError("recall request cannot include imported_unverified when it is disallowed")


def _validate_result(result: Mapping[str, object], *, request: Mapping[str, object] | None) -> None:
    result_project_id = _required_string(result, "project_id")
    if request is not None and request.get("project_id") != result_project_id:
        raise RecallRepositoryError("recall result project_id must match its request")

    hits = result.get("hits")
    if not isinstance(hits, Sequence) or isinstance(hits, (str, bytes)):
        raise RecallRepositoryError("recall result requires hits")

    cross_project = result.get("cross_project")
    if not isinstance(cross_project, Mapping):
        raise RecallRepositoryError("recall result requires cross_project policy")
    cross_used = cross_project.get("used") is True
    if cross_used and (not cross_project.get("grant_id") or not cross_project.get("project_ids")):
        raise RecallRepositoryError("cross-project recall result requires grant_id and project_ids")
    if not cross_used and (cross_project.get("grant_id") is not None or cross_project.get("project_ids") != []):
        raise RecallRepositoryError("project recall result must keep cross-project disabled")

    foreign_hits = []
    for hit in hits:
        if not isinstance(hit, Mapping):
            raise RecallRepositoryError("recall result hit must be an object")
        hit_project_id = hit.get("project_id")
        if hit_project_id != result_project_id:
            foreign_hits.append(hit)
        if hit_project_id == result_project_id and hit.get("source_project_label") is not None:
            raise RecallRepositoryError("same-project recall hit must not use source_project_label")
    if foreign_hits and not cross_used:
        raise RecallRepositoryError("foreign recall hit requires cross-project grant")
    if foreign_hits and any(hit.get("source_project_label") is None for hit in foreign_hits):
        raise RecallRepositoryError("cross-project recall hit requires source_project_label")

    status = result.get("status")
    errors = result.get("errors")
    if not isinstance(errors, Sequence) or isinstance(errors, (str, bytes)):
        raise RecallRepositoryError("recall result requires errors")
    if status == "insufficient_evidence":
        if hits:
            raise RecallRepositoryError("insufficient_evidence result must not include hits")
        if not any(isinstance(error, Mapping) and error.get("code") == "insufficient_evidence" for error in errors):
            raise RecallRepositoryError("insufficient_evidence result requires matching error")
    if status == "index_unavailable":
        if hits:
            raise RecallRepositoryError("index_unavailable result must not include hits")
        if not any(isinstance(error, Mapping) and error.get("code") == "index_unavailable" for error in errors):
            raise RecallRepositoryError("index_unavailable result requires matching error")
    if status == "evidence_found" and not hits:
        raise RecallRepositoryError("evidence_found result requires hits")

    truncation = result.get("truncation")
    if not isinstance(truncation, Mapping):
        raise RecallRepositoryError("recall result requires truncation")
    if truncation.get("final_hit_count") != len(hits):
        raise RecallRepositoryError("recall result final_hit_count must match hits")
    token_total = sum(int(hit.get("token_estimate", 0)) for hit in hits if isinstance(hit, Mapping))
    if truncation.get("final_token_estimate") != token_total:
        raise RecallRepositoryError("recall result final_token_estimate must match hits")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RecallRepositoryError(f"recall payload requires {key}")
    return value


def _required_string_list(payload: Mapping[str, object], key: str) -> list[str]:
    values = payload.get(key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise RecallRepositoryError(f"recall payload requires {key}")
    if not all(isinstance(value, str) and value for value in values):
        raise RecallRepositoryError(f"recall payload {key} must contain strings")
    return list(values)


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RecallRepositoryError(f"recall payload requires integer {key}")
    return value


def _recall_hit_from_adapter_hit(
    *,
    request_id: str,
    project_id: str,
    hit: RecallHit,
) -> dict[str, object]:
    if not hit.source_refs:
        raise RecallRepositoryError("adapter hit requires source refs")
    if not hit.content:
        raise RecallRepositoryError("adapter hit requires content")
    if not 0 <= hit.score <= 1:
        raise RecallRepositoryError("adapter hit score must be between 0 and 1")
    source_refs = [_source_ref_from_adapter_ref(ref) for ref in hit.source_refs]
    return {
        "hit_id": _compact_id("hit-adapter", request_id, hit.layer, hit.object_id),
        "layer": hit.layer,
        "object_id": hit.object_id,
        "project_id": project_id,
        "source_project_label": None,
        "trust_status": hit.trust_status,
        "score": hit.score,
        "token_estimate": _token_estimate(hit.content),
        "source_refs": source_refs,
        "snippet": hit.content,
        "explanation": "Recall adapter returned this traceable same-project evidence.",
    }


def _apply_adapter_budget(
    *,
    request: Mapping[str, object],
    hits: list[dict[str, object]],
) -> _BudgetedAdapterHits:
    budget = request.get("budget")
    max_hits = 12
    max_tokens = 12000
    per_layer_limits: Mapping[str, object] = {}
    if isinstance(budget, Mapping):
        if _is_positive_int(budget.get("max_hits")):
            max_hits = int(budget["max_hits"])
        if _is_positive_int(budget.get("max_tokens")):
            max_tokens = int(budget["max_tokens"])
        limits = budget.get("per_layer_limits")
        if isinstance(limits, Mapping):
            per_layer_limits = limits

    selected: list[dict[str, object]] = []
    dropped_hit_ids: list[str] = []
    layer_counts: dict[str, int] = {}
    token_total = 0
    hit_limited = False
    token_limited = False
    for hit in hits:
        layer = _required_string(hit, "layer")
        token_estimate = _required_int(hit, "token_estimate")
        layer_limit = per_layer_limits.get(layer, max_hits)
        if _is_positive_int(layer_limit) and layer_counts.get(layer, 0) >= int(layer_limit):
            dropped_hit_ids.append(_required_string(hit, "hit_id"))
            hit_limited = True
            continue
        if len(selected) >= max_hits:
            dropped_hit_ids.append(_required_string(hit, "hit_id"))
            hit_limited = True
            continue
        if token_total + token_estimate > max_tokens:
            dropped_hit_ids.append(_required_string(hit, "hit_id"))
            token_limited = True
            continue
        selected.append(hit)
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        token_total += token_estimate

    if hit_limited and token_limited:
        reason = "budget"
    elif token_limited:
        reason = "token_limit"
    elif hit_limited:
        reason = "hit_limit"
    else:
        reason = "none"
    return _BudgetedAdapterHits(
        selected=selected,
        dropped_hit_ids=dropped_hit_ids,
        reason=reason,
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _source_ref_from_adapter_ref(ref: str) -> dict[str, object]:
    if "#" not in ref:
        raise RecallRepositoryError("adapter source ref must use source_id#locator format")
    source_id, locator = ref.split("#", 1)
    if not source_id or not locator:
        raise RecallRepositoryError("adapter source ref requires source_id and locator")
    return {"source_id": source_id, "locator": locator}


def _token_estimate(text: str) -> int:
    return max(1, min(12000, len(text)))


def _unique_strings(values: object) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value not in result:
            result.append(value)
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{parts[0]}-{digest}"


def _compact_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _payload_fingerprint(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay_payload(payload: Mapping[str, object]) -> dict[str, object]:
    canonical = dict(payload)
    canonical.pop("created_at", None)
    return canonical


@contextmanager
def _recall_write_lock(
    object_store: ObjectStorePort,
    collection: str,
    object_id: str,
) -> Iterator[None]:
    root = getattr(object_store, "root", None)
    namespace = getattr(object_store, "namespace_id", "default")
    store_identity = str(root) if root is not None else f"instance:{id(object_store)}"
    key = f"{store_identity}\n{namespace}\n{collection}\n{object_id}"
    with _RECALL_WRITE_LOCK_GUARD:
        entry = _RECALL_WRITE_LOCKS.get(key)
        if entry is None:
            entry = _RecallWriteLock(lock=Lock())
            _RECALL_WRITE_LOCKS[key] = entry
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _RECALL_WRITE_LOCK_GUARD:
            entry.users -= 1
            if entry.users == 0 and _RECALL_WRITE_LOCKS.get(key) is entry:
                _RECALL_WRITE_LOCKS.pop(key, None)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _index_entry(entry: RecallIndexEntry | Mapping[str, object]) -> RecallIndexEntry:
    if isinstance(entry, RecallIndexEntry):
        _validate_index_entry(entry)
        return entry
    source_refs = entry.get("source_refs")
    if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
        raise RecallRepositoryError("recall index entry requires source_refs")
    normalized = RecallIndexEntry(
        object_id=_required_string(entry, "object_id"),
        project_id=_required_string(entry, "project_id"),
        layer=_required_string(entry, "layer"),
        content=_required_string(entry, "content"),
        source_refs=tuple(str(ref) for ref in source_refs if isinstance(ref, str) and ref),
        trust_status=_required_string(entry, "trust_status"),
        base_score=_score_value(entry.get("base_score", 0.5)),
    )
    _validate_index_entry(normalized)
    return normalized


def _index_entry_payload(entry: RecallIndexEntry) -> dict[str, object]:
    _validate_index_entry(entry)
    return {
        "schema_version": "1.0.0",
        "object_id": entry.object_id,
        "project_id": entry.project_id,
        "layer": entry.layer,
        "content": entry.content,
        "source_refs": list(entry.source_refs),
        "trust_status": entry.trust_status,
        "base_score": entry.base_score,
    }


def _validate_index_entry(entry: RecallIndexEntry) -> None:
    if not entry.object_id:
        raise RecallRepositoryError("recall index entry requires object_id")
    if not entry.project_id:
        raise RecallRepositoryError("recall index entry requires project_id")
    if entry.layer not in DEFAULT_RECALL_LAYERS:
        raise RecallRepositoryError("recall index entry layer is not supported")
    if not entry.content:
        raise RecallRepositoryError("recall index entry requires content")
    if not all(isinstance(ref, str) and ref for ref in entry.source_refs):
        raise RecallRepositoryError("recall index entry source_refs must contain strings")
    if not entry.trust_status:
        raise RecallRepositoryError("recall index entry requires trust_status")
    if not 0 <= entry.base_score <= 1:
        raise RecallRepositoryError("recall index entry base_score must be between 0 and 1")


def _validate_persistent_index_entry(entry: RecallIndexEntry) -> None:
    for ref in entry.source_refs:
        if "#" not in ref:
            raise RecallRepositoryError("persistent recall index source_ref must use source_id#locator format")
        source_id, locator = ref.split("#", 1)
        if not source_id or not locator:
            raise RecallRepositoryError("persistent recall index source_ref requires source_id and locator")


def _score_value(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise RecallRepositoryError("recall index entry base_score must be numeric")


def _rank_score(query_text: str, entry: RecallIndexEntry) -> float:
    query_tokens = _tokens(query_text)
    content_tokens = _tokens(entry.content)
    if not query_tokens or not content_tokens:
        overlap_ratio = 0.0
    else:
        overlap_ratio = len(query_tokens.intersection(content_tokens)) / len(query_tokens)
    return round(min(1.0, entry.base_score + overlap_ratio * 0.4), 6)


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text)}
