"""Scope tags and the workbench's documented intent selection rules."""

import pytest


@pytest.mark.parametrize(
    "text,expected",
    [
        ("#项目 记下这段话", ("项目", None, "记下这段话")),
        ("#职业规划\n保留这份资料", ("职业规划", None, "保留这份资料")),
        ("记下这段话 #项目", ("项目", None, "记下这段话")),
        ("保留这份资料\n#职业规划", ("职业规划", None, "保留这份资料")),
        ("#项目/场景 怎么安排？", ("项目", "场景", "怎么安排？")),
        ("#职业规划/面试\n上次聊了什么", ("职业规划", "面试", "上次聊了什么")),
        ("怎么安排？ #项目/场景", ("项目", "场景", "怎么安排？")),
        ("上次聊了什么\n#职业规划/面试", ("职业规划", "面试", "上次聊了什么")),
        ("  #项目/场景  正文  ", ("项目", "场景", "正文")),
        ("  正文  #项目/场景  ", ("项目", "场景", "正文")),
        ("#项目", ("项目", None, "")),
        ("#项目/场景", ("项目", "场景", "")),
        ("第一行\n#项目 第二行\n第三行", ("项目", None, "第一行\n第二行\n第三行")),
        ("第一行\n第二行 #项目/场景\n第三行", ("项目", "场景", "第一行\n第二行\n第三行")),
        ("#项目\r\n怎么开始", ("项目", None, "怎么开始")),
        ("#项目/场景\r\n上次聊了什么", ("项目", "场景", "上次聊了什么")),
        ("正文 #项目\r\n下一行", ("项目", None, "正文\r\n下一行")),
        ("正文 #项目/场景\r\n下一行", ("项目", "场景", "正文\r\n下一行")),
        ("正文", (None, None, "正文")),
        ("", (None, None, "")),
        ("正文 #项目 继续正文", (None, None, "正文 #项目 继续正文")),
        ("正文 #项目/场景 继续正文", (None, None, "正文 #项目/场景 继续正文")),
        ("# 标题", (None, None, "# 标题")),
        ("https://example.com/#片段", (None, None, "https://example.com/#片段")),
    ],
)
def test_parse_scope_tag(text, expected):
    from backend.memory_app.v2.intent import parse_scope_tag

    assert parse_scope_tag(text) == expected


@pytest.mark.parametrize(
    "text,has_files,expected",
    [
        ("https://example.com/read 怎么办？", False, "remember"),
        ("http://example.com/read 帮我整理", False, "remember"),
        ("怎么理解这份资料？", True, "remember"),
        ("帮我写一份报告", True, "remember"),
        ("灵感：把两个方案结合", False, "inspiration"),
        ("灵感 新的方向？", False, "inspiration"),
        ("帮我写报告", False, "do"),
        ("帮我总结？", False, "do"),
        ("请帮我找资料", False, "do"),
        ("请帮忙做总结", False, "do"),
        ("写一篇文章", False, "do"),
        ("写报告？", False, "do"),
        ("整理一份会议纪要", False, "do"),
        ("整理一份复习材料", False, "do"),
        ("做一份预算", False, "do"),
        ("做一份行程", False, "do"),
        ("这是什么？", False, "ask"),
        ("能找到吗?", False, "ask"),
        ("什么办法可行", False, "ask"),
        ("什么是摘要", False, "ask"),
        ("怎么开始", False, "ask"),
        ("怎么整理", False, "ask"),
        ("为什么会这样", False, "ask"),
        ("为什么有差异", False, "ask"),
        ("哪条最相关", False, "ask"),
        ("哪个方案合适", False, "ask"),
        ("上次聊了什么", False, "ask"),
        ("上次的材料在哪里", False, "ask"),
        ("保留这句话", False, "remember"),
        ("今天读了这篇材料", False, "remember"),
        ("#项目 怎么开始", False, "ask"),
        ("怎么开始 #项目/场景", False, "ask"),
        ("#项目/场景 帮我做报告", False, "do"),
        ("帮我做报告 #项目", False, "do"),
        ("#项目 灵感 新方向", False, "inspiration"),
        ("灵感 新方向 #项目/场景", False, "inspiration"),
        ("#项目 https://example.com/read", False, "remember"),
        ("https://example.com/read #项目/场景", False, "remember"),
        ("  怎么开始？  ", False, "ask"),
        ("\n帮我写报告\n", False, "do"),
        ("", False, "remember"),
        ("#项目/场景", False, "remember"),
    ],
)
def test_route_intent(text, has_files, expected):
    from backend.memory_app.v2.intent import route_intent

    assert route_intent(text, has_files) == expected
