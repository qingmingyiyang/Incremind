from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _audit_doc() -> str:
    return (ROOT / "docs" / "product" / "13-old-replay-selective-reuse-audit.md").read_text(
        encoding="utf-8"
    )


def test_old_replay_reuse_audit_covers_reusable_asset_classes() -> None:
    doc = _audit_doc()

    for token in [
        "Source preflight task",
        "adapter skipped contract",
        "activity trace / provenance",
        "旧左侧导航",
        "旧视频识别 / 视频帧提取工作流",
        "旧音频转写 / 会议录音整理链路",
        "摘要生成和内容结构化流程",
        "旧 contract tests",
        "B 站链接解析与下载",
        "视频音频抽取",
        "本地 ASR 转文字",
        "视频总结与结构化 JSON",
        "transcript / summary 检索",
    ]:
        assert token in doc


def test_old_replay_reuse_audit_keeps_forbidden_boundaries() -> None:
    doc = _audit_doc()

    for token in [
        "不复制旧 Node server",
        "不恢复 B 站、日报、报告中心",
        "不复制旧用户数据",
        "不运行旧 workflow",
        "不提交模型 key",
        "不得默认运行 Provider、OCR、ASR、视频处理、批处理或 Memory 发布",
        "未经过 Memory Candidate review 的长期 Memory 写入",
    ]:
        assert token in doc


def test_old_replay_reuse_audit_sets_next_p3_step() -> None:
    doc = _audit_doc()

    assert "P3-1 Provider Doctor 和旧 adapter 诊断口径对齐" in doc
    assert "每类 Provider 都有 ready、disabled、missing、misconfigured、failed 状态" in doc
    assert "不读取用户文件、不运行成本型服务、不发布 Memory" in doc


def test_old_replay_reuse_audit_prioritizes_full_video_workflow_reuse() -> None:
    doc = _audit_doc()

    for token in [
        "YtDlpBilibiliResolver",
        "BilibiliDownloader",
        "FfmpegMediaProcessor",
        "faster-whisper",
        "P3-2 视频链接解析与下载 adapter",
        "P3-3 视频音频抽取 adapter",
        "P3-4 转文字 adapter",
        "P3-5 总结与结构化输出 contract",
        "视频链接解析",
        "提取音频",
        "ASR 转文字",
        "总结与结构化输出",
        "Memory Candidate",
    ]:
        assert token in doc
