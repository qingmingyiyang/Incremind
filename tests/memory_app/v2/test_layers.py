"""Layer projections retain exact document offsets without inventing source evidence."""
import pytest

from backend.memory_app.workspace_generation import _markdown


@pytest.mark.parametrize("markdown,expected", [
    ("# 标题\n\n旧引言\n\n## 摘要\n\n新摘要。\n第二行。\n\n## 关键事实\n- 原文事实\n", "新摘要。\n第二行。"),
    ("# 标题\n\n旧格式摘要。\n第二行。\n\n## 关键事实\n- 事实", "旧格式摘要。\n第二行。"),
    ("# 标题\r\n\r\n## 摘要\r\n\r\n摘要😀\r\n第二行\r\n\r\n## 关键事实\r\n- 事实", "摘要😀\r\n第二行"),
    ("# 标题\n\n## 摘要\n\n重复\n\n重复\n\n## 待办\n- 项", "重复\n\n重复"),
    ("# 标题\n\n## 摘要\n内容\n### 子标题\n内容二\n# 新文档\n后文", "内容\n### 子标题\n内容二"),
    ("# 标题\n\n   摘要正文。  \n\n## 关键事实\n- 事实", "摘要正文。"),
])
def test_summary_preserves_exact_original_markdown_span(markdown, expected):
    from backend.memory_app.v2.layers import summary_of
    text, start, end = summary_of(markdown)
    assert text == expected
    assert markdown[start:end] == text
    assert start == markdown.index(expected)
    assert end == start + len(expected)


@pytest.mark.parametrize("markdown", ["", " \n", "# 标题\n", "# 标题\n\n## 关键事实\n- 事实",
    "# 标题\n\n## 摘要\n\n## 关键事实\n- 事实", "# 标题\n\n- 待办", "# 标题\n\n```text\n代码\n```"])
def test_missing_or_empty_summary_is_not_a_fact_heading_or_code_block(markdown):
    from backend.memory_app.v2.layers import summary_of
    assert summary_of(markdown) == ("", 0, 0)


def test_fenced_pseudo_heading_is_not_summary():
    from backend.memory_app.v2.layers import summary_of
    markdown = "# 标题\n\n旧摘要\n\n```md\n## 摘要\n伪摘要\n```\n\n## 关键事实\n- 事实"
    assert summary_of(markdown) == ("旧摘要", markdown.index("旧摘要"), markdown.index("旧摘要") + 3)


@pytest.mark.parametrize("markdown,expected", [
    ("# 标题\n\n## 摘要\n摘要\n\n## 关键事实\n- 事实一\n- 事实二\n\n## 待办\n- 待办一", ["事实一", "事实二"]),
    ("# 标题\r\n\r\n## 关键事实\r\n- 重复😀\r\n- 重复😀\r\n", ["重复😀", "重复😀"]),
    ("## 关键事实\n* 事实一\n+ 事实二\n- **事实三**\n", ["事实一", "事实二", "**事实三**"]),
    ("## 关键事实\n```md\n- 代码\n```\n- 真实事实\n## 待办\n- 待办", ["真实事实"]),
    ("## 摘要\n- 摘要列表\n## 待办\n- 待办", []),
    ("", []),
    ("## 关键事实\n- 第一句\n续行。\n- 第二句\n  续行二\n\n普通段落\n## 待办\n- 待办", ["第一句\n续行。", "第二句\n  续行二"]),
    ("```markdown\n## 关键事实\n- 代码样例\n```\n## 待办\n- 待办", []),
])
def test_facts_are_text_only_and_never_invent_original_source_coordinates(markdown, expected):
    from backend.memory_app.v2.layers import facts_of
    assert facts_of(markdown) == expected


def test_generated_markdown_labels_summary_before_other_sections():
    draft = {"title": "整理标题", "summary": "摘要内容", "topics": ["主题"],
             "facts": [{"text": "原文事实", "evidence": {"start": 0, "end": 4, "quote": "原文事实"}}],
             "todos": [], "uncertainties": [], "people": [], "dates": [], "suggestions": []}
    assert _markdown(draft) == "# 整理标题\n\n## 摘要\n\n摘要内容\n\n## 主题\n- 主题\n\n## 关键事实\n- 原文事实\n"
