from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from core.product_core.project_memory_recall import (
    ProjectMemoryRecallError,
    ProjectMemoryRecallResult,
)


class WorkbenchDirectQuestionStorePort(Protocol):
    """Persists direct workbench QA records outside Source / Job / Library."""

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        """Persist one direct QA record."""

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        """Read a prior direct QA record for idempotent replay."""


class WorkbenchDirectQuestionRecallPort(Protocol):
    """Optional recall step that pulls published series / scenario / atom context."""

    def execute(
        self,
        project_id: str,
        *,
        query: str,
        created_at: str | None = None,
    ) -> ProjectMemoryRecallResult:
        """Return same-project published memory evidence for the query."""




class WorkbenchDirectQuestionError(ValueError):
    """Raised when a direct question cannot be handled safely."""


@dataclass(frozen=True, slots=True)
class WorkbenchDirectQuestionResult:
    status: str
    question_id: str
    answer_id: str
    question: str
    answer_preview: str
    qa_mode: str
    knowledge_base_write: bool
    source_created: bool
    job_created: bool
    library_item_created: bool
    memory_publication_state: str
    blocked_operations: tuple[str, ...]
    recall_status: str = "disabled"
    recall_request_id: str | None = None
    recall_result_id: str | None = None
    evidence_refs: tuple[Mapping[str, object], ...] = ()
    evidence_count: int = 0
    provider_call_performed: bool = False
    provider_status: str = "not_configured"
    provider_route: str = ""
    context_fingerprint: str = ""
    recall_trace: Mapping[str, object] | None = None
    ephemeral_evidence_refs: tuple[Mapping[str, object], ...] = ()
    deep_evidence_count: int = 0
    provider_egress_authorized: bool = False
    replayed: bool = False


class AnswerWorkbenchDirectQuestion:
    """Handles the homepage unchecked QA path without creating knowledge records."""

    collection = "workbench_direct_questions"

    def __init__(
        self,
        object_store: WorkbenchDirectQuestionStorePort,
        *,
        namespace_id: str = "default",
        recall: WorkbenchDirectQuestionRecallPort | None = None,
        recall_project_id: str = "default",
        provider_unavailable_reason: str = "",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._recall = recall
        self._recall_project_id = recall_project_id
        self._provider_route = ""
        self._provider_unavailable_reason = provider_unavailable_reason
        self._provider_egress_authorized = False

    def execute(
        self,
        *,
        question: str,
        created_at: str | None = None,
    ) -> WorkbenchDirectQuestionResult:
        normalized = _required_question(question)
        timestamp = created_at or _utc_now()
        question_id = _stable_id(
            "workbench-direct-question",
            self._namespace_id,
            *(
                (normalized,)
                if self._recall_project_id == "default"
                else (self._recall_project_id, normalized)
            ),
        )
        recall_outcome = self._run_recall(query=normalized, created_at=timestamp)
        context_fingerprint = _context_fingerprint(
            normalized,
            recall_outcome,
            provider_egress_authorized=self._provider_egress_authorized,
            provider_route=self._provider_route,
        )
        existing = self._object_store.read(self.collection, question_id)
        replay = _replay_result(
            existing,
            context_fingerprint=context_fingerprint,
            recall_outcome=recall_outcome,
            provider_egress_authorized=self._provider_egress_authorized,
        )
        if replay is not None:
            return replay
        answer, provider_call_performed, provider_status = self._answer(
            question=normalized,
            recall_outcome=recall_outcome,
        )
        answer_id = _stable_id("workbench-direct-answer", question_id, answer)
        record = {
            "schema_version": "1.0.0",
            "status": "answered",
            "id": question_id,
            "answer_id": answer_id,
            "namespace_id": self._namespace_id,
            "question": normalized,
            "answer": answer,
            "qa_mode": "provider_evidence_answer" if provider_status == "succeeded" else "direct_local_answer",
            "knowledge_base_write": False,
            "source_created": False,
            "job_created": False,
            "library_item_created": False,
            "memory_publication_state": "not_published",
            "blocked_operations": [
                "source_creation",
                "job_creation",
                "library_item_creation",
                "long_term_memory_publication",
                *([] if provider_call_performed else ["remote_model_provider_execution", "api_key_use"]),
            ],
            "recall_status": recall_outcome.status,
            "recall_request_id": recall_outcome.request_id,
            "recall_result_id": recall_outcome.result_id,
            "evidence_refs": [dict(ref) for ref in recall_outcome.evidence_refs],
            "evidence_count": len(recall_outcome.evidence_refs),
            "deep_evidence": {
                "count": len(recall_outcome.ephemeral_evidence_refs),
                "layers": sorted(
                    {
                        str(ref.get("layer"))
                        for ref in recall_outcome.ephemeral_evidence_refs
                        if ref.get("layer")
                    }
                ),
                "ephemeral": True,
                "content_persisted": False,
            },
            "recall_trace": (
                dict(recall_outcome.progressive_trace)
                if recall_outcome.progressive_trace is not None
                else None
            ),
            "provider_call_performed": provider_call_performed,
            "provider_status": provider_status,
            "provider_route": self._provider_route if provider_call_performed else "",
            "context_fingerprint": context_fingerprint,
            "created_at": timestamp,
        }
        self._object_store.write(self.collection, question_id, record, expected_revision=None)
        return WorkbenchDirectQuestionResult(
            status="answered",
            question_id=question_id,
            answer_id=answer_id,
            question=normalized,
            answer_preview=answer[:800],
            qa_mode=record["qa_mode"],
            knowledge_base_write=False,
            source_created=False,
            job_created=False,
            library_item_created=False,
            memory_publication_state="not_published",
            blocked_operations=tuple(record["blocked_operations"]),
            recall_status=recall_outcome.status,
            recall_request_id=recall_outcome.request_id,
            recall_result_id=recall_outcome.result_id,
            evidence_refs=recall_outcome.evidence_refs,
            evidence_count=recall_outcome.evidence_count,
            provider_call_performed=provider_call_performed,
            provider_status=provider_status,
            provider_route=record["provider_route"],
            context_fingerprint=context_fingerprint,
            recall_trace=recall_outcome.progressive_trace,
            ephemeral_evidence_refs=recall_outcome.ephemeral_evidence_refs,
            deep_evidence_count=len(recall_outcome.ephemeral_evidence_refs),
            provider_egress_authorized=self._provider_egress_authorized,
        )

    def _answer(self, *, question: str, recall_outcome: _RecallOutcome) -> tuple[str, bool, str]:
        answer_evidence = recall_outcome.answer_evidence_refs
        if recall_outcome.status != "recalled" or not answer_evidence:
            return _local_answer(question, recall_outcome=recall_outcome), False, "not_attempted_no_evidence"
        status = "fallback_provider_unavailable" if self._provider_unavailable_reason else "fallback_not_configured"
        return _local_answer(question, recall_outcome=recall_outcome), False, status

    def _run_recall(self, *, query: str, created_at: str) -> _RecallOutcome:
        if self._recall is None:
            return _RecallOutcome(status="disabled")
        try:
            result = self._recall.execute(
                self._recall_project_id,
                query=query,
                created_at=created_at,
            )
        except ProjectMemoryRecallError:
            return _RecallOutcome(status="skipped")
        ephemeral = _ephemeral_evidence_refs(
            getattr(result, "ephemeral_context_bundle", None)
        )
        durable = tuple(_evidence_ref(hit) for hit in result.evidence_hits)
        if result.hit_count <= 0 and not ephemeral:
            return _RecallOutcome(
                status="insufficient_evidence",
                request_id=result.request_id,
                result_id=result.result_id,
                progressive_trace=(
                    dict(result.progressive_trace)
                    if isinstance(
                        getattr(result, "progressive_trace", None),
                        Mapping,
                    )
                    else None
                ),
            )
        return _RecallOutcome(
            status="recalled",
            request_id=result.request_id,
            result_id=result.result_id,
            evidence_refs=durable,
            ephemeral_evidence_refs=ephemeral,
            evidence_count=max(int(result.hit_count), len(durable)) + len(ephemeral),
            progressive_trace=(
                dict(result.progressive_trace)
                if isinstance(
                    getattr(result, "progressive_trace", None),
                    Mapping,
                )
                else None
            ),
        )


def serialize_workbench_direct_question(result: WorkbenchDirectQuestionResult) -> dict[str, object]:
    answer_status = "evidence_found" if result.recall_status == "recalled" and result.evidence_count > 0 else "insufficient_evidence"
    evidence_items = [dict(ref) for ref in result.evidence_refs]
    deep_evidence_items = [dict(ref) for ref in result.ephemeral_evidence_refs]
    all_evidence_items = [*evidence_items, *deep_evidence_items]
    source_links = _source_links(tuple(all_evidence_items))
    return {
        "schema_version": "1.0.0",
        "status": result.status,
        "answer": {"status": answer_status, "text": result.answer_preview},
        "question_id": result.question_id,
        "answer_id": result.answer_id,
        "question": result.question,
        "answer_preview": result.answer_preview,
        "qa_mode": result.qa_mode,
        "knowledge_base_write": result.knowledge_base_write,
        "source_created": result.source_created,
        "job_created": result.job_created,
        "library_item_created": result.library_item_created,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
        "recall_status": result.recall_status,
        "recall_request_id": result.recall_request_id,
        "recall_result_id": result.recall_result_id,
        "evidence_refs": [dict(ref) for ref in result.evidence_refs],
        "evidence_count": result.evidence_count,
        "provider_call_performed": result.provider_call_performed,
        "provider_status": result.provider_status,
        "provider_route": result.provider_route,
        "context_fingerprint": result.context_fingerprint,
        "recall_trace": (
            dict(result.recall_trace)
            if result.recall_trace is not None
            else None
        ),
        "replayed": result.replayed,
        "evidence_items": all_evidence_items,
        "source_links": source_links,
        "deep_evidence": {
            "count": result.deep_evidence_count,
            "layers": sorted(
                {
                    str(ref.get("layer"))
                    for ref in result.ephemeral_evidence_refs
                    if ref.get("layer")
                }
            ),
            "ephemeral": True,
            "content_persisted": False,
            "provider_egress_authorized": result.provider_egress_authorized,
        },
        "error": None,
        "privacy": {
            "mode": "provider_evidence_only" if result.provider_call_performed else "local_only",
            "source_path_exposed": False,
            "provider_call_performed": result.provider_call_performed,
            "deep_evidence_ephemeral": True,
            "deep_evidence_persisted": False,
            "provider_egress_authorized": result.provider_egress_authorized,
        },
    }






@dataclass(frozen=True, slots=True)
class _RecallOutcome:
    """Internal view of the optional recall step attached to a direct question."""

    status: str
    request_id: str | None = None
    result_id: str | None = None
    evidence_refs: tuple[Mapping[str, object], ...] = ()
    ephemeral_evidence_refs: tuple[Mapping[str, object], ...] = ()
    evidence_count: int = 0
    progressive_trace: Mapping[str, object] | None = None

    @property
    def answer_evidence_refs(self) -> tuple[Mapping[str, object], ...]:
        return (*self.ephemeral_evidence_refs, *self.evidence_refs)


def _evidence_ref(hit: Mapping[str, object]) -> dict[str, object]:
    """Compact, privacy-safe evidence reference derived from a recall hit."""

    ref: dict[str, object] = {
        "layer": hit.get("layer"),
        "object_id": hit.get("object_id"),
        "explanation": hit.get("explanation"),
        "score": hit.get("score"),
        "quality": hit.get("trust_status"),
    }
    source_refs = hit.get("source_refs")
    if isinstance(source_refs, list):
        ref["source_refs"] = [
            dict(item) for item in source_refs if isinstance(item, Mapping)
        ]
    else:
        ref["source_refs"] = []
    snippet = hit.get("snippet")
    if isinstance(snippet, str):
        ref["snippet"] = snippet
    return ref


def _ephemeral_evidence_refs(bundle: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(bundle, Mapping) or bundle.get("bundle_version") != "progressive-recall-context-v1":
        return ()
    safety = bundle.get("safety")
    if (
        not isinstance(safety, Mapping)
        or safety.get("ephemeral") is not True
        or safety.get("persistence_allowed") is not False
        or safety.get("logging_allowed") is not False
        or safety.get("provider_egress_allowed") is not False
    ):
        return ()
    items = bundle.get("items")
    if not isinstance(items, list):
        return ()
    result: list[Mapping[str, object]] = []
    for item in items[:6]:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        layer = item.get("layer")
        if (
            layer not in {"r2_structured_content", "r3_source_evidence"}
            or not isinstance(content, str)
            or not content.strip()
        ):
            continue
        source_refs = item.get("source_refs")
        result.append(
            {
                "layer": layer,
                "object_id": item.get("object_id"),
                "explanation": (
                    "当前请求内读取的详细资料。"
                    if layer == "r2_structured_content"
                    else "当前请求内核验的原始来源。"
                ),
                "score": 1.0,
                "quality": "user_confirmed",
                "source_refs": [
                    {
                        "source_id": ref.get("source_id"),
                        "locator": ref.get("locator"),
                    }
                    for ref in source_refs
                    if isinstance(ref, Mapping)
                    and isinstance(ref.get("source_id"), str)
                    and isinstance(ref.get("locator"), str)
                ] if isinstance(source_refs, list) else [],
                "snippet": content[:2400],
                "ephemeral": True,
                "excerpt_hash": item.get("excerpt_hash"),
            }
        )
    return tuple(result)


def _source_links(evidence_refs: tuple[Mapping[str, object], ...]) -> list[dict[str, object]]:
    links: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for evidence in evidence_refs:
        refs = evidence.get("source_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            source_id = ref.get("source_id")
            locator = ref.get("locator")
            if not isinstance(source_id, str) or not source_id or not isinstance(locator, str) or not locator:
                continue
            key = (source_id, locator)
            if key in seen:
                continue
            seen.add(key)
            links.append({"source_id": source_id, "locator": locator, "ref": f"crp://default/sources/{source_id}"})
    return links


def _required_question(value: str) -> str:
    if not isinstance(value, str):
        raise WorkbenchDirectQuestionError("question must be a string")
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise WorkbenchDirectQuestionError("question is required")
    if len(normalized) > 4000:
        raise WorkbenchDirectQuestionError("question is too long for direct QA")
    return normalized


def _local_answer(question: str, *, recall_outcome: _RecallOutcome) -> str:
    if recall_outcome.status != "recalled" or not recall_outcome.evidence_refs:
        return (
            "当前没有足够的已发布本地证据回答这个问题。\n\n"
            f"问题：{question}\n\n"
            "本次问答没有写入资料库或长期记忆。请先审核并发布相关项目记忆，再重新提问。"
        )
    lines = ["基于当前已发布的本地证据："]
    for evidence in recall_outcome.evidence_refs[:6]:
        snippet = _bounded_snippet(evidence.get("snippet"))
        if snippet is None:
            continue
        layer = _answer_layer_label(evidence.get("layer"))
        lines.append(f"- {layer}：{snippet}")
    if len(lines) == 1:
        return (
            "当前召回记录缺少可读的证据摘要，无法据此形成可靠回答。\n\n"
            f"问题：{question}\n\n"
            "本次问答没有写入资料库或长期记忆。"
        )
    lines.extend(
        [
            "",
            "以上内容只归纳当前已发布证据，不补充证据之外的事实。",
            "本次问答没有写入资料库或长期记忆。",
        ]
    )
    return "\n".join(lines)[:800]




def _answer_prompt_ref(value: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"id": "builtin-workbench-evidence-answer", "revision": 1, "source": "builtin"}
    return {
        "id": str(value.get("id") or "pt-answer"),
        "revision": value.get("revision") if isinstance(value.get("revision"), int) else 0,
        "source": str(value.get("source") or "developer_studio_active"),
        **({"activation_revision": value["activation_revision"]} if isinstance(value.get("activation_revision"), int) else {}),
        **({"activation_unit": value["activation_unit"]} if isinstance(value.get("activation_unit"), str) else {}),
    }


def _context_fingerprint(
    question: str,
    recall_outcome: _RecallOutcome,
    *,
    provider_egress_authorized: bool,
    provider_route: str,
) -> str:
    canonical = {
        "question": question,
        "recall_status": recall_outcome.status,
        "recall_result_id": recall_outcome.result_id,
        "provider_egress_authorized": provider_egress_authorized,
        "provider_route": provider_route if provider_egress_authorized else "",
        "evidence_refs": [
            _evidence_identity(item)
            for item in recall_outcome.answer_evidence_refs
        ],
    }
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _replay_result(
    record: Mapping[str, object] | None,
    *,
    context_fingerprint: str,
    recall_outcome: _RecallOutcome,
    provider_egress_authorized: bool,
) -> WorkbenchDirectQuestionResult | None:
    if not isinstance(record, Mapping) or record.get("context_fingerprint") != context_fingerprint:
        return None
    evidence_refs = record.get("evidence_refs")
    blocked = record.get("blocked_operations")
    if not isinstance(evidence_refs, list) or not isinstance(blocked, list):
        return None
    try:
        return WorkbenchDirectQuestionResult(
            status=str(record["status"]),
            question_id=str(record["id"]),
            answer_id=str(record["answer_id"]),
            question=str(record["question"]),
            answer_preview=str(record["answer"]),
            qa_mode=str(record["qa_mode"]),
            knowledge_base_write=bool(record["knowledge_base_write"]),
            source_created=bool(record["source_created"]),
            job_created=bool(record["job_created"]),
            library_item_created=bool(record["library_item_created"]),
            memory_publication_state=str(record["memory_publication_state"]),
            blocked_operations=tuple(str(item) for item in blocked),
            recall_status=str(record["recall_status"]),
            recall_request_id=str(record["recall_request_id"]) if record.get("recall_request_id") else None,
            recall_result_id=str(record["recall_result_id"]) if record.get("recall_result_id") else None,
            evidence_refs=tuple(dict(item) for item in evidence_refs if isinstance(item, Mapping)),
            evidence_count=len(evidence_refs) + len(recall_outcome.ephemeral_evidence_refs),
            provider_call_performed=bool(record.get("provider_call_performed")),
            provider_status=str(record.get("provider_status") or "not_configured"),
            provider_route=str(record.get("provider_route") or ""),
            context_fingerprint=context_fingerprint,
            recall_trace=(
                dict(recall_outcome.progressive_trace)
                if isinstance(recall_outcome.progressive_trace, Mapping)
                else None
            ),
            ephemeral_evidence_refs=recall_outcome.ephemeral_evidence_refs,
            deep_evidence_count=len(recall_outcome.ephemeral_evidence_refs),
            provider_egress_authorized=provider_egress_authorized,
            replayed=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _evidence_identity(value: Mapping[str, object]) -> dict[str, object]:
    snippet = value.get("snippet")
    return {
        "layer": value.get("layer"),
        "object_id": value.get("object_id"),
        "excerpt_hash": (
            value.get("excerpt_hash")
            if isinstance(value.get("excerpt_hash"), str)
            else hashlib.sha256(str(snippet or "").encode("utf-8")).hexdigest()
        ),
        "source_refs": [
            {
                "source_id": ref.get("source_id"),
                "locator": ref.get("locator"),
            }
            for ref in value.get("source_refs", [])
            if isinstance(ref, Mapping)
        ] if isinstance(value.get("source_refs"), list) else [],
    }


def _bounded_snippet(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return normalized[:120]


def _answer_layer_label(value: object) -> str:
    return {
        "l3_project_skill": "L3 Project Skill",
        "l4_persona": "L4 Persona",
        "l3_persona": "L3 Persona（兼容旧数据）",
        "l3_series_memory": "L3 Series Memory",
        "l2_scenario": "L2 Scenario",
        "l1_atom": "L1 Atom",
        "l0_source": "L0 Source",
    }.get(value, "本地证据")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
