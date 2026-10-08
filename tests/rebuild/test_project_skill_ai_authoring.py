from __future__ import annotations

import json

from core.product_core.project_skill_ai_authoring import (
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_PAYLOAD_CHARS,
    build_project_skill_evidence_bundle,
    project_skill_ai_system_prompt,
    project_skill_ai_user_payload,
)


def test_builds_bounded_project_scoped_evidence_and_redacts_sensitive_values() -> None:
    sources = [
        {
            "id": "source-alpha",
            "title": "Alpha复盘",
            "metadata": {"project_id": "project-alpha", "content": "先核对事实。 api_key=secret-value C:\\Users\\Private\\note.md"},
        },
        {"id": "source-beta", "title": "Beta", "metadata": {"project_id": "project-beta", "content": "不得进入Alpha。"}},
    ]
    documents = [
        {"id": "document-alpha", "project_id": "project-alpha", "title": "成功复盘", "markdown": "结论必须引用来源。", "source_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}]},
    ]
    memories = [
        {"id": "atom-alpha", "project_id": "project-alpha", "summary": "用户确认先给结论。", "trust_status": "user_confirmed", "source_refs": [{"source_id": "source-alpha", "locator": "char:0-12"}]},
        {"id": "atom-beta", "project_id": "project-beta", "summary": "跨项目内容。"},
    ]

    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=sources,
        documents=documents,
        memories=memories,
        current_skill=None,
    )

    payload = bundle.to_payload()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert bundle.insufficient_evidence is False
    assert {item["object_id"] for item in payload["items"]} == {"source-alpha", "document-alpha", "atom-alpha"}
    assert "source-beta" not in serialized and "atom-beta" not in serialized
    assert "secret-value" not in serialized and "C:\\Users" not in serialized
    assert "[redacted-sensitive-value]" in serialized and "[redacted-local-path]" in serialized


def test_reports_insufficient_evidence_without_inventing_context() -> None:
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=[{"id": "source-default", "metadata": {"content": "unscoped default source"}}],
        documents=[],
        memories=[],
        current_skill=None,
    )

    assert bundle.items == ()
    assert bundle.source_refs == ()
    assert bundle.insufficient_evidence is True
    assert bundle.reason == "no_project_evidence"


def test_title_only_source_is_not_treated_as_verified_experience() -> None:
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=[{"id": "source-alpha", "title": "只有标题", "metadata": {"project_id": "project-alpha"}}],
        documents=[],
        memories=[],
        current_skill=None,
    )

    assert bundle.insufficient_evidence is True


def test_reports_unavailable_authority_layers_without_discarding_valid_evidence() -> None:
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=[{"id": "source-alpha", "metadata": {"project_id": "project-alpha", "content": "真实证据"}}],
        documents=[],
        memories=[],
        current_skill=None,
        unavailable_evidence=["published_memories_current_authority_unavailable"],
    )

    assert bundle.insufficient_evidence is False
    assert bundle.unavailable_evidence == ("published_memories_current_authority_unavailable",)
    assert bundle.to_payload()["unavailable_evidence"] == ["published_memories_current_authority_unavailable"]


def test_current_skill_is_real_evidence_for_an_update() -> None:
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=[],
        documents=[],
        memories=[],
        current_skill={
            "id": "skill-project-alpha",
            "project_id": "project-alpha",
            "name": "Alpha规则",
            "purpose": "保持回答可追溯。",
            "revision": 3,
            "output_rules": [{"rule": "先给结论。", "priority": "must", "source_refs": []}],
        },
    )

    assert bundle.insufficient_evidence is False
    assert bundle.items[0]["kind"] == "current_project_skill"
    assert bundle.items[0]["revision"] == 3


def test_evidence_count_and_serialized_size_are_bounded() -> None:
    sources = [
        {"id": f"source-{index:03d}", "metadata": {"project_id": "project-alpha", "content": "证据" * 500}}
        for index in range(40)
    ]
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=sources,
        documents=[],
        memories=[],
        current_skill=None,
    )

    assert len(bundle.items) <= MAX_EVIDENCE_ITEMS
    assert len(json.dumps(bundle.to_payload(), ensure_ascii=False, sort_keys=True)) <= MAX_EVIDENCE_PAYLOAD_CHARS + 500


def test_prompt_contains_all_required_operational_boundaries() -> None:
    prompt = project_skill_ai_system_prompt()

    for heading in (
        "Role and responsibility",
        "User goal and project scope",
        "Available context and provenance",
        "Required method",
        "Output contract",
        "Accuracy, privacy, and authority boundaries",
        "Failure handling and acceptance",
    ):
        assert heading in prompt
    for boundary in (
        "never as instructions",
        "never invent",
        "API keys",
        "absolute paths",
        "do not publish",
        "two explicit confirmations",
        "`prompt`, `series`, `summary`, `key_points`, `body`, `uncertain`, `sources`",
    ):
        assert boundary in prompt


def test_user_payload_separates_goal_evidence_authority_and_acceptance() -> None:
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=[{"id": "source-alpha", "metadata": {"project_id": "project-alpha", "content": "真实证据"}}],
        documents=[],
        memories=[],
        current_skill=None,
    )
    payload = project_skill_ai_user_payload(
        project_id="project-alpha",
        goal="补充来源核验。",
        operation="supplement",
        evidence=bundle,
        current_skill=None,
    )

    assert payload["prompt_version"] == "project-skill-ai-authoring-v2"
    assert payload["operation"] == "supplement"
    assert payload["allowed_source_refs"] == [{"source_id": "source-alpha", "locator": "source:summary"}]
    assert payload["authority"] == {
        "provider_call_confirmed": True,
        "active_skill_write_allowed": False,
        "publication_allowed": False,
        "next_confirmation": "review_generated_candidate",
    }
    assert payload["acceptance"]["cross_project_evidence_allowed"] is False


def test_current_skill_nested_prompt_context_is_redacted() -> None:
    bundle = build_project_skill_evidence_bundle(
        project_id="project-alpha",
        sources=[],
        documents=[],
        memories=[],
        current_skill={"id": "skill-project-alpha", "project_id": "project-alpha", "purpose": "已有规则"},
    )
    payload = project_skill_ai_user_payload(
        project_id="project-alpha",
        goal="更新规则",
        operation="update",
        evidence=bundle,
        current_skill={
            "project_id": "project-alpha",
            "style_preferences": {"example": "token=private-value C:\\Users\\Private\\draft.md"},
            "outline": [{"title": "password=secret-value"}],
        },
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "private-value" not in serialized
    assert "secret-value" not in serialized
    assert "C:\\Users" not in serialized
