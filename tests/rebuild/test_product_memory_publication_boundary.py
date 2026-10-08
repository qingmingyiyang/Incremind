from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _boundary_doc() -> str:
    return (ROOT / "docs" / "product" / "12-memory-publication-boundary.md").read_text(
        encoding="utf-8"
    )


def test_memory_publication_boundary_defines_layers_and_contracts() -> None:
    doc = _boundary_doc()

    for token in [
        "Atom",
        "Scenario",
        "Series Memory",
        "Project / Skill Memory",
        "memory_candidates",
        "staging_scenarios",
        "staging_series_memory",
        "staging_project_skills",
        "memory_scenarios",
        "memory_series_memory",
        "project_skills",
        "memory_publications",
        "memory_transitions",
    ]:
        assert token in doc


def test_memory_publication_boundary_prevents_atom_only_overclaim() -> None:
    doc = _boundary_doc()

    for token in [
        "不能用 Atom publication 的测试或文案冒充多层 Memory 完成",
        "Atom publication 测试不能作为 scenario / series / project skill publication 的通过证据",
        "Scenario / Series / Project Skill Memory publication runtime 已实现",
        "Scenario / Series / Project Skill Memory rollback runtime 已实现",
        "四层外部 AI 候选提示词契约已有聚焦测试",
    ]:
        assert token in doc


def test_memory_publication_boundary_preserves_review_publish_rollback_sequence() -> None:
    doc = _boundary_doc()

    for token in [
        "review 动作只把 candidate 提升为 staging 对象",
        "每个层级都需要二次确认发布",
        "rollback 必须按 publication id 执行",
        "重复 rollback 必须失败",
        "rollback 默认不恢复 staging 对象",
        "auto_promote_allowed",
    ]:
        assert token in doc
