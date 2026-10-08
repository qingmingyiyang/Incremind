"""StyleProfile — 统一输出范式服务。

把 Persona（语言风格/格式偏好/需要避免）与 ProjectSkill.style_preferences
（voice/format_defaults）合并为单一风格档案，并渲染为提示词前缀，
在所有模板生成路径（Source/Provider/Media）中无条件注入，确保输出风格统一。

设计要点：
- 无 Persona / ProjectSkill 时返回空字符串，调用方可无条件 prepend
- 不读取文件内容、不调用外部 API，纯 ObjectStore 读取
- 输出纯文本提示词，不含敏感字符串
- 与既有 PersonaTemplateContext.render_template_prefix 互补：
  既有方法只覆盖 Persona，本服务额外合并 ProjectSkill.style_preferences
- 通过 ObjectStorePersonaRepository.digest() 读取 Persona，因为存储记录
  使用 statements 列表（按 category 分组），digest 才会暴露
  language_style / format_preferences / avoidances 等聚合字段
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.product_core.persona import ObjectStorePersonaRepository, PersonaDigest
from .ports import ObjectStorePort


class StyleProfileError(ValueError):
    """Raised when style profile construction fails."""


@dataclass(frozen=True, slots=True)
class StyleProfile:
    """合并后的统一风格档案。

    字段语义对齐 PersonaDigest + ProjectSkill.style_preferences：
    - language_style: 来自 Persona.language_style（如"克制、书面、避免口语"）
    - format_preferences: 来自 Persona.format_preferences + ProjectSkill.format_defaults
    - voice: 来自 ProjectSkill.style_preferences.voice（如"专业但温和"）
    - avoidances: 来自 Persona.avoidances
    - project_id: 风格归属的项目（None 表示全局）
    """

    ready: bool
    language_style: tuple[str, ...] = ()
    format_preferences: tuple[str, ...] = ()
    voice: str | None = None
    avoidances: tuple[str, ...] = ()
    project_id: str | None = None
    persona_scope: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "language_style": list(self.language_style),
            "format_preferences": list(self.format_preferences),
            "voice": self.voice,
            "avoidances": list(self.avoidances),
            "project_id": self.project_id,
            "persona_scope": self.persona_scope,
        }

    def render_prompt_prefix(self) -> str:
        """渲染为提示词前缀，prepend 到所有模板生成的 system prompt 前。

        无任何风格字段时返回空字符串，调用方无需判空。
        """
        if not self.ready:
            return ""
        lines: list[str] = []
        if self.voice:
            lines.append(f"语调：{self.voice}")
        if self.language_style:
            lines.append("语言风格：" + "；".join(self.language_style))
        if self.format_preferences:
            lines.append("格式偏好：" + "；".join(self.format_preferences))
        if self.avoidances:
            lines.append("需要避免：" + "；".join(self.avoidances))
        if not lines:
            return ""
        header = "统一输出范式（用户个人风格，必须遵循）："
        return header + "\n" + "\n".join(lines)


@dataclass(frozen=True, slots=True)
class StyleProfileService:
    """构建 StyleProfile 的服务。

    读取 Persona + ProjectSkill，合并为统一风格档案。
    project_id 为 None 时只读全局 Persona，不读 ProjectSkill。
    """

    object_store: ObjectStorePort
    persona_repository: ObjectStorePersonaRepository | None = None
    project_skill_collection: str = "project_skills"

    def build(self, *, project_id: str | None = None, persona_scope: str | None = None) -> StyleProfile:
        digest = self._persona_digest(persona_scope or "global")
        skill = self._load_project_skill(project_id) if project_id else None
        return self._merge(digest=digest, skill=skill, project_id=project_id, persona_scope=persona_scope)

    def _persona_repository(self) -> ObjectStorePersonaRepository:
        if self.persona_repository is not None:
            return self.persona_repository
        return ObjectStorePersonaRepository(object_store=self.object_store)

    def _persona_digest(self, scope: str) -> PersonaDigest:
        return self._persona_repository().digest(scope)

    def _load_project_skill(self, project_id: str) -> Mapping[str, object] | None:
        if not project_id or project_id == "default":
            return None
        # ProjectSkill 主键与 project_id 一致（runtime.py 中 save 用 project_id 作为 key）
        return self.object_store.read(self.project_skill_collection, project_id)

    def _merge(
        self,
        *,
        digest: PersonaDigest,
        skill: Mapping[str, object] | None,
        project_id: str | None,
        persona_scope: str | None,
    ) -> StyleProfile:
        language_style: tuple[str, ...] = digest.language_style if digest.ready else ()
        format_prefs: tuple[str, ...] = digest.format_preferences if digest.ready else ()
        avoidances: tuple[str, ...] = digest.avoidances if digest.ready else ()

        voice: str | None = None
        skill_format_defaults: tuple[str, ...] = ()
        if skill is not None:
            style_prefs = skill.get("style_preferences")
            if isinstance(style_prefs, Mapping):
                voice = _opt_str(style_prefs.get("voice"))
                skill_format_defaults = _str_tuple(style_prefs.get("format_defaults"))

        # format_preferences 合并去重：Persona 在前，ProjectSkill 补充
        merged_format = _dedup((*format_prefs, *skill_format_defaults))

        ready = bool(language_style or merged_format or voice or avoidances)
        return StyleProfile(
            ready=ready,
            language_style=language_style,
            format_preferences=merged_format,
            voice=voice,
            avoidances=avoidances,
            project_id=project_id,
            persona_scope=persona_scope,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) and not isinstance(value, tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _opt_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _dedup(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)
