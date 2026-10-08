"""R134 集成测试：outline 章节树注入 + ProjectSkill.outline 覆盖。

覆盖：
- 默认 outline 渲染（answer_manual/review/project_summary 各有固定结构）
- ProjectSkill.outline 覆盖默认 outline
- outline_override 传入 creator 时的校验（非法 outline 报错）
- 隐私：渲染结果无敏感串
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.document_engine import ObjectStoreDocumentRepository
from core.product_core.outline import Outline
from core.product_core.source_template_document import (
    CreateSourceTemplateDocument,
    SourceTemplateDocumentError,
)
from core.project_skill_core.runtime import (
    ObjectStoreProjectSkillRepository,
    ProjectSkillUpdate,
)
from core.storage_provider import JsonObjectStore, RebuildStorageSettings


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _settings(tmp_path: Path) -> RebuildStorageSettings:
    """Match the temporary JSON store root while exercising the route factory contract."""
    return RebuildStorageSettings.from_mapping({}, repository_root=tmp_path)


def _publish_skill(
    store: JsonObjectStore,
    *,
    project_id: str = "project-alpha",
    outline: list | None = None,
) -> None:
    """发布一个 ProjectSkill，可选带 outline 字段。"""
    structured: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": f"skill-{project_id}",
        "project_id": project_id,
        "name": f"{project_id} Skill",
        "purpose": "测试用 Skill",
        "markdown_uri": f"crp://default/projects/{project_id}/project-skill.md",
        "json_uri": f"crp://default/projects/{project_id}/project-skill.json",
        "markdown_revision": 1,
        "json_revision": 1,
        "required_context": [
            {
                "context_id": "ctx-1",
                "kind": "source",
                "object_id": "source-1",
                "uri": "crp://default/sources/source-1.json",
                "reason": "测试上下文",
                "stale": False,
            }
        ],
        "output_rules": [
            {
                "rule_id": "rule-1",
                "origin": "user",
                "rule": "使用 Markdown",
                "priority": "must",
                "source_refs": [{"source_id": "source-1", "locator": "char:0-100"}],
                "locked_by_user": False,
            }
        ],
        "style_preferences": {
            "voice": "克制、温柔",
            "format_defaults": ["Markdown", "短段落"],
        },
        "update_rules": {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": ["refresh_stale_refs"],
        },
        "source_refs": [{"source_id": "source-1", "locator": "char:0-100"}],
        "evidence_refs": [{"source_id": "source-1", "locator": "char:0-100"}],
        "decision_log": [
            {
                "decision_id": "dec-1",
                "reason": "初始化",
                "actor": "user",
                "created_at": "2026-07-04T09:00:00+08:00",
            }
        ],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "revision": 1,
        "status": "active",
        "trust_status": "user_confirmed",
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    if outline is not None:
        structured["outline"] = outline
    repo = ObjectStoreProjectSkillRepository(object_store=store)
    repo.save(
        ProjectSkillUpdate(
            project_id=project_id,
            markdown="# Alpha Skill\n\n测试。",
            structured=structured,
            expected_revision=0,
            reason="测试初始化",
        )
    )


def _source_with_structure(
    *,
    source_id: str = "source-struct-1",
    project_id: str | None = None,
    title: str = "结构化资料",
    summary: str = "这是结构化摘要。",
    key_points: tuple[str, ...] = ("要点一", "要点二"),
    structured_body: str = "结构化正文段落。",
) -> dict[str, object]:
    source: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": source_id,
        "source_type": "link",
        "title": title,
        "content": "原始正文内容。",
        "metadata": {
            "content_structure": {
                "status": "completed",
                "summary": summary,
                "key_points": list(key_points),
                "structured_body": structured_body,
                "structure_ref": f"crp://default/source-structures/structure-{source_id}.json",
            },
            "series_assignment": {"series_name": "测试系列"},
        },
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    if project_id:
        source["project_id"] = project_id
    return source


# ---------------------------------------------------------------------------
# 默认 outline 渲染
# ---------------------------------------------------------------------------


def test_answer_manual_default_outline_renders_conclusion_and_evidence(tmp_path: Path) -> None:
    """answer_manual 默认 outline 应渲染"结论"和"依据"章节。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    result = creator.execute(source_id="source-1", template_type="answer_manual")
    markdown = documents.markdown(result.document_id)
    assert markdown is not None
    assert "## 结论" in markdown
    assert "## 依据" in markdown
    assert "## 追溯来源" in markdown
    assert "## 待确认" in markdown
    # 不应出现 review 的章节
    assert "## 背景" not in markdown
    assert "## 判断" not in markdown


def test_review_default_outline_renders_background_and_judgment(tmp_path: Path) -> None:
    """review 默认 outline 应渲染"背景"和"判断"章节。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    result = creator.execute(source_id="source-1", template_type="review")
    markdown = documents.markdown(result.document_id)
    assert markdown is not None
    assert "## 背景" in markdown
    assert "## 关键事实" in markdown
    assert "## 判断" in markdown
    assert "## 来源" in markdown
    # 不应出现 answer_manual 的章节
    assert "## 结论" not in markdown
    assert "## 依据" not in markdown


def test_project_summary_default_outline_renders_goal_and_progress(tmp_path: Path) -> None:
    """project_summary 默认 outline 应渲染"目标"和"当前进展"章节。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    result = creator.execute(source_id="source-1", template_type="project_summary")
    markdown = documents.markdown(result.document_id)
    assert markdown is not None
    assert "## 目标" in markdown
    assert "## 当前进展" in markdown
    assert "## 来源" in markdown
    # 不应出现 review 的章节
    assert "## 背景" not in markdown
    assert "## 判断" not in markdown


def test_media_summary_default_outline_renders_core_summary_and_media_body(tmp_path: Path) -> None:
    """media_summary 默认 outline 应渲染"核心摘要"、"关键结论"和"媒体正文"章节。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    result = creator.execute(source_id="source-1", template_type="media_summary")
    markdown = documents.markdown(result.document_id)
    assert markdown is not None
    assert "## 核心摘要" in markdown
    assert "## 关键结论" in markdown
    assert "## 媒体正文" in markdown
    assert "## 待确认" in markdown
    assert "## 追溯来源" in markdown
    # 不应出现 answer_manual / review / project_summary 的章节
    assert "## 结论" not in markdown
    assert "## 依据" not in markdown
    assert "## 背景" not in markdown
    assert "## 目标" not in markdown


# ---------------------------------------------------------------------------
# ProjectSkill.outline 覆盖
# ---------------------------------------------------------------------------


def test_project_skill_outline_overrides_default(tmp_path: Path) -> None:
    """ProjectSkill.outline 覆盖默认 outline。"""
    store = _store(tmp_path)
    _publish_skill(
        store,
        project_id="project-alpha",
        outline=[
            {"section_id": "intro", "title": "引言", "kind": "summary", "required": True},
            {"section_id": "main", "title": "主体内容", "kind": "body", "required": True},
            {"section_id": "refs", "title": "参考文献", "kind": "sources", "required": True},
        ],
    )
    store.write(
        "sources",
        "source-1",
        _source_with_structure(project_id="project-alpha"),
        expected_revision=None,
    )
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    # 模拟路由读取 ProjectSkill.outline 并传入
    from backend.api.routes.product.document_templates import _outline_override_for_source

    override = _outline_override_for_source(tmp_path, store, _settings(tmp_path), "source-1")
    assert override is not None
    result = creator.execute(
        source_id="source-1",
        template_type="answer_manual",
        outline_override=override,
    )
    markdown = documents.markdown(result.document_id)
    assert markdown is not None
    # 覆盖后的章节
    assert "## 引言" in markdown
    assert "## 主体内容" in markdown
    assert "## 参考文献" in markdown
    # 默认 outline 的章节不应出现
    assert "## 结论" not in markdown
    assert "## 依据" not in markdown


def test_no_project_skill_uses_default_outline(tmp_path: Path) -> None:
    """无 ProjectSkill 时，路由 helper 返回 None，用默认 outline。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    from backend.api.routes.product.document_templates import _outline_override_for_source

    override = _outline_override_for_source(tmp_path, store, _settings(tmp_path), "source-1")
    assert override is None


def test_project_skill_without_outline_returns_none(tmp_path: Path) -> None:
    """ProjectSkill 无 outline 字段时，helper 返回 None。"""
    store = _store(tmp_path)
    _publish_skill(store, project_id="project-alpha", outline=None)
    store.write(
        "sources",
        "source-1",
        _source_with_structure(project_id="project-alpha"),
        expected_revision=None,
    )
    from backend.api.routes.product.document_templates import _outline_override_for_source

    override = _outline_override_for_source(tmp_path, store, _settings(tmp_path), "source-1")
    assert override is None


# ---------------------------------------------------------------------------
# outline 校验
# ---------------------------------------------------------------------------


def test_invalid_outline_override_raises_error(tmp_path: Path) -> None:
    """非法 outline（缺字段）应报错。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    with pytest.raises(SourceTemplateDocumentError, match="missing non-empty"):
        creator.execute(
            source_id="source-1",
            template_type="answer_manual",
            outline_override=[{"title": "缺 section_id", "kind": "summary"}],
        )


def test_unsupported_kind_raises_error(tmp_path: Path) -> None:
    """不支持的 kind 应报错。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    with pytest.raises(SourceTemplateDocumentError, match="unsupported kind"):
        creator.execute(
            source_id="source-1",
            template_type="answer_manual",
            outline_override=[
                {"section_id": "s1", "title": "T", "kind": "unknown_kind", "required": True}
            ],
        )


# ---------------------------------------------------------------------------
# 隐私
# ---------------------------------------------------------------------------


def test_outline_render_excludes_secrets(tmp_path: Path) -> None:
    """渲染结果不应含敏感凭据串。"""
    store = _store(tmp_path)
    store.write("sources", "source-1", _source_with_structure(), expected_revision=None)
    documents = ObjectStoreDocumentRepository(store)
    creator = CreateSourceTemplateDocument(
        object_store=store,
        documents=documents,
    )
    result = creator.execute(source_id="source-1", template_type="answer_manual")
    markdown = documents.markdown(result.document_id) or ""
    lowered = markdown.lower()
    assert "sk-" not in lowered
    assert "cookie" not in lowered
    assert "authorization" not in lowered
    assert "password" not in lowered
    assert "token" not in lowered
