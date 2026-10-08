from __future__ import annotations

from backend.shared.llm.prompt_contracts import (
    prompt_messages,
    redact_private_prompt_text,
    redact_private_prompt_value,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)
from core.product_core.transcript_ad_filter import TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE


VIDEO_MINDMAP_PROMPT_VERSION = "video-summary-mindmap-v4"
VIDEO_MINDMAP_TIMEOUT_SECONDS = 90

MINDMAP_PROMPT_TEMPLATE = (
    "请基于以下视频概况信息，生成一个适合前端交互展示的思维导图 JSON。\n"
    "要求：\n"
    "1. 只输出 JSON，不要输出额外解释。\n"
    "2. 不要编造 summary 中不存在的信息。\n"
    "3. 导图节点必须是树结构，且每个节点都包含 id、title、summary、start_seconds、end_seconds、children。\n"
    "4. 请按知识结构组织节点，而不是机械复述章节目录；可以参考章节，但不要被时间顺序束缚。\n"
    "5. 层级深度由内容复杂度决定：简单主题可以较浅，复杂主题可以自然展开到更深层，但每一层都必须有信息价值。\n"
    "6. 节点标题尽量简洁，优先使用关键词或短语，不要把整句摘要直接当标题。\n"
    "7. 时间范围必须落在视频时长内。\n\n"
    "视频标题：{title}\n"
    "视频时长秒数：{duration_seconds}\n"
    "概况 JSON：\n"
    "{summary_json}\n"
)


def build_mindmap_messages(
    *,
    title: str,
    duration_seconds: float,
    summary_data: dict[str, object],
) -> list[dict[str, str]]:
    return prompt_messages(
        system=(
            "你是视频知识结构设计助手。你的职责是把一份已生成的视频结构化概况转换为前端可展示的思维导图 JSON 草稿；"
            "你没有修改概况、发布、调用工具、联网搜索或更新长期记忆的权限。\n"
            + untrusted_envelope_clause(
                fields="标题和 summary_data",
                content_noun="标题或概况内容",
            )
            + "\n"
            "summary_data 是唯一内容证据，不得补造；若缺少信息，使用更浅的树或空 children，"
            "不得凑节点。"
            + TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE
            + redaction_boundary_clause()
            + "\n"
            "只输出符合 response schema 的单个根节点 JSON，不要代码围栏或解释。每个节点必须包含 id、title、summary、"
            "start_seconds、end_seconds、children；id 在整棵树中唯一，标题简洁，层级按知识关系组织。时间必须来自概况证据并"
            "限制在 0 到 video_duration_seconds 之间；无可靠时间时使用父节点范围。该结果是未发布的可视化草稿。"
        ),
        payload={
            "schema_version": "1.0",
            "prompt_version": VIDEO_MINDMAP_PROMPT_VERSION,
            "data_class": "untrusted_generated_video_summary",
            "video_title": redact_private_prompt_text(title),
            "video_duration_seconds": max(0.0, duration_seconds),
            "summary_data": redact_private_prompt_value(summary_data),
        },
    )
