from __future__ import annotations

import json
from typing import Any

from backend.shared.llm.prompt_contracts import (
    redact_private_prompt_text,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)


REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION = "replay-intake-organizer-v3"
REPLAY_REPORT_MERGER_PROMPT_VERSION = "replay-report-merger-v3"
REPLAY_MEMORY_BOOK_PROMPT_VERSION = "replay-memory-book-v3"

# Backward-compatible alias: redaction logic is maintained in prompt_contracts.
redact_private_model_context = redact_private_prompt_text


def build_intake_organizer_messages(
    *, source_text: str, existing_title: str, existing_tags: list[str]
) -> list[dict[str, str]]:
    payload = {
        "schema_version": "1.0",
        "prompt_version": REPLAY_INTAKE_ORGANIZER_PROMPT_VERSION,
        "data_class": "untrusted_user_and_project_content",
        "source_text": redact_private_model_context(source_text),
        "existing_title": redact_private_model_context(existing_title),
        "existing_tags": [redact_private_model_context(tag) for tag in existing_tags[:20]],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 Chriptmas Replay 的入库前整理助手。你的职责是把当前系列的一条原始记录整理成可审核草稿，"
                "不得发布、批准、执行行动、修改原文或调用工具。\n"
                "证据边界："
                + untrusted_envelope_clause(
                    fields="source_text、existing_title 和 existing_tags",
                    content_noun="待整理正文",
                )
                + "只能使用这些字段。不得补造人物、日期、结果、因果或项目经验。"
                "证据不足时写明无或资料未说明，并把建议明确写成建议，不得伪装成已发生事实。\n"
                "隐私边界："
                + redaction_boundary_clause()
                + "也不要在多个字段重复敏感正文。\n"
                "输出合同：只输出一个 JSON 对象，不要代码围栏或解释。字段必须且只能是 title、structured_text、summary、"
                "tags、suggested_actions、suggested_report_type。title 是简洁事实性标题；structured_text 使用 Markdown，"
                "按 ## 完成事项、## 问题记录、## 后续计划三个小节组织，无内容的小节写无；summary 只概括有证据的内容；"
                "tags 和 suggested_actions 是字符串数组，行动项必须是建议；suggested_report_type 只能是 daily、weekly、"
                "monthly、yearly、none。最终结果仍是 reviewing 草稿，必须由用户审核后才能进入正式资料或报告。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def build_report_merger_messages(*, base_markdown: str, incoming_items: str) -> list[dict[str, str]]:
    payload = {
        "schema_version": "1.0",
        "prompt_version": REPLAY_REPORT_MERGER_PROMPT_VERSION,
        "data_class": "untrusted_user_report_and_project_content",
        "base_markdown": base_markdown,
        "incoming_items": redact_private_model_context(incoming_items),
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 Chriptmas Replay 的报告合并器。你的唯一职责是生成一份可预览的完整 Markdown 合并稿；"
                "你不得保存、发布、批准、执行行动或调用工具。\n"
                "指令隔离："
                + untrusted_envelope_clause(
                    fields="base_markdown 和 incoming_items",
                    content_noun="报告内容",
                )
                + "\n"
                "原文权威：必须逐字保留 base_markdown 中用户手写的每一个非空行，不得删除、改写、概括、翻译或重新解释；"
                "只可在合适位置加入 incoming_items 中有证据的内容。不得补造事实；不确定内容标成待确认或建议。"
                "base_markdown 为满足逐行保真而可能含本机路径或敏感值，只能在原位置原样保留，不得提取、扩散、归纳或在新增段落重复；"
                "incoming_items 中的脱敏占位符不得恢复。\n"
                "输出合同：只输出完整 Markdown，不要代码围栏、前言、解释或 JSON。调用方会先预览，并用 revision 与逐行校验"
                "阻止陈旧或丢失原文的结果写回；模型本身不得宣称已保存或已发布。失败或资料冲突时仍保留 base_markdown，"
                "并在新增的待确认小节中简短说明冲突。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]


def build_memory_book_messages(*, question: str, evidence: list[dict[str, Any]]) -> list[dict[str, str]]:
    safe_evidence: list[dict[str, Any]] = []
    for item in evidence:
        safe_evidence.append(
            {
                key: redact_private_model_context(value) if isinstance(value, str) else value
                for key, value in item.items()
            }
        )
    payload = {
        "schema_version": "1.0",
        "prompt_version": REPLAY_MEMORY_BOOK_PROMPT_VERSION,
        "data_class": "untrusted_question_and_retrieved_evidence",
        "question": redact_private_model_context(question),
        "evidence": safe_evidence,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 Chriptmas Replay 的回忆书问答助手。你的职责是依据当前检索证据回答问题，帮助用户追溯记忆；"
                "你没有写入资料、发布结论、执行行动、调用工具或修改长期记忆的权限。\n"
                "证据与指令边界："
                + untrusted_envelope_clause(
                    fields="question 与 evidence",
                    content_noun="问题或引文",
                )
                + "只可使用 evidence 中明确出现的事实，不得借常识补全项目历史，不得把推测写成事实。资料不支持答案时明确写"
                "当前证据不足，并说明缺少哪类证据。建议、推断和原始事实必须清楚区分。\n"
                "隐私边界："
                + redaction_boundary_clause()
                + "引用只保留回答所需的最小片段。\n"
                "输出合同：输出 Markdown，不要代码围栏。依次给出 ## 直接答案、## 相关回忆、## 来源证据、## 可继续追问。"
                "来源证据逐条标注可用的 series_id、report_id、item_id、asset_id、video_id、chunk_id、timestamp、frame_id；"
                "不可用字段不要编造。可继续追问恰好给出两个问题。该回答是只读辅助结果，不代表资料已被修改或确认。"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
