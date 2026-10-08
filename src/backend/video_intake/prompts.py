from __future__ import annotations

from backend.shared.llm.prompt_contracts import (
    prompt_messages,
    redact_private_prompt_text,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)


VIDEO_INTAKE_QA_PROMPT_VERSION = "video-intake-question-answer-v3"
VIDEO_INTAKE_QA_TIMEOUT_SECONDS = 60


def build_video_question_messages(*, question: str, context: str) -> list[dict[str, str]]:
    payload = {
        "schema_version": "1.0",
        "prompt_version": VIDEO_INTAKE_QA_PROMPT_VERSION,
        "data_class": "untrusted_user_question_and_video_evidence",
        "question": redact_private_prompt_text(question),
        "video_evidence": redact_private_prompt_text(context),
    }
    return prompt_messages(
        system=(
            "你是个人视频资料库的只读问答助手。目标是依据当前视频的转写证据，直接、完整地回答用户问题，"
            "帮助用户回到原始时间位置核验。你没有联网搜索、修改视频资料、写笔记、发布内容、调用工具或更新长期记忆的权限。\n"
            + untrusted_envelope_clause(
                fields="question 与 video_evidence",
                content_noun="问题或转写内容",
            )
            + "\n"
            "只使用 video_evidence 中明确出现的事实；不得用常识补造人物、数字、因果、观点或视频未提到的背景。"
            "每个关键判断后逐字复制 video_evidence 中已有的 [时间] 或 [开始时间-结束时间] 标签，禁止生成来源中不存在的时间。"
            "证据冲突时并列说明；资料不足时明确写"
            "当前视频证据不足，并指出缺少哪类内容。建议或推断必须单独标记，不能冒充视频事实。\n"
            + redaction_boundary_clause()
            + "输出简洁中文 Markdown，不要代码围栏，不得宣称已保存、已发布或已执行操作。"
        ),
        payload=payload,
    )
