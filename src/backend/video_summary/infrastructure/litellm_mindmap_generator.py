from __future__ import annotations

import json

from backend.shared.llm import LiteLLMCompletionGateway, WireAttemptSink
from backend.video_summary.generation.ports import MindmapGenerator
from backend.video_summary.infrastructure.prompts import (
    MINDMAP_PROMPT_TEMPLATE,
    VIDEO_MINDMAP_TIMEOUT_SECONDS,
    build_mindmap_messages,
)
from backend.video_summary.generation import MindmapNodePayload


class LiteLLMMindmapGenerator(MindmapGenerator):
    def __init__(self, gateway: LiteLLMCompletionGateway) -> None:
        self._gateway = gateway

    async def generate(
        self,
        *,
        title: str,
        duration_seconds: float,
        summary_data: dict[str, object],
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> dict[str, object]:
        payload = await self._gateway.acomplete_structured(
            build_mindmap_messages(
                title=title,
                duration_seconds=duration_seconds,
                summary_data=summary_data,
            ),
            response_model=MindmapNodePayload,
            retries=3,
            timeout=VIDEO_MINDMAP_TIMEOUT_SECONDS,
            wire_attempt_sink=wire_attempt_sink,
        )
        _validate_mindmap_payload(payload, duration_seconds=duration_seconds)
        return payload.model_dump()


def build_mindmap_prompt(*, title: str, duration_seconds: float, summary_data: dict[str, object]) -> str:
    return MINDMAP_PROMPT_TEMPLATE.format(
        title=title,
        duration_seconds=int(duration_seconds),
        summary_json=json.dumps(summary_data, ensure_ascii=False, indent=2),
    )


def _validate_mindmap_payload(payload: MindmapNodePayload, *, duration_seconds: float) -> None:
    maximum = max(0.0, duration_seconds)
    seen_ids: set[str] = set()

    def visit(node: MindmapNodePayload) -> None:
        node_id = node.id.strip()
        if not node_id or node_id in seen_ids:
            raise RuntimeError("思维导图包含空或重复节点 ID，已拒绝保存。")
        seen_ids.add(node_id)
        if not node.title.strip():
            raise RuntimeError("思维导图包含空标题节点，已拒绝保存。")
        if node.start_seconds < 0 or node.end_seconds < node.start_seconds or node.end_seconds > maximum:
            raise RuntimeError("思维导图包含超出视频范围的时间，已拒绝保存。")
        for child in node.children:
            visit(child)

    visit(payload)
