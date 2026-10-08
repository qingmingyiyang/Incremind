"""模板章节树（Outline）—— 让 answer_manual / review / project_summary 等模板
按固定结构渲染 Markdown，而不是写死章节顺序。

Outline 由若干 section 组成，每个 section 声明：
- section_id：唯一标识（便于前端锚点跳转和 ProjectSkill 覆盖）
- title：章节标题（如"结论"、"依据"、"来源"）
- kind：内容类型，决定 OutlineApplier 如何填充
- required：是否必需（False 时空内容则跳过该 section）

kind 取值：
- prompt       —— 生成提示词
- series       —— 系列名称
- summary      —— 摘要文本
- key_points   —— 关键点列表
- body         —— 正文（来源由调用者决定：structured_body / media_text / provider_markdown）
- uncertain    —— 待确认提示
- sources      —— 来源行列表

ProjectSkill 可选 `outline` 字段（见 project_skill.schema.json）覆盖默认 outline，
与 style_preferences 的合并模式一致：项目级 outline 优先于模板默认。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


# 支持的 section kind 枚举（避免拼写错误）
SUPPORTED_KINDS = frozenset(
    {"prompt", "series", "summary", "key_points", "body", "uncertain", "sources"}
)


class OutlineError(ValueError):
    """Outline 校验或渲染错误。"""


@dataclass(frozen=True, slots=True)
class OutlineSection:
    """Outline 中的一个章节。"""

    section_id: str
    title: str
    kind: str
    required: bool = True

    def to_payload(self) -> dict[str, object]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "kind": self.kind,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class Outline:
    """一个模板的完整章节树。"""

    sections: tuple[OutlineSection, ...]

    @classmethod
    def from_payload(cls, payload: object) -> "Outline":
        """从 JSON-like payload 构造 Outline，做完整校验。"""
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise OutlineError("outline must be a list of sections")
        if len(payload) == 0:
            raise OutlineError("outline must contain at least one section")

        sections: list[OutlineSection] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(payload):
            if not isinstance(item, Mapping):
                raise OutlineError(f"outline section at index {index} must be an object")
            section = _section_from_mapping(item, index)
            if section.section_id in seen_ids:
                raise OutlineError(
                    f"outline section_id '{section.section_id}' duplicated at index {index}"
                )
            seen_ids.add(section.section_id)
            sections.append(section)
        return cls(sections=tuple(sections))

    def to_payload(self) -> list[dict[str, object]]:
        return [section.to_payload() for section in self.sections]


def _section_from_mapping(item: Mapping[str, object], index: int) -> OutlineSection:
    section_id = _required_str_field(item, "section_id", index)
    title = _required_str_field(item, "title", index)
    kind = _required_str_field(item, "kind", index)
    if kind not in SUPPORTED_KINDS:
        raise OutlineError(
            f"outline section at index {index} has unsupported kind '{kind}'"
        )
    required_value = item.get("required", True)
    if not isinstance(required_value, bool):
        raise OutlineError(
            f"outline section at index {index} 'required' must be boolean"
        )
    return OutlineSection(
        section_id=section_id,
        title=title,
        kind=kind,
        required=required_value,
    )


def _required_str_field(item: Mapping[str, object], field: str, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise OutlineError(
            f"outline section at index {index} missing non-empty '{field}'"
        )
    return value.strip()


@dataclass(frozen=True, slots=True)
class OutlineRenderInputs:
    """OutlineApplier 渲染所需的全部输入。

    所有 kind 共享这一组输入，按 kind 取用对应字段：
    - prompt kind → prompt
    - series kind → series_name
    - summary kind → summary
    - key_points kind → key_points
    - body kind → body
    - uncertain kind → uncertain_notes
    - sources kind → sources
    """

    template_label: str
    title: str
    series_name: str
    prompt: str
    summary: str
    key_points: tuple[str, ...]
    body: str
    sources: tuple[str, ...]
    uncertain_notes: tuple[str, ...]


class OutlineApplier:
    """按 Outline 顺序渲染 Markdown。

    保持纯函数式：不读 ObjectStore、不读 ProjectSkill，所有输入通过
    OutlineRenderInputs 传入。调用者（_markdown / _media_markdown /
    _provider_wrapped_markdown）负责准备 inputs。
    """

    def render(
        self,
        outline: Outline,
        inputs: OutlineRenderInputs,
    ) -> str:
        lines: list[str] = [f"# {inputs.template_label}：{inputs.title}", ""]
        for section in outline.sections:
            section_lines = self._render_section(section, inputs)
            if section_lines is None:
                # required=False 且无内容 → 跳过
                continue
            lines.extend(section_lines)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_section(
        self,
        section: OutlineSection,
        inputs: OutlineRenderInputs,
    ) -> list[str] | None:
        if section.kind == "prompt":
            return self._text_section(section, inputs.prompt, fallback="- 待补充生成提示词")
        if section.kind == "series":
            return self._text_section(section, inputs.series_name, fallback="- 待补充系列")
        if section.kind == "summary":
            return self._text_section(section, inputs.summary, fallback="- 待补充摘要")
        if section.kind == "key_points":
            return self._list_section(section, inputs.key_points, fallback="- 待补充关键点")
        if section.kind == "body":
            return self._text_section(section, inputs.body, fallback="- 待补充正文")
        if section.kind == "uncertain":
            notes = inputs.uncertain_notes
            if not notes:
                # 无自定义 notes：required=True 用默认提示，required=False 跳过
                if not section.required:
                    return None
                notes = (
                    "- 请确认标题、系列归类和关键点是否准确。",
                    "- 如需进入长期记忆，仍需走 Memory Candidate review / publication。",
                )
            return self._list_section(section, notes, fallback=None)
        if section.kind == "sources":
            return self._list_section(section, inputs.sources, fallback="- 暂无来源信息")
        # 不应到达这里（kind 已在 from_payload 校验）
        raise OutlineError(f"unsupported kind '{section.kind}' at render time")

    @staticmethod
    def _text_section(
        section: OutlineSection,
        content: str,
        *,
        fallback: str,
    ) -> list[str] | None:
        text = content.strip()
        if not text:
            if not section.required:
                return None
            text = fallback
        return [f"## {section.title}", "", text, ""]

    @staticmethod
    def _list_section(
        section: OutlineSection,
        items: Sequence[str],
        *,
        fallback: str | None,
    ) -> list[str] | None:
        clean_items = [item for item in items if isinstance(item, str) and item.strip()]
        if not clean_items:
            if not section.required:
                return None
            if fallback is None:
                return None
            clean_items = [fallback]
        lines = [f"## {section.title}", ""]
        for item in clean_items:
            # 已含列表标记则原样输出，否则补 "- "
            if item.lstrip().startswith(("-", "*")):
                lines.append(item)
            else:
                lines.append(f"- {item}")
        lines.append("")
        return lines
