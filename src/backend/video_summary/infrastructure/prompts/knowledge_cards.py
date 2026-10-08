from __future__ import annotations

from backend.shared.llm.prompt_contracts import (
    prompt_messages,
    redact_private_prompt_text,
    redact_private_prompt_value,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)
from core.product_core.transcript_ad_filter import TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE


VIDEO_KNOWLEDGE_CARD_PROMPT_VERSION = "video-summary-knowledge-cards-v4"
VIDEO_KNOWLEDGE_CARD_TIMEOUT_SECONDS = 90


def build_knowledge_card_messages(*, title: str, summary_data: dict[str, object]) -> list[dict[str, str]]:
    return prompt_messages(
        system=(
            "你是视频知识卡片整理助手。你的职责是从当前结构化视频概况中筛选真正值得复习的概念、方法和反常识洞见，"
            "生成可审核的 JSON 草稿；你没有发布卡片、调用工具、修改视频、写笔记或更新长期记忆的权限。\n"
            + untrusted_envelope_clause(
                fields="标题和 summary_data",
                content_noun="标题或概况内容",
            )
            + "\n"
            "只能使用 summary_data 明确支持的信息，不得补造定义、案例、因果或适用条件。"
            "概况证据不足时少生成或返回空 cards，禁止为了数量凑卡。"
            + TRANSCRIPT_AD_FILTER_PROMPT_CLAUSE
            + redaction_boundary_clause()
            + "\n"
            "只输出符合 response schema 的 JSON，不要代码围栏或解释。每张卡只讲一件事，kind 只能是 concept、method、"
            "insight；title 简洁，summary 1 到 2 句，details 是能脱离视频理解的短讲义。避免依赖章节顺序或把推断写成事实。"
            "tags 最多 4 个，keywords 最多 8 个，related_card_ids 由本地后处理，模型不得输出。该结果仍需产品流程审核或使用。"
        ),
        payload={
            "schema_version": "1.0",
            "prompt_version": VIDEO_KNOWLEDGE_CARD_PROMPT_VERSION,
            "data_class": "untrusted_generated_video_summary",
            "video_title": redact_private_prompt_text(title),
            "summary_data": redact_private_prompt_value(summary_data),
        },
    )
