from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import build_four_layer_memory_candidate_import
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import (
    FourLayerMemoryCandidateImportError,
    ImportFourLayerMemoryCandidatesFromProviderOutput,
    ReadSourceTextContent,
    serialize_four_layer_memory_candidate_import,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _content_read_source(object_store: JsonObjectStore) -> MappingFixture:
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="text",
            title="Product design memory source",
            content="产品设计文档说明：资料库需要把正文读取、视频处理和四层记忆候选串成显式确认流程。",
        )
    )
    ReadSourceTextContent(object_store).execute(source_id=str(source["id"]))
    return MappingFixture(source_id=str(source["id"]))


class MappingFixture:
    def __init__(self, *, source_id: str) -> None:
        self.source_id = source_id
        self.content_read_id = f"content-read-{source_id}"
        self.source_ref = {
            "source_id": source_id,
            "locator": "source:content",
            "quote": "产品设计文档说明：资料库需要把正文读取、视频处理和四层记忆候选串成显式确认流程。",
        }


def _provider_output(fixture: MappingFixture) -> dict[str, object]:
    return {
        "candidates": [
            _candidate(
                fixture,
                target_layer="atom",
                proposed_content="资料库必须保留 Source output 到 Memory Candidate 的显式确认流程。",
            ),
            _candidate(
                fixture,
                target_layer="scenario",
                proposed_content="当资料已完成正文读取或媒体处理时，先生成候选，再由用户确认是否发布。",
            ),
            _candidate(
                fixture,
                target_layer="series_memory",
                proposed_content="围绕产品设计文档的长期方向是逐步把真实内容处理链路接入四层记忆。",
            ),
            _candidate(
                fixture,
                target_layer="project_skill",
                proposed_content="项目技能：外部 Provider 只能返回待审候选 JSON，本地导入器负责验证和写入。",
            ),
        ],
        "insufficient_evidence": ["没有足够跨时间证据时，Series Memory 需要用户后续复核。"],
        "provider_boundary": {"provider_must_not": ["publish_memory", "write_long_term_memory"]},
    }


def _candidate(
    fixture: MappingFixture,
    *,
    target_layer: str,
    proposed_content: str,
) -> dict[str, object]:
    return {
        "target_layer": target_layer,
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": proposed_content,
        "source_refs": [fixture.source_ref],
        "evidence_refs": [fixture.source_ref],
        "review_prompt": f"请确认 {target_layer} 候选是否值得进入长期记忆。",
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
        },
    }


def test_provider_output_imports_four_layer_candidates_without_publication(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    fixture = _content_read_source(object_store)
    use_case = ImportFourLayerMemoryCandidatesFromProviderOutput(
        object_store,
        now="2026-07-02T10:30:00+08:00",
    )

    result = use_case.execute_from_content_read(
        source_id=fixture.source_id,
        project_id="project-alpha",
        content_read_id=fixture.content_read_id,
        provider_output=_provider_output(fixture),
    )
    payload = serialize_four_layer_memory_candidate_import(result)
    candidates = [
        ObjectStoreMemoryCandidateRepository(object_store).get(candidate_id)
        for candidate_id in result.candidate_ids
    ]

    assert payload["status"] == "candidates_imported"
    assert payload["candidate_count"] == 4
    assert payload["memory_publication_state"] == "candidates_created_not_published"
    assert payload["insufficient_evidence"] == ["没有足够跨时间证据时，Series Memory 需要用户后续复核。"]
    assert [candidate["target_layer"] for candidate in candidates if candidate is not None] == [
        "atom",
        "scenario",
        "series_memory",
        "project_skill",
    ]
    for candidate in candidates:
        assert candidate is not None
        assert candidate["status"] == "pending_review"
        assert candidate["review"]["requires_user_confirmation"] is True
        assert candidate["review"]["auto_promote_allowed"] is False
        assert candidate["source_refs"] == [fixture.source_ref]
        assert candidate["provenance"]["source_content_read_id"] == fixture.content_read_id
        assert validate_contract_instance(
            "memory_candidate.schema.json",
            _schema("memory_candidate.schema.json"),
            candidate,
        ) == []
    events = object_store.list("activity_events")
    assert any(event["type"] == "four_layer_memory_candidates_imported" for event in events)
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "staging_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_scenarios").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_series_memory").exists()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "project_skills").exists()


def test_provider_output_import_rejects_secrets_paths_and_untrusted_refs(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    fixture = _content_read_source(object_store)
    use_case = ImportFourLayerMemoryCandidatesFromProviderOutput(object_store)

    with pytest.raises(FourLayerMemoryCandidateImportError, match="forbidden secret or local path"):
        use_case.execute_from_content_read(
            source_id=fixture.source_id,
            project_id="project-alpha",
            provider_output={
                "candidates": [
                    {
                        **_candidate(
                            fixture,
                            target_layer="atom",
                            proposed_content="这条候选包含本地绝对路径 C:\\Users\\Secret\\cookie.txt",
                        )
                    }
                ],
                "insufficient_evidence": [],
                "provider_boundary": {},
            },
        )

    with pytest.raises(FourLayerMemoryCandidateImportError, match="authorized evidence"):
        use_case.execute_from_content_read(
            source_id=fixture.source_id,
            project_id="project-alpha",
            provider_output={
                "candidates": [
                    {
                        **_candidate(
                            fixture,
                            target_layer="scenario",
                            proposed_content="候选不能引用未授权证据。",
                        ),
                        "source_refs": [
                            {
                                "source_id": "other-source",
                                "locator": "source:content",
                                "quote": "未授权证据",
                            }
                        ],
                    }
                ],
                "insufficient_evidence": [],
                "provider_boundary": {},
            },
        )


def test_four_layer_candidate_import_composition_uses_temp_storage(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    fixture = _content_read_source(object_store)
    use_case = build_four_layer_memory_candidate_import(ROOT, runtime_root=tmp_path)

    result = use_case.execute_from_content_read(
        source_id=fixture.source_id,
        project_id="project-alpha",
        provider_output={
            "candidates": [_candidate(fixture, target_layer="atom", proposed_content="组合入口候选。")],
            "insufficient_evidence": [],
            "provider_boundary": {},
        },
    )

    assert result.candidate_count == 1
    assert (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_candidates").exists()
    assert not (tmp_path / "library").exists()
