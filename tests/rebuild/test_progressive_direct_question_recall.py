from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from core.product_core.memory_projection_authority import (
    CurrentMemoryProjectionAuthority,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
    ProjectionReadResult,
)
from core.product_core.progressive_direct_question_recall import (
    ProgressiveDirectQuestionRecall,
)
from core.product_core.progressive_recall_drilldown import (
    AuthorityEvidenceCandidate,
    EvidenceSourceRef,
)
from core.product_core.workbench_direct_question import (
    AnswerWorkbenchDirectQuestion,
    serialize_workbench_direct_question,
)
from core.storage_provider import JsonObjectStore


CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "core-contracts" / "rebuild"
NOW = "2026-07-26T12:00:00+08:00"


@dataclass(frozen=True)
class _LegacyResult:
    project_id: str
    skill_id: str = "skill-default"
    request_id: str = "legacy-request"
    result_id: str = "legacy-result"
    hit_count: int = 1
    evidence_hits: tuple[dict[str, object], ...] = (
        {
            "hit_id": "legacy-hit",
            "layer": "l1_atom",
            "object_id": "legacy-atom",
            "source_refs": [{"source_id": "source-1", "locator": "section:legacy"}],
            "snippet": "旧召回仍可回答。",
            "explanation": "现有召回证据。",
            "score": 0.8,
            "token_estimate": 12,
            "trust_status": "user_confirmed",
        },
    )


class _LegacyRecall:
    def __init__(self, project_id: str = "project-1") -> None:
        self.project_id = project_id
        self.calls: list[tuple[str, str, tuple[str, ...] | None]] = []

    def execute(
        self,
        project_id: str,
        *,
        query: str,
        layers: tuple[str, ...] | None = None,
        created_at: str | None = None,
    ):
        self.calls.append((project_id, query, layers))
        return _LegacyResult(project_id=project_id)


class _Authority:
    def __init__(self, snapshot: MemoryProjectionAuthoritySnapshot) -> None:
        self.snapshot = snapshot
        self.load_calls = 0

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        assert project_id == self.snapshot.project_id
        self.load_calls += 1
        return self.snapshot


class _GenerationAuthority(_Authority):
    def __init__(
        self,
        snapshot: MemoryProjectionAuthoritySnapshot,
        generation_token: str,
    ) -> None:
        super().__init__(snapshot)
        self.authority_identity = snapshot.authority_identity
        self.current_generation_token = generation_token

    def generation_token(self, project_id: str) -> str:
        assert project_id == self.snapshot.project_id
        return self.current_generation_token


class _DeepReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_structured(self, **_kwargs):
        self.calls.append("r2")
        content = "R2-PRIVATE-CANARY 结构化详细资料"
        return (
            AuthorityEvidenceCandidate(
                layer="r2_structured_content",
                object_type="document_block",
                object_id="document-deep#block-1",
                revision_identity="document:document-deep:r1",
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                source_refs=(
                    EvidenceSourceRef(
                        "source-1",
                        "section:architecture",
                        "b" * 64,
                    ),
                ),
                series_id="memory-architecture",
                relevance_score=1.0,
            ),
        )

    def read_source_evidence(self, **_kwargs):
        self.calls.append("r3")
        content = "R3-PRIVATE-CANARY 原始来源正文"
        return (
            AuthorityEvidenceCandidate(
                layer="r3_source_evidence",
                object_type="source_content_read",
                object_id="source-read-deep",
                revision_identity="source-read:current",
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                source_refs=(
                    EvidenceSourceRef(
                        "source-1",
                        "section:architecture",
                        "b" * 64,
                    ),
                ),
                relevance_score=1.0,
            ),
        )


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _snapshot(
    *,
    project_id: str = "project-1",
    revision: int = 1,
) -> MemoryProjectionAuthoritySnapshot:
    return MemoryProjectionAuthoritySnapshot(
        project_id=project_id,
        authority_identity="progressive-memory-authority-v1:test:test",
        series_memories=(
            {
                "id": "series-memory-memory",
                "series_id": "memory-architecture",
                "overview": "记忆系统采用系列路由与分层读取。",
                "scenario_ids": ["scenario-memory"],
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:architecture"}
                ],
                "project_ids": [project_id],
                "stale": False,
                "revision": revision,
                "trust_status": "user_confirmed",
            },
        ),
        scenarios=(
            {
                "id": "scenario-memory",
                "title": "记忆系统架构",
                "summary": "先读取系列概况，再按需下钻。",
                "atom_ids": ["atom-memory"],
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:layers"}
                ],
                "tags": ["记忆系统", "架构"],
                "series_id": "memory-architecture",
                "project_id": project_id,
                "stale": False,
                "revision": 1,
                "trust_status": "trusted",
            },
        ),
        atoms=(
            {
                "id": "atom-memory",
                "source_id": "source-1",
                "content": "R0 与 R1 负责有界召回。",
                "atom_type": "decision",
                "tags": ["记忆系统"],
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:r0-r1"}
                ],
                "revision": 1,
                "trust_status": "trusted",
            },
        ),
        project_skills=(),
    )


def _activate(
    repository: ObjectStoreMemoryProjectionRepository,
    snapshot: MemoryProjectionAuthoritySnapshot,
    *,
    generation_token: str | None = None,
) -> str:
    fingerprint = authority_snapshot_fingerprint(snapshot)
    job_id = f"job-{fingerprint[:16]}"
    repository.begin_rebuild(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        updated_at=NOW,
    )
    artifact_id = repository.stage_projection(snapshot.build(generated_at=NOW))
    repository.activate_staged(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        artifact_id=artifact_id,
        updated_at=NOW,
    )
    if generation_token is not None:
        repository.bind_generation(
            project_id=snapshot.project_id,
            authority_identity=snapshot.authority_identity,
            authority_generation_token=generation_token,
            authority_fingerprint=fingerprint,
        )
    return fingerprint


def test_missing_projection_preserves_legacy_runtime_but_respects_query_depth(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    scheduled: list[tuple[str, str]] = []
    recall = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=ObjectStoreMemoryProjectionRepository(_store(tmp_path)),
        authority=_Authority(snapshot),
        schedule_rebuild=lambda value, fingerprint: (
            scheduled.append((value.project_id, fingerprint))
            or "projection-job-1"
        ),
    )

    result = recall.execute(
        "project-1",
        query="记忆系统架构是什么？",
        created_at=NOW,
    )

    assert result.result_id == "legacy-result"
    assert [hit["object_id"] for hit in result.evidence_hits] == ["legacy-atom"]
    assert result.hit_count == 1
    assert result.progressive_trace["fallback"]["used"] is True
    assert result.progressive_trace["rebuild"] == {
        "scheduled": True,
        "job_id": "projection-job-1",
    }
    assert len(scheduled) == 1


def test_fresh_projection_routes_r1_before_legacy_evidence(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    fingerprint = _activate(repository, snapshot)
    recall = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=_Authority(snapshot),
    )

    result = recall.execute(
        "project-1",
        query="记忆系统架构",
        created_at=NOW,
    )

    assert result.result_id.startswith("progressive-recall-result-")
    assert result.evidence_hits[0]["object_id"] == "series-memory-memory"
    assert result.evidence_hits[0]["snippet"] == "记忆系统采用系列路由与分层读取。"
    assert len(result.evidence_hits) == 1
    assert all(hit["object_id"] != "legacy-atom" for hit in result.evidence_hits)
    assert result.progressive_trace["authority_fingerprint"] == fingerprint
    assert result.progressive_trace["layers_read"] == [
        "r0_series_router",
        "r1_series_digest",
    ]
    assert result.progressive_trace["memory_layers"] == [
        {
            "layer": "L4",
            "status": "unavailable",
            "reason": "eligible_evidence_not_found",
            "hit_count": 0,
            "token_estimate": 0,
        },
        {
            "layer": "L3",
            "status": "selected",
            "reason": "project_scope_context",
            "hit_count": 1,
            "token_estimate": len("记忆系统采用系列路由与分层读取。") // 4,
        },
        {
            "layer": "L2",
            "status": "skipped",
            "reason": "query_depth_not_requested",
            "hit_count": 0,
            "token_estimate": 0,
        },
        {
            "layer": "L1",
            "status": "skipped",
            "reason": "query_depth_not_requested",
            "hit_count": 0,
            "token_estimate": 0,
        },
        {
            "layer": "L0",
            "status": "skipped",
            "reason": "query_depth_not_requested",
            "hit_count": 0,
            "token_estimate": 0,
        },
    ]
    performance = result.progressive_trace["performance"]
    assert performance["total_ms"] >= 0
    assert performance["legacy_recall_ms"] >= 0
    assert [stage["stage"] for stage in performance["stages"]] == [
        "r0_series_router",
        "r1_series_digest",
        "r2_structured_content",
        "r3_source_evidence",
    ]
    assert [stage["status"] for stage in performance["stages"]] == [
        "used",
        "used",
        "not_requested",
        "not_requested",
    ]
    assert result.progressive_trace["fallback"]["used"] is False


def test_generation_binding_reads_projection_without_building_authority_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    generation_token = "c" * 64
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    fingerprint = _activate(
        repository,
        snapshot,
        generation_token=generation_token,
    )
    authority = _GenerationAuthority(snapshot, generation_token)

    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=authority,
    ).execute("project-1", query="记忆系统架构", created_at=NOW)

    assert authority.load_calls == 0
    assert result.progressive_trace["authority_fingerprint"] == fingerprint
    assert result.progressive_trace["fallback"]["used"] is False


def test_generation_change_falls_back_to_snapshot_and_schedules_rebuild(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot, generation_token="c" * 64)
    authority = _GenerationAuthority(snapshot, "d" * 64)
    scheduled: list[str] = []

    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=authority,
        schedule_rebuild=lambda _snapshot, fingerprint: (
            scheduled.append(fingerprint) or "projection-job-generation-change"
        ),
    ).execute("project-1", query="记忆系统架构", created_at=NOW)

    assert authority.load_calls == 1
    assert result.progressive_trace["fallback"]["used"] is False
    assert scheduled == []


def test_standard_question_does_not_read_r2_or_r3(tmp_path: Path) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    reader = _DeepReader()
    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=_Authority(snapshot),
        authority_reader=reader,
    ).execute("project-1", query="记忆系统架构", created_at=NOW)

    assert reader.calls == []
    assert result.ephemeral_context_bundle is None
    assert result.progressive_trace["layers_read"] == [
        "r0_series_router",
        "r1_series_digest",
    ]
    assert result.progressive_trace["drilldown"]["status"] == "not_requested"
    assert [hit["layer"] for hit in result.evidence_hits] == ["l3_series_memory"]


def test_fresh_projection_orders_l4_then_l3_and_drops_unrequested_deep_hits(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    atom = dict(_LegacyResult(project_id="project-1").evidence_hits[0])
    legacy = _LegacyRecall()
    legacy.execute = lambda *_args, **_kwargs: _LegacyResult(
        project_id="project-1",
        evidence_hits=(
            {
                **atom,
                "hit_id": "persona-hit",
                "layer": "l4_persona",
                "object_id": "persona-global",
                "snippet": "先给结论。",
            },
            {
                **atom,
                "hit_id": "skill-hit",
                "layer": "l3_project_skill",
                "object_id": "skill-project-1",
                "snippet": "按项目证据回答。",
            },
            atom,
        ),
        hit_count=3,
    )

    result = ProgressiveDirectQuestionRecall(
        legacy_recall=legacy,
        projections=repository,
        authority=_Authority(snapshot),
    ).execute("project-1", query="记忆系统架构", created_at=NOW)

    assert [hit["layer"] for hit in result.evidence_hits] == [
        "l4_persona",
        "l3_series_memory",
    ]
    assert all(hit["object_id"] != "legacy-atom" for hit in result.evidence_hits)
    trace = result.progressive_trace["memory_layers"]
    assert [item["layer"] for item in trace] == ["L4", "L3", "L2", "L1", "L0"]
    assert [item["status"] for item in trace] == [
        "selected",
        "selected",
        "skipped",
        "skipped",
        "skipped",
    ]


def test_fresh_projection_reads_only_query_planned_authority_lanes(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    expected = {
        "记忆系统架构整体概况": ("l4_persona",),
        "记忆系统架构版本是什么": ("l4_persona", "l1_atom"),
        "记忆系统架构 按以前工作方法": (
            "l4_persona",
            "l3_project_skill",
        ),
        "记忆系统架构 完整技术方案": (
            "l4_persona",
            "l3_project_skill",
        ),
        "请核对记忆系统架构原文出处": ("l4_persona",),
    }

    for query, expected_layers in expected.items():
        legacy = _LegacyRecall()
        ProgressiveDirectQuestionRecall(
            legacy_recall=legacy,
            projections=repository,
            authority=_Authority(snapshot),
            authority_reader=_DeepReader(),
        ).execute("project-1", query=query, created_at=NOW)

        assert legacy.calls == [("project-1", query, expected_layers)]


def test_fact_lookup_keeps_only_relevant_legacy_atom_after_l3(tmp_path: Path) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    legacy = _LegacyRecall()
    atom_template = dict(_LegacyResult(project_id="project-1").evidence_hits[0])
    legacy_result = _LegacyResult(
        project_id="project-1",
        evidence_hits=(
            {
                **atom_template,
                "object_id": "atom-version",
                "snippet": "当前版本是 2.4.1。",
            },
            {
                **atom_template,
                "object_id": "atom-weather",
                "snippet": "今天是晴天。",
            },
        ),
        hit_count=2,
    )
    legacy.execute = lambda *_args, **_kwargs: legacy_result

    result = ProgressiveDirectQuestionRecall(
        legacy_recall=legacy,
        projections=repository,
        authority=_Authority(snapshot),
    ).execute("project-1", query="记忆系统架构当前具体版本是多少？", created_at=NOW)

    assert [hit["layer"] for hit in result.evidence_hits] == [
        "l3_series_memory",
        "l1_atom",
        "l1_atom",
    ]
    assert result.evidence_hits[1]["object_id"] == "atom-version"
    trace = {item["layer"]: item for item in result.progressive_trace["memory_layers"]}
    assert trace["L1"]["status"] == "selected"
    assert trace["L2"]["status"] == "unavailable"


def test_deep_synthesis_reads_r2_only_and_keeps_content_out_of_trace(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    reader = _DeepReader()
    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=_Authority(snapshot),
        authority_reader=reader,
    ).execute("project-1", query="记忆系统架构详细分析", created_at=NOW)

    assert reader.calls == ["r2"]
    assert result.ephemeral_context_bundle is not None
    assert [
        item["layer"] for item in result.ephemeral_context_bundle["items"]
    ] == ["r2_structured_content"]
    assert result.progressive_trace["layers_read"] == [
        "r0_series_router",
        "r1_series_digest",
        "r2_structured_content",
    ]
    stages = result.progressive_trace["performance"]["stages"]
    assert [stage["status"] for stage in stages] == [
        "used",
        "used",
        "used",
        "not_requested",
    ]
    trace_body = json.dumps(
        result.progressive_trace,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "R2-PRIVATE-CANARY" not in trace_body
    assert "section:architecture" not in trace_body


def test_source_verification_reads_r2_and_r3(tmp_path: Path) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    reader = _DeepReader()
    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=_Authority(snapshot),
        authority_reader=reader,
    ).execute("project-1", query="记忆系统架构原文出处", created_at=NOW)

    assert reader.calls == ["r2", "r3"]
    assert result.ephemeral_context_bundle is not None
    assert [
        item["layer"] for item in result.ephemeral_context_bundle["items"]
    ] == ["r2_structured_content", "r3_source_evidence"]
    assert result.progressive_trace["layers_read"] == [
        "r0_series_router",
        "r1_series_digest",
        "r2_structured_content",
        "r3_source_evidence",
    ]
    assert result.progressive_trace["drilldown"]["status"] == "completed"
    memory_trace = {
        item["layer"]: item
        for item in result.progressive_trace["memory_layers"]
    }
    assert memory_trace["L2"]["status"] == "selected"
    assert memory_trace["L0"]["status"] == "selected"
    assert memory_trace["L1"]["status"] == "skipped"


def test_changed_authority_never_reads_stale_projection(
    tmp_path: Path,
) -> None:
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, _snapshot(revision=1))
    scheduled: list[str] = []
    recall = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=_Authority(_snapshot(revision=2)),
        schedule_rebuild=lambda _snapshot, fingerprint: (
            scheduled.append(fingerprint) or "projection-job-2"
        ),
    )

    result = recall.execute("project-1", query="记忆系统架构", created_at=NOW)

    assert result.result_id == "legacy-result"
    assert result.progressive_trace["projection"]["read_status"] == "stale"
    assert result.progressive_trace["fallback"]["used"] is True
    assert scheduled


class _CorruptProjectionRepository:
    def load_current(self, **_kwargs):
        return ProjectionReadResult(
            status="corrupt",
            fallback_to_authority=True,
            reason_code="projection_artifact_corrupt",
            projection=None,
            manifest={"status": "ready"},
        )


def test_corrupt_immutable_artifact_falls_back_without_false_rebuild_claim() -> None:
    scheduled: list[str] = []
    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=_CorruptProjectionRepository(),
        authority=_Authority(_snapshot()),
        schedule_rebuild=lambda _snapshot, fingerprint: (
            scheduled.append(fingerprint) or "unexpected-job"
        ),
    ).execute("project-1", query="记忆系统架构", created_at=NOW)

    assert result.result_id == "legacy-result"
    assert result.progressive_trace["projection"]["read_status"] == "corrupt"
    assert result.progressive_trace["rebuild"]["scheduled"] is False
    assert scheduled == []


def test_trace_contract_excludes_query_content_and_source_refs(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    _activate(repository, snapshot)
    canary = "PRIVATE-QUERY-CANARY-7E2D"
    result = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=repository,
        authority=_Authority(snapshot),
    ).execute("project-1", query=f"记忆系统架构 {canary}", created_at=NOW)
    serialized = json.dumps(
        result.progressive_trace,
        ensure_ascii=False,
        sort_keys=True,
    )
    schema = json.loads(
        (CONTRACT_ROOT / "progressive_direct_question_trace.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert Draft202012Validator(schema).is_valid(result.progressive_trace)
    assert canary not in serialized
    assert "旧召回仍可回答" not in serialized
    assert "section:architecture" not in serialized
    assert '"source_refs":' not in serialized


class _Memory:
    def __init__(self, snapshot: MemoryProjectionAuthoritySnapshot) -> None:
        self.snapshot = snapshot

    def list(self, layer: str):
        return {
            "series_memory": self.snapshot.series_memories,
            "scenario": self.snapshot.scenarios,
            "atom": self.snapshot.atoms,
        }[layer]

    def generation_token(self):
        return "a" * 64


class _Skills:
    def generation_token(self):
        return "b" * 64

    def list_by_project(self, project_id: str):
        return (
            {
                "id": f"skill-{project_id}",
                "project_id": project_id,
                "name": "项目方法",
                "purpose": "只属于当前项目。",
                "status": "active",
                "trust_status": "user_confirmed",
                "conflict": {"status": "none"},
                "revision": 1,
            },
        )


def test_current_authority_combines_resolved_memory_and_skill_identities() -> None:
    authority = CurrentMemoryProjectionAuthority(
        memory=_Memory(_snapshot()),
        project_skills=_Skills(),
        memory_authority_identity="sqlite:memory",
        project_skill_authority_identity="sqlite:skills",
    )

    snapshot = authority.load("project-1")

    assert snapshot.authority_identity == (
        "progressive-memory-authority-v1:sqlite:memory:sqlite:skills"
    )
    assert snapshot.project_id == "project-1"
    assert snapshot.project_skills[0]["id"] == "skill-project-1"
    assert snapshot.authority_generation_token == authority.generation_token(
        "project-1"
    )


def test_current_authority_disables_fast_path_for_invalid_store_token() -> None:
    memory = _Memory(_snapshot())
    memory.generation_token = lambda: None  # type: ignore[method-assign]
    authority = CurrentMemoryProjectionAuthority(
        memory=memory,
        project_skills=_Skills(),
        memory_authority_identity="sqlite:memory",
        project_skill_authority_identity="sqlite:skills",
    )

    assert authority.generation_token("project-1") is None
    assert authority.load("project-1").authority_generation_token is None


class _Provider:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def complete_json(self, *, system_prompt: str, user_payload):
        self.payloads.append(dict(user_payload))
        return {
            "status": "evidence_answer",
            "answer": "当前记忆系统采用分层读取。",
            "cited_evidence_ids": ["evidence-1"],
            "limitations": [],
            "next_steps": [],
        }


def test_provider_receives_only_bounded_r1_evidence_not_r2_r3_bundle(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    store = _store(tmp_path)
    projections = ObjectStoreMemoryProjectionRepository(store)
    _activate(projections, snapshot)
    provider = _Provider()
    recall = ProgressiveDirectQuestionRecall(
        legacy_recall=_LegacyRecall(),
        projections=projections,
        authority=_Authority(snapshot),
    )
    result = AnswerWorkbenchDirectQuestion(
        store,
        recall=recall,
        recall_project_id="project-1",
        answer_provider=provider,
        provider_route="search.answer:test",
        provider_egress_authorized=True,
    ).execute(question="记忆系统架构")

    assert result.provider_status == "succeeded"
    assert provider.payloads[0]["untrusted_published_evidence"][0]["snippet"] == (
        "记忆系统采用系列路由与分层读取。"
    )
    serialized = json.dumps(provider.payloads[0], ensure_ascii=False)
    assert "progressive-context-" not in serialized
    assert "r2_structured_content" not in serialized
    assert "r3_source_evidence" not in serialized


def test_authorized_provider_receives_bounded_r2_and_r3_for_source_verification(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    store = _store(tmp_path)
    projections = ObjectStoreMemoryProjectionRepository(store)
    _activate(projections, snapshot)
    provider = _Provider()
    result = AnswerWorkbenchDirectQuestion(
        store,
        recall=ProgressiveDirectQuestionRecall(
            legacy_recall=_LegacyRecall(),
            projections=projections,
            authority=_Authority(snapshot),
            authority_reader=_DeepReader(),
        ),
        recall_project_id="project-1",
        answer_provider=provider,
        provider_route="search.answer:test",
        provider_egress_authorized=True,
    ).execute(question="记忆系统架构原文出处")

    request_body = json.dumps(provider.payloads[0], ensure_ascii=False)
    response = serialize_workbench_direct_question(result)
    persisted = json.dumps(
        store.read("workbench_direct_questions", result.question_id),
        ensure_ascii=False,
        sort_keys=True,
    )

    assert result.provider_status == "succeeded"
    assert "R2-PRIVATE-CANARY" in request_body
    assert "R3-PRIVATE-CANARY" in request_body
    assert response["deep_evidence"]["count"] == 2
    assert "R2-PRIVATE-CANARY" not in persisted
    assert "R3-PRIVATE-CANARY" not in persisted
