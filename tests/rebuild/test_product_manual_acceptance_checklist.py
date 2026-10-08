from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _checklist_doc() -> str:
    return (ROOT / "docs" / "product" / "11-manual-acceptance-checklist.md").read_text(
        encoding="utf-8"
    )


def test_manual_acceptance_checklist_covers_product_entrypoints() -> None:
    doc = _checklist_doc()

    for token in [
        "设置页 Provider 状态",
        "资料库 Source 详情",
        "Source 输出到 Memory Candidate",
        "Candidate Review",
        "长期 Memory 发布",
        "Rollback",
        "文件选择和授权边界",
        "状态：待执行",
    ]:
        assert token in doc


def test_manual_acceptance_checklist_keeps_action_boundaries() -> None:
    doc = _checklist_doc()

    for token in [
        "选择文件不是授权",
        "授权不是读取正文",
        "读取或 Provider 运行不是 Memory 发布",
        "run endpoint 不接收 UI 传入命令",
        "review 不是发布",
        "长期发布必须由用户动作触发",
        "rollback 不恢复 staging atom",
        "不代表人工验收已经完成",
    ]:
        assert token in doc
