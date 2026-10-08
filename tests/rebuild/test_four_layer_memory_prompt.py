from __future__ import annotations

import pytest

from core.product_core import (
    FOUR_LAYER_MEMORY_SPECS,
    FourLayerMemoryPromptError,
    build_four_layer_memory_prompt,
    serialize_four_layer_memory_prompt,
)
from core.product_core.four_layer_memory_prompt import FOUR_LAYER_MEMORY_PROMPT_VERSION


def test_four_layer_memory_prompt_exposes_all_candidate_layers() -> None:
    prompt = build_four_layer_memory_prompt(
        source_kind="transcript_summary",
        source_summary="视频总结已经完成，包含产品设计、视频处理和记忆沉淀建议。",
    )
    payload = serialize_four_layer_memory_prompt(prompt)

    assert prompt.prompt_version == FOUR_LAYER_MEMORY_PROMPT_VERSION
    assert payload["prompt_version"] == FOUR_LAYER_MEMORY_PROMPT_VERSION
    assert f"Prompt版本：{FOUR_LAYER_MEMORY_PROMPT_VERSION}" in prompt.system_prompt

    assert [spec.target_layer for spec in prompt.layer_specs] == [
        "atom",
        "scenario",
        "series_memory",
        "project_skill",
    ]
    assert payload["output_contract"]["allowed_target_layers"] == [
        "atom",
        "scenario",
        "series_memory",
        "project_skill",
    ]
    assert "target_layer=atom" in prompt.system_prompt
    assert "target_layer=scenario" in prompt.system_prompt
    assert "target_layer=series_memory" in prompt.system_prompt
    assert "target_layer=project_skill" in prompt.system_prompt
    assert len(FOUR_LAYER_MEMORY_SPECS) == 4


def test_four_layer_memory_prompt_is_candidate_only_not_publication() -> None:
    prompt = build_four_layer_memory_prompt(
        source_kind="docx_content_read",
        source_summary="用户授权的产品设计文档正文已读取。",
    )
    contract = prompt.output_contract
    review_contract = contract["review_contract"]

    assert review_contract == {
        "status": "pending_review",
        "requires_user_confirmation": True,
        "auto_promote_allowed": False,
        "published_memory_allowed": False,
    }
    assert "不能写入、发布、撤回或修改长期 Memory" in prompt.system_prompt
    assert "status 必须是 pending_review" in prompt.system_prompt
    assert "review.auto_promote_allowed 必须是 false" in prompt.system_prompt
    assert "memory_publications" in contract["forbidden_outputs"]
    assert "memory_transitions" in contract["forbidden_outputs"]
    assert "publish_memory" in prompt.provider_boundary["provider_must_not"]
    assert "write_long_term_memory" in prompt.provider_boundary["provider_must_not"]


def test_four_layer_memory_prompt_keeps_provider_secret_and_file_boundaries() -> None:
    prompt = build_four_layer_memory_prompt(
        source_kind="external_ai_summary",
        source_summary="由外部模型返回的摘要文本，已在本地去除敏感字段。",
    )

    assert "不输出 API key、Cookie、完整本地路径" in prompt.system_prompt
    assert "不调用外部工具，不下载视频，不读取文件" in prompt.system_prompt
    assert "api_keys" in prompt.output_contract["forbidden_outputs"]
    assert "cookies" in prompt.output_contract["forbidden_outputs"]
    assert "absolute_local_paths" in prompt.output_contract["forbidden_outputs"]
    assert "log_or_return_secrets" in prompt.provider_boundary["provider_must_not"]
    assert "read_local_files" in prompt.provider_boundary["provider_must_not"]


def test_four_layer_memory_prompt_can_scope_to_project_skill_layer() -> None:
    prompt = build_four_layer_memory_prompt(
        source_kind="reviewed_workflow",
        source_summary="用户确认某套视频处理流程可以复用。",
        allowed_layers=["project_skill"],
    )

    assert [spec.target_layer for spec in prompt.layer_specs] == ["project_skill"]
    assert prompt.output_contract["allowed_target_layers"] == ["project_skill"]
    assert "target_layer=project_skill" in prompt.system_prompt
    assert "target_layer=atom" not in prompt.system_prompt


def test_four_layer_memory_prompt_rejects_empty_or_unknown_inputs() -> None:
    with pytest.raises(FourLayerMemoryPromptError, match="source_kind is required"):
        build_four_layer_memory_prompt(source_kind="", source_summary="摘要")
    with pytest.raises(FourLayerMemoryPromptError, match="source_summary is required"):
        build_four_layer_memory_prompt(source_kind="summary", source_summary=" ")
    with pytest.raises(FourLayerMemoryPromptError, match="allowed_layers did not match"):
        build_four_layer_memory_prompt(
            source_kind="summary",
            source_summary="摘要",
            allowed_layers=["persona"],
        )
