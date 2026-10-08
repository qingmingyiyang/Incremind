from __future__ import annotations

import pytest

from core.companion_core import companion_provider_instruction, parse_companion_chat_envelope


@pytest.mark.parametrize(
    ("route_key", "terms"),
    [
        ("companion.chat", ("当前角色设定", "事实、推断和建议", "工具完成证据", "只含两个字段")),
        ("companion.diary", ("最近三天结构化互动摘要", "角色自己的体会", "不得杜撰引语", "300 至 600", "只返回日记正文纯文本")),
        ("companion.vision", ("单张屏幕截图", "遮罩区域", "不可信数据", "无法支持判断", "删除临时图片")),
        ("companion.event", ("title 和 artist", "不可信数据", "不得编造", "只返回纯文本中文短评", "最多 160")),
        ("companion.ambient", ("粗粒度时段和心情", "不得要求执行操作", "恰好两个对象", "allowed_result_template_ids")),
        ("companion.voice", ("确认的语音转写文本", "可能有误识别", "适合立即朗读", "本地 TTS", "澄清请求")),
    ],
)
def test_route_instructions_preserve_the_current_contract(route_key: str, terms: tuple[str, ...]) -> None:
    instruction = companion_provider_instruction(route_key)
    assert all(term in instruction for term in terms)


def test_route_instruction_rejects_unknown_routes() -> None:
    with pytest.raises(RuntimeError, match="route is invalid"):
        companion_provider_instruction("companion.unknown")


def test_chat_envelope_accepts_only_the_bounded_affect_contract() -> None:
    assert parse_companion_chat_envelope('{"text":"收到啦","affect":"positive","mood_delta":999}') == ("收到啦", "positive")
    assert parse_companion_chat_envelope('{"text":"收到","affect":"furious"}') == ("收到", "neutral")
    assert parse_companion_chat_envelope("普通文本") == ("普通文本", "neutral")


def test_chat_envelope_rejects_an_empty_response() -> None:
    with pytest.raises(RuntimeError, match="response is empty"):
        parse_companion_chat_envelope("  ")
