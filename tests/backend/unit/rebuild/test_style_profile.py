from __future__ import annotations

from pathlib import Path

from core.product_core import (
    ObjectStorePersonaRepository,
    PersonaExtractor,
)
from core.product_core.style_profile import (
    StyleProfile,
    StyleProfileService,
)
from core.storage_provider import JsonObjectStore


# ── 测试夹具 ──


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _confirmed_atom(
    *,
    atom_id: str = "atom-alpha",
    project_id: str = "project-alpha",
    language_style: str | None = "克制、书面、避免命令式",
    format_preferences: tuple[str, ...] = ("Markdown", "短段落"),
    avoidances: tuple[str, ...] = ("不要使用 emoji",),
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": atom_id,
        "layer": "atom",
        "project_id": project_id,
        "content": "已确认的 Atom 内容。",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    if language_style:
        payload["language_style"] = language_style
    if format_preferences:
        payload["format_preferences"] = list(format_preferences)
    if avoidances:
        payload["avoidances"] = list(avoidances)
    return payload


def _publish_persona(
    store: JsonObjectStore,
    *,
    scope: str = "global",
    confirmed_entries: list[dict[str, object]] | None = None,
) -> None:
    """通过 PersonaExtractor + Repository 写入并确认一条真实 Persona 记录。"""
    extractor = PersonaExtractor()
    record = extractor.extract(
        scope=scope,
        confirmed_entries=confirmed_entries or [_confirmed_atom()],
    )
    repo = ObjectStorePersonaRepository(store)
    repo.save(record)  # pending draft
    repo.update_confirmation(scope, status="confirmed", reason="测试夹具确认 Persona")


def _project_skill(
    *,
    project_id: str = "project-alpha",
    voice: str | None = "直接、具体、可执行",
    format_defaults: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": f"skill-{project_id}",
        "project_id": project_id,
        "name": f"{project_id} 项目 Skill",
        "purpose": "测试用项目 Skill。",
        "markdown_uri": f"crp://default/projects/{project_id}/project-skill.md",
        "json_uri": f"crp://default/projects/{project_id}/project-skill.json",
        "markdown_revision": 1,
        "json_revision": 1,
        "required_context": [],
        "output_rules": [],
        "style_preferences": {
            "voice": voice,
            "format_defaults": format_defaults if format_defaults is not None else ["Markdown", "分节标题"],
        },
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": [],
        },
        "source_refs": [],
        "evidence_refs": [],
        "decision_log": [],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "revision": 1,
        "status": "active",
        "trust_status": "user_confirmed",
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:10:00+08:00",
    }


# ── StyleProfile dataclass ──


def test_style_profile_render_prompt_prefix_returns_empty_when_not_ready() -> None:
    profile = StyleProfile(ready=False)
    assert profile.render_prompt_prefix() == ""


def test_style_profile_render_prompt_prefix_returns_empty_when_all_fields_blank() -> None:
    profile = StyleProfile(ready=True)
    assert profile.render_prompt_prefix() == ""


def test_style_profile_render_prompt_prefix_includes_voice() -> None:
    profile = StyleProfile(ready=True, voice="专业但温和")
    text = profile.render_prompt_prefix()
    assert "统一输出范式（用户个人风格，必须遵循）：" in text
    assert "语调：专业但温和" in text


def test_style_profile_render_prompt_prefix_includes_all_fields() -> None:
    profile = StyleProfile(
        ready=True,
        voice="直接",
        language_style=("克制", "书面"),
        format_preferences=("Markdown", "短段落"),
        avoidances=("不用 emoji", "不编造"),
    )
    text = profile.render_prompt_prefix()
    assert "统一输出范式（用户个人风格，必须遵循）：" in text
    assert "语调：直接" in text
    assert "语言风格：克制；书面" in text
    assert "格式偏好：Markdown；短段落" in text
    assert "需要避免：不用 emoji；不编造" in text


def test_style_profile_to_payload_serializes_fields() -> None:
    profile = StyleProfile(
        ready=True,
        voice="直接",
        language_style=("克制",),
        format_preferences=("Markdown",),
        avoidances=("不用 emoji",),
        project_id="project-alpha",
        persona_scope="global",
    )
    payload = profile.to_payload()
    assert payload["ready"] is True
    assert payload["voice"] == "直接"
    assert payload["language_style"] == ["克制"]
    assert payload["format_preferences"] == ["Markdown"]
    assert payload["avoidances"] == ["不用 emoji"]
    assert payload["project_id"] == "project-alpha"
    assert payload["persona_scope"] == "global"


# ── StyleProfileService.build —— 基础场景 ──


def test_service_build_returns_not_ready_when_no_persona_no_skill(tmp_path: Path) -> None:
    service = StyleProfileService(object_store=_store(tmp_path))
    profile = service.build()
    assert profile.ready is False
    assert profile.render_prompt_prefix() == ""


def test_service_build_includes_persona_language_style(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    service = StyleProfileService(object_store=store)
    profile = service.build()
    assert profile.ready is True
    assert "克制、书面、避免命令式" in profile.language_style
    text = profile.render_prompt_prefix()
    assert "语言风格：克制、书面、避免命令式" in text
    assert "格式偏好：Markdown；短段落" in text
    assert "需要避免：不要使用 emoji" in text


def test_service_build_returns_persona_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store, scope="global")
    service = StyleProfileService(object_store=store)
    profile = service.build(persona_scope="global")
    assert profile.persona_scope == "global"


# ── StyleProfileService.build —— ProjectSkill 合并 ──


def test_service_build_includes_project_skill_voice_and_format_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    store.write("project_skills", "project-alpha", _project_skill(), expected_revision=None)

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    assert profile.ready is True
    assert profile.voice == "直接、具体、可执行"
    # Persona 的 format_preferences + ProjectSkill 的 format_defaults 去重合并
    assert "Markdown" in profile.format_preferences
    assert "短段落" in profile.format_preferences
    assert "分节标题" in profile.format_preferences
    text = profile.render_prompt_prefix()
    assert "语调：直接、具体、可执行" in text
    assert "格式偏好：Markdown；短段落；分节标题" in text


def test_service_build_deduplicates_format_preferences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)  # Persona 的 format_preferences = ["Markdown", "短段落"]
    store.write(
        "project_skills",
        "project-alpha",
        _project_skill(format_defaults=["Markdown", "分节标题", "来源引用"]),
        expected_revision=None,
    )

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    # Markdown 只出现一次（去重）
    assert profile.format_preferences.count("Markdown") == 1
    assert "短段落" in profile.format_preferences
    assert "分节标题" in profile.format_preferences
    assert "来源引用" in profile.format_preferences


def test_service_build_keeps_persona_order_before_skill(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    store.write(
        "project_skills",
        "project-alpha",
        _project_skill(format_defaults=["附录", "分节标题"]),
        expected_revision=None,
    )

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    # Persona 的在前
    assert profile.format_preferences.index("Markdown") < profile.format_preferences.index("附录")
    assert profile.format_preferences.index("短段落") < profile.format_preferences.index("分节标题")


def test_service_build_skips_default_project_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    store.write("project_skills", "default", _project_skill(project_id="default"), expected_revision=None)

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="default")
    # default 项目不会读取 ProjectSkill，所以没有 voice
    assert profile.voice is None
    assert profile.ready is True  # 但 Persona 仍生效


def test_service_build_with_skill_only_no_persona(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("project_skills", "project-alpha", _project_skill(), expected_revision=None)

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    assert profile.ready is True
    assert profile.voice == "直接、具体、可执行"
    assert "Markdown" in profile.format_preferences
    assert profile.language_style == ()
    assert profile.avoidances == ()


def test_service_build_handles_skill_without_style_preferences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    skill_payload = _project_skill()
    del skill_payload["style_preferences"]
    store.write("project_skills", "project-alpha", skill_payload, expected_revision=None)

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    assert profile.ready is True
    assert profile.voice is None
    # Persona 的 format_preferences 仍然存在
    assert "Markdown" in profile.format_preferences


def test_service_build_handles_skill_with_empty_voice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    store.write(
        "project_skills",
        "project-alpha",
        _project_skill(voice="   "),
        expected_revision=None,
    )

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    assert profile.voice is None
    assert profile.ready is True


def test_service_build_handles_skill_with_non_string_voice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    skill_payload = _project_skill()
    skill_payload["style_preferences"]["voice"] = 123  # 非字符串
    store.write("project_skills", "project-alpha", skill_payload, expected_revision=None)

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    assert profile.voice is None


def test_service_build_handles_skill_with_non_list_format_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    skill_payload = _project_skill()
    skill_payload["style_preferences"]["format_defaults"] = "Markdown"  # 不是 list
    store.write("project_skills", "project-alpha", skill_payload, expected_revision=None)

    service = StyleProfileService(object_store=store)
    profile = service.build(project_id="project-alpha")
    # 非 list 的 format_defaults 被忽略，只剩 Persona 的
    assert profile.format_preferences == ("Markdown", "短段落")


# ── StyleProfileService —— persona_repository 注入 ──


def test_service_uses_injected_persona_repository(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    repo = ObjectStorePersonaRepository(store)
    service = StyleProfileService(object_store=store, persona_repository=repo)
    profile = service.build()
    assert profile.ready is True
    assert "克制、书面、避免命令式" in profile.language_style


# ── 隐私 / 安全：提示词前缀不应泄露敏感字段 ──


def test_render_prompt_prefix_does_not_leak_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _publish_persona(store)
    service = StyleProfileService(object_store=store)
    profile = service.build()
    text = profile.render_prompt_prefix()
    # 不应包含常见敏感字符串
    assert "sk-" not in text.lower()
    assert "cookie" not in text.lower()
    assert "authorization" not in text.lower()
    assert "password" not in text.lower()
    assert "token" not in text.lower()
