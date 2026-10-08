from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _acceptance_doc() -> str:
    return (ROOT / "docs" / "product" / "08-acceptance-criteria.md").read_text(
        encoding="utf-8"
    )


def test_product_acceptance_matrix_reflects_enabled_content_pipeline() -> None:
    doc = _acceptance_doc()

    assert "当前验收口径 2026-07-01" in doc
    assert "用户可见验收矩阵" in doc
    for token in [
        "文本正文读取",
        "PDF / Word 文档正文读取",
        "图片 OCR",
        "音频转写",
        "视频帧提取",
        "Provider 健康状态",
        "Source 输出到 Memory Candidate",
        "长期 Memory 发布和 rollback",
        "Desktop 文件选择",
        "资料库完整回看",
    ]:
        assert token in doc

    assert "P2C-22" in doc
    assert "用户确认 DOCX 样本后的真实文档正文读取验证已完成" in doc
    assert "PDF 用户样本未单独验证" in doc
    assert "不运行真实 Provider、OCR、转写、视频帧提取、Memory 发布" not in doc


def test_product_acceptance_matrix_keeps_safety_boundaries() -> None:
    doc = _acceptance_doc()

    for token in [
        "authorized_file_refs",
        "run endpoint 不接收 UI 传入命令",
        "不自动发布长期 Memory",
        "rollback 不恢复 staging atom",
        "不声明真实 Windows 文件选择对话框人工点击已完成",
    ]:
        assert token in doc
