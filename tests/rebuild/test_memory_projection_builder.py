from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.product_core.memory_projection_builder import (
    MemoryProjectionBuildError,
    build_r0_r1_memory_projection,
    projection_matches_authority,
)
from core.product_core.memory_projection_contract import (
    serialize_memory_retrieval_projection,
)
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "memory_retrieval_projection.schema.json"
)
GENERATED_AT = "2026-07-26T10:00:00+08:00"


def _series(
    *,
    revision: int = 3,
    overview: str = "Chriptmas OS 的长期记忆架构、召回流程和验证状态。",
) -> dict[str, object]:
    return {
        "id": "series-memory-1",
        "series_id": "memory-system",
        "title": "记忆系统",
        "scope": "project",
        "overview": overview,
        "scenario_ids": ["scenario-1", "scenario-2"],
        "source_refs": [
            {
                "source_id": "source-architecture",
                "locator": "section:memory",
                "quote": "SOURCE-QUOTE-CANARY-DO-NOT-COPY",
            }
        ],
        "project_ids": ["project-1"],
        "stale": False,
        "revision": revision,
        "trust_status": "user_confirmed",
        "updated_at": "2026-07-26T09:00:00+08:00",
    }


def _scenarios() -> list[dict[str, object]]:
    return [
        {
            "id": "scenario-2",
            "title": "召回验证",
            "summary": "验证渐进下钻、引用和停止条件。",
            "atom_ids": ["atom-2"],
            "source_refs": [
                {
                    "source_id": "source-tests",
                    "locator": "case:recall",
                }
            ],
            "tags": ["验证", "召回"],
            "series_id": "memory-system",
            "project_id": "project-1",
            "stale": False,
            "revision": 2,
            "trust_status": "trusted",
        },
        {
            "id": "scenario-1",
            "title": "架构设计",
            "summary": "权威知识平面与可重建读取投影分离。",
            "atom_ids": ["atom-1"],
            "source_refs": [
                {
                    "source_id": "source-architecture",
                    "locator": "section:planes",
                }
            ],
            "tags": ["架构", "记忆"],
            "series_id": "memory-system",
            "project_id": "project-1",
            "stale": False,
            "revision": 4,
            "trust_status": "system_generated",
        },
    ]


def _atoms() -> list[dict[str, object]]:
    return [
        {
            "id": "atom-2",
            "source_id": "source-tests",
            "content": "旧投影必须在 authority fingerprint 变化后失效。",
            "atom_type": "decision",
            "tags": ["失效"],
            "confidence": 1.0,
            "source_refs": [
                {
                    "source_id": "source-tests",
                    "locator": "case:fingerprint",
                }
            ],
            "revision": 2,
            "trust_status": "trusted",
        },
        {
            "id": "atom-1",
            "source_id": "source-architecture",
            "content": "读取投影不是业务权威，可以从当前对象重建。",
            "atom_type": "fact",
            "tags": ["双平面"],
            "confidence": 1.0,
            "source_refs": [
                {
                    "source_id": "source-architecture",
                    "locator": "section:authority",
                }
            ],
            "revision": 1,
            "trust_status": "user_confirmed",
        },
    ]


def _skill(
    *,
    revision: int = 5,
    hidden_canary: str = "PROJECT-SKILL-BODY-CANARY-DO-NOT-COPY",
) -> dict[str, object]:
    return {
        "id": "skill-1",
        "project_id": "project-1",
        "name": "记忆架构交付方法",
        "purpose": "按证据、合同和回归逐阶段交付记忆功能。",
        "markdown": f"# 私有规则\n{hidden_canary}",
        "output_rules": [
            {
                "rule_id": "rule-1",
                "rule": hidden_canary,
            }
        ],
        "source_refs": [
            {
                "source_id": "source-architecture",
                "locator": "section:delivery",
            }
        ],
        "revision": revision,
        "status": "active",
        "trust_status": "user_confirmed",
        "conflict": {
            "status": "none",
            "conflict_refs": [],
            "resolution": None,
        },
    }


def _build(
    *,
    series: list[dict[str, object]] | None = None,
    scenarios: list[dict[str, object]] | None = None,
    atoms: list[dict[str, object]] | None = None,
    skills: list[dict[str, object]] | None = None,
    authority_identity: str = "sqlite:structured-records-v1",
    generated_at: str = GENERATED_AT,
):
    return build_r0_r1_memory_projection(
        project_id="project-1",
        authority_identity=authority_identity,
        series_memories=[_series()] if series is None else series,
        scenarios=_scenarios() if scenarios is None else scenarios,
        atoms=_atoms() if atoms is None else atoms,
        project_skills=[_skill()] if skills is None else skills,
        generated_at=generated_at,
    )


def test_builder_creates_bounded_r0_and_r1_projection() -> None:
    projection = _build()
    payload = serialize_memory_retrieval_projection(projection)

    assert projection.status == "ready"
    assert len(projection.r0_items) == 1
    assert len(projection.r1_items) == 1
    assert projection.r0_items[0].series_id == "memory-system"
    assert projection.r0_items[0].description.startswith("Chriptmas OS")
    assert projection.r1_items[0].summary.startswith("Chriptmas OS")
    assert [ref.scenario_id for ref in projection.r1_items[0].scenario_refs] == [
        "scenario-1",
        "scenario-2",
    ]
    assert [ref.atom_id for ref in projection.r1_items[0].atom_refs] == [
        "atom-1",
        "atom-2",
    ]
    assert projection.r1_items[0].skill_refs[0].skill_id == "skill-1"
    assert payload["r0_items"][0]["authority_fingerprint"] == (
        projection.authority_fingerprint
    )
    assert payload["r0_items"][0]["generator_policy_id"] == (
        "deterministic-r0-r1-builder-v1"
    )
    assert payload["r0_items"][0]["status"] == "ready"
    assert payload["r0_items"][0]["failure_code"] is None
    assert payload["r1_items"][0]["derived_from"]
    assert payload["safety"] == {
        "derived": True,
        "business_authority": False,
        "rebuildable": True,
        "source_body_included": False,
        "project_skill_body_included": False,
        "business_writes_allowed": False,
    }


def test_serialized_projection_matches_contract() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert validate_contract_instance(
        SCHEMA_PATH.name,
        schema,
        serialize_memory_retrieval_projection(_build()),
    ) == []
    assert validate_contract_instance(
        SCHEMA_PATH.name,
        schema,
        serialize_memory_retrieval_projection(
            _build(series=[], scenarios=[], atoms=[], skills=[])
        ),
    ) == []


def test_projection_is_deterministic_across_input_order_and_generation_time() -> None:
    first = _build()
    second = _build(
        scenarios=list(reversed(_scenarios())),
        atoms=list(reversed(_atoms())),
        generated_at="2026-07-26T11:00:00+08:00",
    )

    assert first.authority_fingerprint == second.authority_fingerprint
    assert first.r0_items[0].projection_id == second.r0_items[0].projection_id
    assert first.r1_items[0].projection_id == second.r1_items[0].projection_id
    assert first.generated_at != second.generated_at


@pytest.mark.parametrize(
    "mutation",
    [
        "series_revision",
        "series_content",
        "scenario_revision",
        "atom_content",
        "skill_revision",
        "skill_body",
        "delete_scenario",
        "authority_identity",
    ],
)
def test_authority_changes_invalidate_existing_projection(mutation: str) -> None:
    projection = _build()
    series = [_series()]
    scenarios = _scenarios()
    atoms = _atoms()
    skills = [_skill()]
    authority_identity = "sqlite:structured-records-v1"

    if mutation == "series_revision":
        series[0]["revision"] = 4
    elif mutation == "series_content":
        series[0]["overview"] = "更新后的系列总览。"
    elif mutation == "scenario_revision":
        scenarios[0]["revision"] = 3
    elif mutation == "atom_content":
        atoms[0]["content"] = "更新后的权威事实。"
    elif mutation == "skill_revision":
        skills[0]["revision"] = 6
    elif mutation == "skill_body":
        skills[0]["output_rules"] = [{"rule": "更新后的私有规则"}]
    elif mutation == "delete_scenario":
        scenarios.pop()
    elif mutation == "authority_identity":
        authority_identity = "json:object-store-v2"

    assert projection_matches_authority(
        projection,
        project_id="project-1",
        authority_identity=authority_identity,
        series_memories=series,
        scenarios=scenarios,
        atoms=atoms,
        project_skills=skills,
    ) is False


def test_rollback_snapshot_invalidates_newer_projection() -> None:
    current = _build(series=[_series(revision=4, overview="第四版权威总览。")])

    assert projection_matches_authority(
        current,
        project_id="project-1",
        authority_identity="sqlite:structured-records-v1",
        series_memories=[_series(revision=3, overview="回滚后的第三版总览。")],
        scenarios=_scenarios(),
        atoms=_atoms(),
        project_skills=[_skill()],
    ) is False


def test_project_skill_and_source_bodies_are_not_copied_to_projection() -> None:
    skill_canary = "PRIVATE-SKILL-RULE-CANARY-9981"
    payload = serialize_memory_retrieval_projection(
        _build(skills=[_skill(hidden_canary=skill_canary)])
    )
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert skill_canary not in serialized
    assert "SOURCE-QUOTE-CANARY-DO-NOT-COPY" not in serialized
    assert "markdown" not in serialized
    assert "output_rules" not in serialized
    assert '"quote"' not in serialized
    assert payload["project_skill_refs"] == [
        {
            "skill_id": "skill-1",
            "revision": 5,
            "name": "记忆架构交付方法",
            "purpose_preview": "按证据、合同和回归逐阶段交付记忆功能。",
        }
    ]


def test_stale_untrusted_and_conflicted_authority_is_excluded() -> None:
    stale_series = _series()
    stale_series["id"] = "series-memory-stale"
    stale_series["series_id"] = "stale-series"
    stale_series["stale"] = True
    untrusted_scenario = copy.deepcopy(_scenarios()[0])
    untrusted_scenario["id"] = "scenario-untrusted"
    untrusted_scenario["trust_status"] = "imported_unverified"
    conflicted_skill = _skill()
    conflicted_skill["id"] = "skill-conflicted"
    conflicted_skill["conflict"] = {
        "status": "detected",
        "conflict_refs": ["conflict-1"],
        "resolution": None,
    }

    projection = _build(
        series=[_series(), stale_series],
        scenarios=[*_scenarios(), untrusted_scenario],
        skills=[_skill(), conflicted_skill],
    )

    assert [item.series_id for item in projection.r0_items] == ["memory-system"]
    assert all(
        ref.scenario_id != "scenario-untrusted"
        for ref in projection.r1_items[0].scenario_refs
    )
    assert [ref.skill_id for ref in projection.project_skill_refs] == ["skill-1"]


def test_scenario_must_be_linked_by_current_series_authority() -> None:
    orphan = copy.deepcopy(_scenarios()[0])
    orphan["id"] = "scenario-orphan"

    projection = _build(scenarios=[*_scenarios(), orphan])

    assert all(
        ref.scenario_id != "scenario-orphan"
        for ref in projection.r1_items[0].scenario_refs
    )


def test_empty_project_has_explicit_empty_non_authoritative_projection() -> None:
    projection = _build(series=[], scenarios=[], atoms=[], skills=[_skill()])

    assert projection.status == "empty"
    assert projection.derived_from == ()
    assert projection.project_skill_refs == ()
    assert projection.r0_items == ()
    assert projection.r1_items == ()
    assert len(projection.authority_fingerprint) == 64


def test_unreferenced_atom_does_not_invalidate_projection() -> None:
    projection = _build()
    unreferenced = copy.deepcopy(_atoms()[0])
    unreferenced["id"] = "atom-unreferenced"
    unreferenced["content"] = "不影响当前 R0/R1 投影的孤立事实。"

    assert projection_matches_authority(
        projection,
        project_id="project-1",
        authority_identity="sqlite:structured-records-v1",
        series_memories=[_series()],
        scenarios=_scenarios(),
        atoms=[*_atoms(), unreferenced],
        project_skills=[_skill()],
    ) is True


def test_duplicate_authority_ids_fail_closed() -> None:
    with pytest.raises(MemoryProjectionBuildError, match="duplicate scenario id"):
        _build(scenarios=[_scenarios()[0], copy.deepcopy(_scenarios()[0])])


@pytest.mark.parametrize(
    "generated_at",
    [
        "",
        "2026-07-26T10:00:00",
        "not-a-date",
    ],
)
def test_invalid_generation_time_fails_closed(generated_at: str) -> None:
    with pytest.raises(MemoryProjectionBuildError):
        _build(generated_at=generated_at)


def test_non_json_authority_snapshot_fails_closed() -> None:
    series = _series()
    series["unsupported"] = {object()}

    with pytest.raises(
        MemoryProjectionBuildError,
        match="authority snapshot must be canonical JSON",
    ):
        _build(series=[series])
