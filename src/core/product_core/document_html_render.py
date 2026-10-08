"""DocumentHtmlRenderer — Markdown → 统一风格 HTML 渲染器。

纯 Python 实现，无新依赖。支持模板生成的 Markdown 子集：
- 标题 H1-H4
- 段落
- 无序/有序列表
- 引用块 blockquote（含"要点"突出）
- 行内代码 / 围栏代码块
- 表格（GFM 风格 | a | b |）
- 水平分割线
- 链接
- 加粗 / 斜体 / 删除线

安全：所有文本先 HTML 转义，再应用行内格式；不渲染裸 HTML 标签。
风格：内联 CSS，与前端 Porcelain Editorial OS / 瓷白编辑台一致。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from html import escape


class DocumentHtmlRenderError(ValueError):
    """Raised when HTML rendering fails."""


@dataclass(frozen=True, slots=True)
class DocumentHtmlRenderResult:
    """HTML 渲染结果。"""

    html: str
    title: str
    document_id: str
    revision: int | None

    def to_payload(self) -> dict[str, object]:
        return {
            "html": self.html,
            "title": self.title,
            "document_id": self.document_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class DocumentHtmlRenderer:
    """把 Document Markdown 渲染为统一风格 HTML 字符串。

    用法：
        renderer = DocumentHtmlRenderer()
        result = renderer.render(document, markdown)
        html_string = result.html
    """

    def render(
        self,
        document: Mapping[str, object],
        markdown: str,
    ) -> DocumentHtmlRenderResult:
        if not isinstance(document, Mapping):
            raise DocumentHtmlRenderError("document must be a mapping")
        if not isinstance(markdown, str):
            raise DocumentHtmlRenderError("markdown must be a string")
        document_id = str(document.get("id") or "")
        if not document_id:
            raise DocumentHtmlRenderError("document.id is required")
        title = str(document.get("title") or document_id)
        revision = document.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            revision = None
        body_html = _render_markdown(markdown)
        html = _wrap_html(title=title, body_html=body_html, document_id=document_id)
        return DocumentHtmlRenderResult(
            html=html,
            title=title,
            document_id=document_id,
            revision=revision,
        )


# ---------------------------------------------------------------------------
# Markdown → HTML 渲染
# ---------------------------------------------------------------------------


def _render_markdown(markdown: str) -> str:
    """把 Markdown 渲染为 HTML 片段（不含 <html><body> 外壳）。

    按行分块处理：标题、围栏代码、列表、引用、表格、水平线、段落。
    """
    lines = markdown.split("\n")
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 围栏代码块 ```
        if stripped.startswith("```"):
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过闭合 ```
            blocks.append(_render_code_block("\n".join(code_lines)))
            continue

        # 空行
        if not stripped:
            i += 1
            continue

        # 标题 # ## ### ####
        if stripped.startswith("#"):
            blocks.append(_render_heading(stripped))
            i += 1
            continue

        # 水平线 --- / ***
        if stripped in {"---", "***", "___"} or _is_hr(stripped):
            blocks.append('<hr class="cr-doc-hr" />')
            i += 1
            continue

        # 表格（| a | b |）
        if stripped.startswith("|") and i + 1 < n and _is_table_separator(lines[i + 1].strip()):
            table_lines: list[str] = []
            while i < n and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            blocks.append(_render_table(table_lines))
            continue

        # 无序列表 - / * / +
        if _is_ul_item(stripped):
            list_lines: list[str] = []
            while i < n and _is_ul_item(lines[i].strip()):
                list_lines.append(lines[i].strip())
                i += 1
            blocks.append(_render_ul(list_lines))
            continue

        # 有序列表 1. 2.
        if _is_ol_item(stripped):
            list_lines = []
            while i < n and _is_ol_item(lines[i].strip()):
                list_lines.append(lines[i].strip())
                i += 1
            blocks.append(_render_ol(list_lines))
            continue

        # 引用块 >
        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.append(_render_blockquote(quote_lines))
            continue

        # 段落（连续非空非块行）
        para_lines: list[str] = []
        while i < n:
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("#")
                or nxt.startswith("```")
                or nxt.startswith("|")
                or nxt.startswith(">")
                or _is_ul_item(nxt)
                or _is_ol_item(nxt)
                or _is_hr(nxt)
            ):
                break
            para_lines.append(lines[i])
            i += 1
        if para_lines:
            blocks.append(_render_paragraph("\n".join(para_lines)))

    return "\n".join(blocks)


def _is_hr(stripped: str) -> bool:
    if len(stripped) < 3:
        return False
    char = stripped[0]
    if char not in {"-", "*", "_"}:
        return False
    return all(c == char for c in stripped) and len(stripped) >= 3


def _is_ul_item(stripped: str) -> bool:
    if len(stripped) < 2:
        return False
    if stripped[0] in {"-", "*", "+"} and stripped[1] == " ":
        return True
    return False


def _is_ol_item(stripped: str) -> bool:
    if len(stripped) < 3:
        return False
    j = 0
    while j < len(stripped) and stripped[j].isdigit():
        j += 1
    if j == 0 or j >= len(stripped):
        return False
    return stripped[j] == "." and j + 1 < len(stripped) and stripped[j + 1] == " "


def _is_table_separator(stripped: str) -> bool:
    if not stripped.startswith("|"):
        return False
    cells = stripped.split("|")
    for cell in cells[1:-1]:
        cell = cell.strip()
        if not cell:
            continue
        if not all(c in {"-", ":"} for c in cell):
            return False
    return True


def _render_heading(stripped: str) -> str:
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level > 4:
        level = 4
    content = stripped[level:].strip()
    return f'<h{level} class="cr-doc-h{level}">{_inline(content)}</h{level}>'


def _render_paragraph(text: str) -> str:
    return f'<p class="cr-doc-p">{_inline(text)}</p>'


def _render_ul(items: list[str]) -> str:
    parts = ['<ul class="cr-doc-ul">']
    for item in items:
        content = item[2:].strip() if len(item) > 2 else ""
        parts.append(f"<li>{_inline(content)}</li>")
    parts.append("</ul>")
    return "\n".join(parts)


def _render_ol(items: list[str]) -> str:
    parts = ['<ol class="cr-doc-ol">']
    for item in items:
        dot = item.find(".")
        content = item[dot + 1 :].strip() if dot >= 0 else ""
        parts.append(f"<li>{_inline(content)}</li>")
    parts.append("</ol>")
    return "\n".join(parts)


def _render_blockquote(lines: list[str]) -> str:
    joined = " ".join(lines).strip()
    cls = "cr-doc-blockquote"
    # "要点"突出：> **要点**：内容
    if joined.startswith("**要点**") or joined.startswith("**要点："):
        cls = "cr-doc-callout"
    return f'<blockquote class="{cls}">{_inline(joined)}</blockquote>'


def _render_code_block(code: str) -> str:
    escaped = escape(code)
    return f'<pre class="cr-doc-pre"><code>{escaped}</code></pre>'


def _render_table(lines: list[str]) -> str:
    if len(lines) < 2:
        return ""
    header_cells = [c.strip() for c in lines[0].split("|")[1:-1]]
    # lines[1] 是分隔行，跳过
    body_rows: list[list[str]] = []
    for line in lines[2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        body_rows.append(cells)
    parts = ['<table class="cr-doc-table">']
    parts.append("<thead><tr>")
    for cell in header_cells:
        parts.append(f"<th>{_inline(cell)}</th>")
    parts.append("</tr></thead>")
    parts.append("<tbody>")
    for row in body_rows:
        parts.append("<tr>")
        for cell in row:
            parts.append(f"<td>{_inline(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


# 行内格式：加粗 / 斜体 / 删除线 / 代码 / 链接
def _inline(text: str) -> str:
    if not text:
        return ""
    # 先转义，再用占位符替换行内格式，避免转义后的内容被二次处理
    escaped = escape(text)
    result = escaped
    # 链接 [text](url) — url 只允许 http/https/相对路径
    import re

    def _link_repl(match: re.Match) -> str:
        label = match.group(1)
        url = match.group(2)
        if not _is_safe_url(url):
            return label
        return f'<a class="cr-doc-a" href="{url}" target="_blank" rel="noreferrer">{label}</a>'

    result = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link_repl, result)
    # 行内代码 `code`
    result = re.sub(r"`([^`]+)`", r'<code class="cr-doc-code">\1</code>', result)
    # 加粗 **text** 或 __text__
    result = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", result)
    result = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", result)
    # 删除线 ~~text~~
    result = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", result)
    # 斜体 *text* 或 _text_（放在加粗后避免冲突）
    result = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", result)
    return result


def _is_safe_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    if lowered.startswith(("http://", "https://", "/")):
        return True
    # 阻止 javascript:/data: 等危险协议
    if ":" in lowered and not lowered.startswith(("http://", "https://")):
        return False
    return True


# ---------------------------------------------------------------------------
# HTML 外壳 + 内联 CSS
# ---------------------------------------------------------------------------


_PORCELAIN_CSS = """
:root {
  --cr-bg: #FFFCF5;
  --cr-bg-2: #F8F1E7;
  --cr-ink: #2A2520;
  --cr-text: #3F362F;
  --cr-muted: #706B64;
  --cr-faint: #A7A19A;
  --cr-line: #E8E6E2;
  --cr-red: #B30000;
  --cr-red-soft: #F1B9B3;
  --cr-sage: #DDE8E2;
  --font-serif: "Noto Serif SC", "Songti SC", "STSong", serif;
  --font-sans: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --cr-bg: #1A1714;
    --cr-bg-2: #221E1A;
    --cr-ink: #E8E4DE;
    --cr-text: #C9C2BA;
    --cr-muted: #8E8680;
    --cr-faint: #5E5852;
    --cr-line: #2E2924;
    --cr-red: #E07070;
    --cr-red-soft: #4A2A2A;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 40px 24px;
  background: linear-gradient(180deg, var(--cr-bg), var(--cr-bg-2));
  color: var(--cr-text);
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: 1.75;
  min-height: 100vh;
}
.cr-doc {
  max-width: 760px;
  margin: 0 auto;
  padding: 40px 48px;
  background: var(--cr-bg);
  border: 1px solid var(--cr-line);
  border-radius: 18px;
  box-shadow: 0 8px 28px rgba(55, 45, 35, 0.07);
}
.cr-doc-header {
  margin-bottom: 28px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--cr-line);
}
.cr-doc-title {
  margin: 0 0 6px;
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 700;
  color: var(--cr-ink);
  line-height: 1.35;
}
.cr-doc-meta {
  margin: 0;
  color: var(--cr-faint);
  font-size: 12px;
  letter-spacing: 0.04em;
}
.cr-doc-h1 {
  margin: 28px 0 12px;
  font-family: var(--font-serif);
  font-size: 22px;
  font-weight: 700;
  color: var(--cr-ink);
  border-bottom: 1px solid var(--cr-line);
  padding-bottom: 6px;
}
.cr-doc-h2 {
  margin: 24px 0 10px;
  font-family: var(--font-serif);
  font-size: 18px;
  font-weight: 700;
  color: var(--cr-ink);
}
.cr-doc-h3 {
  margin: 20px 0 8px;
  font-family: var(--font-serif);
  font-size: 15px;
  font-weight: 700;
  color: var(--cr-ink);
}
.cr-doc-h4 {
  margin: 16px 0 6px;
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 600;
  color: var(--cr-ink);
}
.cr-doc-p {
  margin: 10px 0;
  color: var(--cr-text);
}
.cr-doc-ul, .cr-doc-ol {
  margin: 10px 0;
  padding-left: 24px;
  color: var(--cr-text);
}
.cr-doc-ul li, .cr-doc-ol li {
  margin: 4px 0;
}
.cr-doc-blockquote {
  margin: 12px 0;
  padding: 10px 16px;
  border-left: 3px solid var(--cr-muted);
  background: rgba(0, 0, 0, 0.02);
  color: var(--cr-muted);
  font-style: italic;
  border-radius: 0 8px 8px 0;
}
.cr-doc-callout {
  margin: 14px 0;
  padding: 12px 18px;
  border-left: 3px solid var(--cr-red);
  background: linear-gradient(90deg, var(--cr-red-soft) 0%, transparent 100%);
  color: var(--cr-ink);
  font-weight: 500;
  border-radius: 0 12px 12px 0;
}
.cr-doc-callout strong {
  color: var(--cr-red);
}
.cr-doc-code {
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--cr-line);
  color: var(--cr-red);
  font-family: ui-monospace, "SF Mono", Consolas, monospace;
  font-size: 13px;
}
.cr-doc-pre {
  margin: 12px 0;
  padding: 14px 16px;
  border: 1px solid var(--cr-line);
  border-radius: 10px;
  background: var(--cr-bg-2);
  overflow-x: auto;
}
.cr-doc-pre code {
  padding: 0;
  background: transparent;
  color: var(--cr-ink);
  font-family: ui-monospace, "SF Mono", Consolas, monospace;
  font-size: 13px;
  line-height: 1.6;
}
.cr-doc-table {
  width: 100%;
  margin: 14px 0;
  border-collapse: collapse;
  border: 1px solid var(--cr-line);
  border-radius: 8px;
  overflow: hidden;
  font-size: 14px;
}
.cr-doc-table th, .cr-doc-table td {
  padding: 8px 12px;
  border: 1px solid var(--cr-line);
  text-align: left;
}
.cr-doc-table th {
  background: var(--cr-bg-2);
  color: var(--cr-ink);
  font-weight: 600;
}
.cr-doc-table td {
  color: var(--cr-text);
}
.cr-doc-a {
  color: var(--cr-red);
  text-decoration: underline;
  text-underline-offset: 2px;
}
.cr-doc-a:hover {
  opacity: 0.8;
}
.cr-doc-hr {
  margin: 20px 0;
  border: none;
  border-top: 1px solid var(--cr-line);
}
.cr-doc-footer {
  margin-top: 32px;
  padding-top: 16px;
  border-top: 1px solid var(--cr-line);
  color: var(--cr-faint);
  font-size: 11px;
  text-align: right;
  letter-spacing: 0.04em;
}
"""


def _wrap_html(*, title: str, body_html: str, document_id: str) -> str:
    """生成完整 HTML 文档（含内联 CSS）。"""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="UTF-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_PORCELAIN_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<article class="cr-doc">\n'
        '<header class="cr-doc-header">\n'
        f'<h1 class="cr-doc-title">{escape(title)}</h1>\n'
        f'<p class="cr-doc-meta">Document ID: {escape(document_id)}</p>\n'
        "</header>\n"
        f"{body_html}\n"
        '<footer class="cr-doc-footer">\n'
        "Generated by Chriptmas OS · Porcelain Editorial OS\n"
        "</footer>\n"
        "</article>\n"
        "</body>\n"
        "</html>\n"
    )
