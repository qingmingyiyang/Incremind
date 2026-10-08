from __future__ import annotations

import pytest

from core.product_core.document_html_render import (
    DocumentHtmlRenderError,
    DocumentHtmlRenderer,
)


# ── 基础校验 ──


def test_render_rejects_non_mapping_document() -> None:
    renderer = DocumentHtmlRenderer()
    with pytest.raises(DocumentHtmlRenderError, match="document must be a mapping"):
        renderer.render("not-a-mapping", "# title")  # type: ignore[arg-type]


def test_render_rejects_non_string_markdown() -> None:
    renderer = DocumentHtmlRenderer()
    with pytest.raises(DocumentHtmlRenderError, match="markdown must be a string"):
        renderer.render({"id": "doc-1"}, 123)  # type: ignore[arg-type]


def test_render_rejects_missing_document_id() -> None:
    renderer = DocumentHtmlRenderer()
    with pytest.raises(DocumentHtmlRenderError, match="document.id is required"):
        renderer.render({"title": "no id"}, "# title")


def test_render_uses_document_id_when_title_missing() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "正文内容")
    assert result.title == "doc-1"
    assert "<title>doc-1</title>" in result.html


def test_render_returns_result_with_metadata() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render(
        {"id": "doc-001", "title": "测试文档", "revision": 3},
        "# 标题\n\n正文。",
    )
    assert result.document_id == "doc-001"
    assert result.title == "测试文档"
    assert result.revision == 3
    assert isinstance(result.html, str)
    assert "<!DOCTYPE html>" in result.html


def test_render_handles_non_int_revision() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render(
        {"id": "doc-001", "title": "测试", "revision": "3"},
        "正文",
    )
    assert result.revision is None


def test_render_handles_bool_revision_as_none() -> None:
    # bool 是 int 的子类，但不应作为 revision
    renderer = DocumentHtmlRenderer()
    result = renderer.render(
        {"id": "doc-001", "title": "测试", "revision": True},
        "正文",
    )
    assert result.revision is None


def test_to_payload_serializes_result_fields() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render(
        {"id": "doc-001", "title": "测试", "revision": 2},
        "正文",
    )
    payload = result.to_payload()
    assert payload["document_id"] == "doc-001"
    assert payload["title"] == "测试"
    assert payload["revision"] == 2
    assert isinstance(payload["html"], str)


# ── HTML 外壳 ──


def test_html_includes_doctype_and_meta() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "正文")
    assert "<!DOCTYPE html>" in result.html
    assert '<meta charset="UTF-8"' in result.html
    assert 'name="viewport"' in result.html


def test_html_includes_porcelain_css() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "正文")
    # 包含瓷白编辑台关键 CSS 变量
    assert "--cr-bg" in result.html
    assert "--cr-ink" in result.html
    assert "cr-doc" in result.html


def test_html_includes_dark_mode_media_query() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "正文")
    assert "@media (prefers-color-scheme: dark)" in result.html


def test_html_includes_document_id_in_meta() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-xyz"}, "正文")
    assert "Document ID: doc-xyz" in result.html


def test_html_includes_footer() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "正文")
    assert "Porcelain Editorial OS" in result.html
    assert "cr-doc-footer" in result.html


# ── 标题 ──


def test_render_heading_h1_to_h4() -> None:
    renderer = DocumentHtmlRenderer()
    md = "# H1\n\n## H2\n\n### H3\n\n#### H4"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<h1 class="cr-doc-h1">H1</h1>' in result.html
    assert '<h2 class="cr-doc-h2">H2</h2>' in result.html
    assert '<h3 class="cr-doc-h3">H3</h3>' in result.html
    assert '<h4 class="cr-doc-h4">H4</h4>' in result.html


def test_render_heading_caps_at_h4() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "##### 五级标题")
    # 超过 4 级降级为 h4
    assert '<h4 class="cr-doc-h4">' in result.html


# ── 段落 ──


def test_render_paragraph() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "这是一段正文。")
    assert '<p class="cr-doc-p">这是一段正文。</p>' in result.html


def test_render_multiline_paragraph_joined() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "第一行\n第二行")
    # 多行段落合并为一个内容 <p>（不含 header 中的 meta <p>）
    assert result.html.count('<p class="cr-doc-p">') == 1
    assert "第一行" in result.html
    assert "第二行" in result.html


# ── 列表 ──


def test_render_unordered_list() -> None:
    renderer = DocumentHtmlRenderer()
    md = "- 项目一\n- 项目二\n- 项目三"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<ul class="cr-doc-ul">' in result.html
    assert "<li>项目一</li>" in result.html
    assert "<li>项目二</li>" in result.html
    assert "<li>项目三</li>" in result.html


def test_render_ordered_list() -> None:
    renderer = DocumentHtmlRenderer()
    md = "1. 第一\n2. 第二\n3. 第三"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<ol class="cr-doc-ol">' in result.html
    assert "<li>第一</li>" in result.html
    assert "<li>第二</li>" in result.html
    assert "<li>第三</li>" in result.html


# ── 引用块 / 要点突出 ──


def test_render_blockquote() -> None:
    renderer = DocumentHtmlRenderer()
    md = "> 普通引用"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<blockquote class="cr-doc-blockquote">' in result.html
    assert "普通引用" in result.html


def test_render_callout_for_要点() -> None:
    renderer = DocumentHtmlRenderer()
    md = "> **要点**：这是重点内容"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<blockquote class="cr-doc-callout">' in result.html
    assert "<strong>要点</strong>" in result.html
    assert "这是重点内容" in result.html


def test_render_callout_for_要点_with_colon_in_label() -> None:
    renderer = DocumentHtmlRenderer()
    md = "> **要点：**带冒号的要点"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<blockquote class="cr-doc-callout">' in result.html


# ── 代码 ──


def test_render_inline_code() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "使用 `print()` 函数")
    assert '<code class="cr-doc-code">print()</code>' in result.html


def test_render_code_block() -> None:
    renderer = DocumentHtmlRenderer()
    md = "```\nprint('hello')\n```"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<pre class="cr-doc-pre">' in result.html
    # 代码块内容被 HTML 转义（' -> &#x27;），检查转义后的形式
    assert "print(" in result.html
    assert "hello" in result.html
    assert "</pre>" in result.html
    # 确保原始代码以转义形式存在
    assert "&#x27;hello&#x27;" in result.html


# ── 表格 ──


def test_render_table() -> None:
    renderer = DocumentHtmlRenderer()
    md = "| 名称 | 值 |\n| --- | --- |\n| A | 1 |\n| B | 2 |"
    result = renderer.render({"id": "doc-1"}, md)
    assert '<table class="cr-doc-table">' in result.html
    assert "<thead>" in result.html
    assert "<th>名称</th>" in result.html
    assert "<th>值</th>" in result.html
    assert "<td>A</td>" in result.html
    assert "<td>1</td>" in result.html
    assert "<td>B</td>" in result.html
    assert "<td>2</td>" in result.html


# ── 水平线 ──


def test_render_hr_with_dashes() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "上文\n\n---\n\n下文")
    assert '<hr class="cr-doc-hr" />' in result.html


def test_render_hr_with_asterisks() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "***")
    assert '<hr class="cr-doc-hr" />' in result.html


# ── 行内格式 ──


def test_render_bold() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "**加粗文本**")
    assert "<strong>加粗文本</strong>" in result.html


def test_render_italic() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "*斜体文本*")
    assert "<em>斜体文本</em>" in result.html


def test_render_strikethrough() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "~~删除文本~~")
    assert "<del>删除文本</del>" in result.html


# ── 链接 / URL 安全 ──


def test_render_safe_link() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "[示例](https://example.com)")
    assert '<a class="cr-doc-a" href="https://example.com"' in result.html
    assert ">示例</a>" in result.html


def test_render_relative_link() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "[文档](/docs/123)")
    assert 'href="/docs/123"' in result.html


def test_render_javascript_link_stripped() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "[点击](javascript:alert(1))")
    # 危险 URL：返回纯文本 label，不含链接 <a>（用 <a class="cr-doc-a" 精确匹配）
    assert "点击" in result.html
    assert "javascript:alert(1)" not in result.html
    assert '<a class="cr-doc-a"' not in result.html


def test_render_data_link_stripped() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "[data](data:text/html,<script>)")
    assert "data:text/html" not in result.html


# ── HTML 转义 ──


def test_render_escapes_html_in_paragraph() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "<script>alert('xss')</script>")
    assert "<script>" not in result.html
    assert "&lt;script&gt;" in result.html


def test_render_escapes_html_in_heading() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "# <b>标题</b>")
    assert "&lt;b&gt;标题&lt;/b&gt;" in result.html


def test_render_escapes_html_in_code_block() -> None:
    renderer = DocumentHtmlRenderer()
    md = "```\n<div>raw html</div>\n```"
    result = renderer.render({"id": "doc-1"}, md)
    assert "<div>raw html</div>" not in result.html
    assert "&lt;div&gt;raw html&lt;/div&gt;" in result.html


def test_render_escapes_html_in_title() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render(
        {"id": "doc-1", "title": "<script>x</script>"},
        "正文",
    )
    # title 在 <title> 与 <h1> 中都要转义
    assert "<script>" not in result.html
    assert "&lt;script&gt;" in result.html


def test_render_does_not_render_raw_html_in_markdown() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "<div class='evil'>hi</div>")
    # 裸 HTML 标签被当作段落文本转义，不会作为 HTML 渲染
    assert "class='evil'" not in result.html
    assert "&lt;div" in result.html


# ── 隐私：渲染结果不泄露敏感字段 ──


def test_render_does_not_leak_secrets() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "正文")
    lowered = result.html.lower()
    assert "sk-" not in lowered
    assert "cookie" not in lowered
    assert "authorization" not in lowered
    assert "password" not in lowered


# ── 空输入 ──


def test_render_empty_markdown() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "")
    # 空 markdown 仍生成完整 HTML 外壳，body 为空
    assert "<!DOCTYPE html>" in result.html
    assert "<article" in result.html


def test_render_whitespace_only_markdown() -> None:
    renderer = DocumentHtmlRenderer()
    result = renderer.render({"id": "doc-1"}, "   \n\n  ")
    assert "<!DOCTYPE html>" in result.html
