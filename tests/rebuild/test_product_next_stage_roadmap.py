from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _roadmap_doc() -> str:
    return (ROOT / "docs" / "product" / "10-next-stage-roadmap.md").read_text(
        encoding="utf-8"
    )


def test_next_stage_roadmap_covers_acceptance_matrix_limits() -> None:
    doc = _roadmap_doc()

    for token in [
        "P2C-22",
        "P2C-28",
        "P2C-29",
        "P2C-30",
        "P2C-31",
        "P2C-32",
        "用户确认 PDF / DOCX 样本文档验证",
        "产品手动验收清单",
        "真实 Provider 环境探测",
        "Scenario / Series Memory 发布边界",
        "真实 Windows 文件选择人工验收",
    ]:
        assert token in doc


def test_next_stage_roadmap_preserves_safety_boundaries() -> None:
    doc = _roadmap_doc()

    for token in [
        "不扫描用户目录",
        "不自动挑选文件",
        "不读取用户资料",
        "不自动运行成本型服务",
        "不自动发布 Memory",
        "授权、review、二次确认和 rollback 记录",
    ]:
        assert token in doc
