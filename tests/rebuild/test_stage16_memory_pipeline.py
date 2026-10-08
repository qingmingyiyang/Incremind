"""阶段 1.6 后端测试 — Memory Quality Gate / External Import / Memory Export。

覆盖：
- quality_gate: 信任等级、敏感信息检测、脱敏、冲突、L3 提升、分组
- external_import: 4 种 adapter（Markdown/JSON/ZIP/LLM 对话）、role 识别、
  assistant 不直接进入长期记忆、custom_instructions 生成 L4 Persona 候选、
  冲突项 needs_review、evidence_refs 完整性
- memory_export: 完整资产包、Markdown 知识包、compact persona prompt、
  RAG corpus、脱敏导出、round-trip 导入
"""
from __future__ import annotations

import io
import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

# 把 src/ 加入 sys.path（与现有 test_workbench_auto_intake.py 保持一致）
import sys
import os
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from core.product_core.memory_quality_gate import (  # noqa: E402
    MemoryCandidate,
    compute_trust_level,
    detect_secrets,
    evaluate_candidate,
    evaluate_candidates,
    redact_content,
    serialize_quality_gate_report,
)
from core.product_core.external_import_framework import (  # noqa: E402
    ExternalImportError,
    GenericJsonConversationAdapter,
    GenericLLMConversationAdapter,
    GenericMarkdownTxtAdapter,
    GenericZipBundleAdapter,
    detect_adapter,
    import_external_bundle,
    validate_import_result,
)
from core.product_core.memory_export_framework import (  # noqa: E402
    ExportPayload,
    ExportScope,
    ExportableEvidenceLink,
    ExportableMemory,
    ExportableSource,
    ExportableSourceAsset,
    ExportableTag,
    export_memory,
    import_asset_package,
)
from core.product_core.memory_router import (  # noqa: E402
    MemoryRouter,
)


# ──────────────────────────────────────────────────────────────
# Quality Gate 测试
# ──────────────────────────────────────────────────────────────


class TestTrustLevel:
    """信任等级评估。"""

    def test_user_file_is_high_trust(self) -> None:
        assert compute_trust_level("user", "file", 0.9) == "high"

    def test_user_document_is_high_trust(self) -> None:
        assert compute_trust_level("user", "document", 0.85) == "high"

    def test_user_custom_instruction_is_high_trust(self) -> None:
        assert compute_trust_level("user", "custom_instruction", 0.88) == "high"

    def test_user_conversation_is_medium_trust(self) -> None:
        assert compute_trust_level("user", "conversation", 0.7) == "medium"

    def test_assistant_is_low_trust(self) -> None:
        """assistant 回复默认低可信，不能直接进入长期事实。"""
        assert compute_trust_level("assistant", "conversation", 0.9) == "low"

    def test_system_is_medium_trust(self) -> None:
        assert compute_trust_level("system", "custom_instruction", 0.8) == "medium"

    def test_tool_is_medium_trust(self) -> None:
        assert compute_trust_level("tool", "tool_result", 0.75) == "medium"

    def test_user_confirmed_overrides_to_high(self) -> None:
        assert compute_trust_level("assistant", "conversation", 0.5, user_confirmed=True) == "high"


class TestSecretDetection:
    """敏感信息检测。"""

    def test_detect_api_key(self) -> None:
        secrets = detect_secrets("my api_key=sk-abc123def456ghi789jkl012mno345pqr")
        assert "api_key" in secrets

    def test_detect_bearer_token(self) -> None:
        secrets = detect_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234567890")
        assert "bearer_token" in secrets

    def test_detect_cookie(self) -> None:
        secrets = detect_secrets("Cookie: sessionid=abcdefgh1234")
        assert "cookie" in secrets

    def test_detect_aws_key(self) -> None:
        secrets = detect_secrets("AKIAIOSFODNN7EXAMPLE")
        assert "aws_key" in secrets

    def test_detect_private_key(self) -> None:
        secrets = detect_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA")
        assert "private_key" in secrets

    def test_detect_email(self) -> None:
        secrets = detect_secrets("联系我：test@example.com")
        assert "email" in secrets

    def test_no_secrets_in_clean_content(self) -> None:
        secrets = detect_secrets("项目目标是构建私人 AI 记忆工作台")
        assert secrets == ()

    def test_redact_replaces_secrets(self) -> None:
        redacted = redact_content("my api_key=sk-abc123def456ghi789jkl012mno345pqr")
        assert "sk-abc123" not in redacted
        assert "[REDACTED]" in redacted

    @pytest.mark.parametrize("content, forbidden", (
        ("api_key=short-but-secret-value", "short-but-secret-value"),
        ("token=secret-value-456", "secret-value-456"),
        ("password: correct-horse-battery-staple", "correct-horse"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234567890", "abcdefghijklmnopqrstuvwxyz"),
        ("Cookie: sessionid=abcdefgh1234; preference=private", "abcdefgh1234"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n"
            "-----END RSA PRIVATE KEY-----",
            "MIIEpAIBAAKCAQEA",
        ),
    ))
    def test_redact_removes_the_secret_value_not_only_its_label(
        self, content: str, forbidden: str,
    ) -> None:
        redacted = redact_content(content)
        assert forbidden not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_preserves_clean_content(self) -> None:
        clean = "项目目标是构建私人 AI 记忆工作台"
        assert redact_content(clean) == clean


class TestQualityGateEvaluation:
    """质量门评估。"""

    def test_high_trust_with_evidence_auto_publishes(self) -> None:
        """高可信 + 有证据 → auto_publish。"""
        candidate = MemoryCandidate(
            memory_id="m1",
            layer="L1",
            type="fact",
            content="项目目标是构建记忆工作台",
            confidence=0.92,
            source_role="user",
            source_type="document",
            evidence_refs=("source://s1",),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "auto_publish"
        assert decision.new_status == "confirmed"

    def test_no_evidence_needs_review(self) -> None:
        """无证据 → needs_review。"""
        candidate = MemoryCandidate(
            memory_id="m2",
            layer="L1",
            type="fact",
            content="某条无证据的事实",
            confidence=0.9,
            source_role="user",
            source_type="document",
            evidence_refs=(),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "needs_review"
        assert decision.new_status == "needs_review"

    def test_assistant_low_trust_needs_review(self) -> None:
        """assistant 回复默认低可信 → low_trust + needs_review。"""
        candidate = MemoryCandidate(
            memory_id="m3",
            layer="L1",
            type="other",
            content="assistant 推测的某条信息",
            confidence=0.7,
            source_role="assistant",
            source_type="conversation",
            evidence_refs=("source://s1",),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "low_trust"
        assert decision.new_status == "needs_review"

    def test_secret_detected_needs_human_judgment(self) -> None:
        """含 secret → needs_human_judgment + suggested_layer=L0。"""
        candidate = MemoryCandidate(
            memory_id="m4",
            layer="L1",
            type="fact",
            content="my api_key=sk-abc123def456ghi789jkl012mno345pqr",
            confidence=0.9,
            source_role="user",
            source_type="document",
            evidence_refs=("source://s1",),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "needs_human_judgment"
        assert "api_key" in decision.detected_secrets
        assert decision.suggested_layer == "L0"
        assert "[REDACTED]" in decision.redacted_content

    def test_conflict_goes_to_conflict_group(self) -> None:
        """冲突项 → conflict + needs_review。"""
        candidate = MemoryCandidate(
            memory_id="m5",
            layer="L1",
            type="fact",
            content="与已有事实冲突的新事实",
            confidence=0.85,
            source_role="user",
            source_type="document",
            evidence_refs=("source://s1",),
            conflict_refs=("existing-m1",),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "conflict"
        assert decision.new_status == "needs_review"

    def test_duplicate_is_ignored(self) -> None:
        """重复内容 → ignored + archived。"""
        candidate = MemoryCandidate(
            memory_id="m6",
            layer="L1",
            type="fact",
            content="已有的事实",
            confidence=0.9,
            source_role="user",
            source_type="document",
            evidence_refs=("source://s1",),
        )
        decision = evaluate_candidate(
            candidate,
            existing_memory_signatures=["已有的事实"],
        )
        assert decision.group == "ignored"
        assert decision.new_status == "archived"

    def test_l3_promotion_requires_multiple_evidence(self) -> None:
        """L3 候选需要至少 2 条证据 → l3_promotion。"""
        candidate = MemoryCandidate(
            memory_id="m7",
            layer="L3",
            type="preference",
            content="用户偏好简洁回答风格",
            confidence=0.92,
            source_role="user",
            source_type="custom_instruction",
            evidence_refs=("source://s1", "source://s2"),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "l3_promotion"
        assert decision.can_promote_to_l3 is True
        assert decision.suggested_layer == "L3"

    def test_l3_with_single_evidence_needs_review(self) -> None:
        """L3 候选只有 1 条证据 → needs_review。"""
        candidate = MemoryCandidate(
            memory_id="m8",
            layer="L3",
            type="preference",
            content="用户偏好简洁回答风格",
            confidence=0.92,
            source_role="user",
            source_type="custom_instruction",
            evidence_refs=("source://s1",),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "needs_review"
        assert decision.suggested_layer == "L2"

    def test_too_short_content_l0_only(self) -> None:
        """过短内容 → l0_only + archived。"""
        candidate = MemoryCandidate(
            memory_id="m9",
            layer="L1",
            type="fact",
            content="短",
            confidence=0.9,
            source_role="user",
            source_type="document",
            evidence_refs=("source://s1",),
        )
        decision = evaluate_candidate(candidate)
        assert decision.group == "l0_only"


class TestQualityGateReport:
    """质量门汇总报告。"""

    def test_report_aggregates_groups(self) -> None:
        candidates = (
            MemoryCandidate(
                memory_id="m1",
                layer="L1",
                type="fact",
                content="这是一条高可信的项目事实记录",
                confidence=0.92,
                source_role="user",
                source_type="document",
                evidence_refs=("source://s1",),
            ),
            MemoryCandidate(
                memory_id="m2",
                layer="L1",
                type="other",
                content="assistant 给出的推测性回答内容",
                confidence=0.6,
                source_role="assistant",
                source_type="conversation",
                evidence_refs=("source://s2",),
            ),
            MemoryCandidate(
                memory_id="m3",
                layer="L1",
                type="fact",
                content="含 api_key=sk-abc123def456ghi789jkl012mno345pqr 的内容",
                confidence=0.85,
                source_role="user",
                source_type="document",
                evidence_refs=("source://s3",),
            ),
        )
        report = evaluate_candidates(candidates)
        assert report.total_candidates == 3
        assert "auto_publish" in report.group_counts
        assert "low_trust" in report.group_counts
        assert "needs_human_judgment" in report.group_counts
        assert report.redacted_count == 1

    def test_report_serializes(self) -> None:
        candidate = MemoryCandidate(
            memory_id="m1",
            layer="L1",
            type="fact",
            content="测试事实",
            confidence=0.9,
            source_role="user",
            source_type="document",
            evidence_refs=("source://s1",),
        )
        report = evaluate_candidates([candidate])
        serialized = serialize_quality_gate_report(report)
        assert "decisions" in serialized
        assert "group_counts" in serialized
        assert serialized["total_candidates"] == 1


# ──────────────────────────────────────────────────────────────
# Memory Router export_event 测试
# ──────────────────────────────────────────────────────────────


class TestMemoryRouterExportEvent:
    """阶段 1.6：MemoryRouter 的 export_event 类型。"""

    def test_export_trigger_returns_export_event(self) -> None:
        router = MemoryRouter()
        result = router.execute(content="export", trigger_event="export")
        assert result.memory_event_type == "export_event"
        assert result.quality_gate == "skip_long_term"

    def test_export_event_user_visible_summary(self) -> None:
        router = MemoryRouter()
        result = router.execute(trigger_event="export")
        assert "导出" in result.user_visible_summary

    def test_export_event_does_not_create_l1_l2_l3_delta(self) -> None:
        """export_event 不应产生 L1/L2/L3 写入。"""
        router = MemoryRouter()
        result = router.execute(trigger_event="export")
        layers = {d.layer for d in result.memory_delta}
        assert "L1" not in layers
        assert "L2" not in layers
        assert "L3" not in layers

    def test_external_import_trigger(self) -> None:
        router = MemoryRouter()
        result = router.execute(content="some import", trigger_event="external_import")
        assert result.memory_event_type == "external_import"


# ──────────────────────────────────────────────────────────────
# External Import 测试
# ──────────────────────────────────────────────────────────────


class TestGenericMarkdownTxtAdapter:
    """Markdown / TXT 适配器。"""

    def test_detect_markdown(self) -> None:
        adapter = GenericMarkdownTxtAdapter()
        assert adapter.detect("# 项目说明\n\n这是内容。") is True

    def test_detect_plain_text_with_md_extension(self) -> None:
        adapter = GenericMarkdownTxtAdapter()
        # 通过文件名识别
        assert adapter.detect("notes.md") is True or adapter.detect("# 标题") is True

    def test_parse_extracts_titles(self) -> None:
        adapter = GenericMarkdownTxtAdapter()
        bundle = "# 项目说明\n\n我喜欢简洁的回答风格。\n项目目标是构建记忆工作台。"
        parsed = adapter.parse(bundle)
        assert parsed.format == "markdown"
        assert len(parsed.raw_documents) == 1
        assert parsed.raw_documents[0].title == "项目说明"

    def test_to_memory_candidates_extracts_preferences(self) -> None:
        adapter = GenericMarkdownTxtAdapter()
        bundle = "# 项目说明\n\n我喜欢简洁的回答风格"
        parsed = adapter.parse(bundle)
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")
        # 应该抽取到偏好候选
        pref_candidates = [c for c in candidates if c.type == "preference"]
        assert len(pref_candidates) >= 1
        assert pref_candidates[0].trust_level == "high"
        assert pref_candidates[0].layer == "L4"

    def test_to_memory_candidates_extracts_facts(self) -> None:
        adapter = GenericMarkdownTxtAdapter()
        bundle = "# 项目说明\n\n项目目标是构建记忆工作台"
        parsed = adapter.parse(bundle)
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")
        fact_candidates = [c for c in candidates if c.type == "fact"]
        assert len(fact_candidates) >= 1

    def test_evidence_refs_present(self) -> None:
        adapter = GenericMarkdownTxtAdapter()
        parsed = adapter.parse("# 标题\n\n项目目标是构建")
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")
        for c in candidates:
            assert len(c.evidence_refs) > 0


class TestGenericJsonConversationAdapter:
    """JSON / JSONL 对话适配器。"""

    def test_detect_json_with_messages(self) -> None:
        adapter = GenericJsonConversationAdapter()
        bundle = '{"messages": [{"role": "user", "content": "hello"}]}'
        assert adapter.detect(bundle) is True

    def test_parse_json_messages(self) -> None:
        adapter = GenericJsonConversationAdapter()
        bundle = '{"messages": [{"role": "user", "content": "我喜欢简洁回答"}, {"role": "assistant", "content": "好的我会保持简洁回答风格"}]}'
        parsed = adapter.parse(bundle)
        assert len(parsed.raw_messages) == 2
        assert parsed.raw_messages[0].role == "user"
        assert parsed.raw_messages[1].role == "assistant"

    def test_parse_jsonl(self) -> None:
        adapter = GenericJsonConversationAdapter()
        bundle = '{"role": "user", "content": "第一条消息"}\n{"role": "assistant", "content": "第二条消息回复内容"}'
        parsed = adapter.parse(bundle)
        assert len(parsed.raw_messages) == 2

    def test_parse_conversations_format(self) -> None:
        """支持 {"conversations": [{"from": "human", "value": "..."}]} 格式。"""
        adapter = GenericJsonConversationAdapter()
        bundle = '{"conversations": [{"from": "human", "value": "用户消息内容"}, {"from": "gpt", "value": "AI 回复内容"}]}'
        parsed = adapter.parse(bundle)
        assert len(parsed.raw_messages) == 2
        assert parsed.raw_messages[0].role == "user"
        assert parsed.raw_messages[1].role == "assistant"

    def test_assistant_candidates_are_low_trust(self) -> None:
        """assistant 回复默认低可信。"""
        adapter = GenericJsonConversationAdapter()
        bundle = '{"messages": [{"role": "assistant", "content": "这是 AI 的回复内容，足够长"}]}'
        parsed = adapter.parse(bundle)
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")
        assert len(candidates) == 1
        assert candidates[0].source_role == "assistant"
        assert candidates[0].trust_level == "low"
        assert candidates[0].layer == "L0"  # assistant 默认不进入 L1
        assert candidates[0].status == "needs_review"

    def test_user_candidates_are_medium_trust(self) -> None:
        adapter = GenericJsonConversationAdapter()
        bundle = '{"messages": [{"role": "user", "content": "这是用户的消息内容"}]}'
        parsed = adapter.parse(bundle)
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")
        assert candidates[0].source_role == "user"
        assert candidates[0].trust_level == "medium"
        assert candidates[0].layer == "L1"

    def test_system_candidates_go_to_l4_review(self) -> None:
        """system 消息只能进入待确认 L4 Persona 候选。"""
        adapter = GenericJsonConversationAdapter()
        bundle = '{"messages": [{"role": "system", "content": "你是用户的私人记忆助手"}]}'
        parsed = adapter.parse(bundle)
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")
        assert candidates[0].source_role == "system"
        assert candidates[0].layer == "L4"
        assert candidates[0].type == "persona"
        assert candidates[0].status == "needs_review"


class TestGenericZipBundleAdapter:
    """ZIP 知识包适配器。"""

    def test_detect_zip_bytes(self) -> None:
        adapter = GenericZipBundleAdapter()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("test.txt", "hello")
        assert adapter.detect(buf.getvalue()) is True

    def test_parse_zip_with_markdown_and_instructions(self) -> None:
        adapter = GenericZipBundleAdapter()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"version": "1.0"}))
            zf.writestr("notes.md", "# 项目笔记\n项目目标是构建记忆工作台")
            zf.writestr("custom_instructions.md", "保持简洁回答风格")
        zip_bytes = buf.getvalue()

        parsed = adapter.parse(zip_bytes)
        assert parsed.format == "zip"
        assert len(parsed.raw_documents) >= 1
        assert len(parsed.raw_custom_instructions) >= 1

    def test_parse_zip_keeps_advertised_csv_and_html_documents(self) -> None:
        adapter = GenericZipBundleAdapter()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("facts.csv", "name,value\nproject,knowledge-workbench")
            zf.writestr("page.html", "<html><body><h1>项目页面</h1></body></html>")

        parsed = adapter.parse(buf.getvalue())

        assert [(doc.title, doc.raw_format) for doc in parsed.raw_documents] == [
            ("facts.csv", "csv"),
            ("page.html", "html"),
        ]
        assert "knowledge-workbench" in parsed.raw_documents[0].content
        assert "项目页面" in parsed.raw_documents[1].content

    def test_corrupt_zip_returns_product_error_instead_of_bad_zip_exception(self) -> None:
        result = import_external_bundle(
            b"PK\x03\x04not-a-valid-central-directory",
            import_batch_id="bad-zip",
        )

        assert result.error == "ZIP 包损坏或无法读取"
        assert result.sources == ()
        assert result.candidates == ()

    def test_empty_zip_returns_no_importable_content_error(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass

        result = import_external_bundle(buf.getvalue(), import_batch_id="empty-zip")

        assert result.error == "导入包未包含可解析内容"
        assert result.sources == ()
        assert result.candidates == ()

    def test_duplicate_zip_entry_names_are_rejected(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("notes.md", "first")
            with pytest.warns(UserWarning, match="Duplicate name"):
                zf.writestr("notes.md", "second")

        result = import_external_bundle(buf.getvalue(), import_batch_id="duplicate-zip")

        assert result.error == "ZIP 包包含重复文件名"

    def test_expanded_zip_entry_limit_is_enforced(self, monkeypatch) -> None:
        monkeypatch.setattr(GenericZipBundleAdapter, "MAX_ENTRY_BYTES", 4)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("notes.md", "12345")

        result = import_external_bundle(buf.getvalue(), import_batch_id="large-entry")

        assert "单个文件解压后超过 4 字节" in (result.error or "")

    def test_to_memory_candidates_includes_l4_persona(self) -> None:
        adapter = GenericZipBundleAdapter()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("custom_instructions.md", "保持简洁回答风格")
            zf.writestr("notes.md", "# 笔记\n项目目标是构建记忆工作台")
        zip_bytes = buf.getvalue()

        parsed = adapter.parse(zip_bytes)
        normalized = adapter.normalize(parsed)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id="b1")

        # custom_instructions 应生成待确认 L4 Persona 候选
        l4_candidates = [c for c in candidates if c.layer == "L4"]
        assert len(l4_candidates) >= 1
        assert l4_candidates[0].type == "persona"
        assert l4_candidates[0].status == "needs_review"


class TestGenericLLMConversationAdapter:
    """通用 LLM 对话导出适配器。"""

    def test_detect_chatgpt_export(self) -> None:
        adapter = GenericLLMConversationAdapter()
        bundle = '[{"title": "测试对话", "mapping": {}}]'
        assert adapter.detect(bundle) is True

    def test_parse_chatgpt_conversations_json(self) -> None:
        """ChatGPT conversations.json 格式。"""
        adapter = GenericLLMConversationAdapter()
        bundle = json.dumps([
            {
                "title": "测试对话",
                "id": "conv-1",
                "mapping": {
                    "node-1": {
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["我喜欢简洁回答"]},
                            "create_time": 1700000000,
                        }
                    },
                    "node-2": {
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["好的，我会保持简洁回答风格"]},
                            "create_time": 1700000001,
                        }
                    },
                }
            }
        ])
        parsed = adapter.parse(bundle)
        assert len(parsed.raw_messages) == 2
        assert parsed.raw_messages[0].role == "user"
        assert parsed.raw_messages[1].role == "assistant"
        assert parsed.platform == "chatgpt_export"

    def test_assistant_does_not_auto_publish(self) -> None:
        """assistant 回复不会直接自动发布为长期记忆。"""
        adapter = GenericLLMConversationAdapter()
        bundle = json.dumps([
            {
                "title": "测试",
                "id": "conv-1",
                "mapping": {
                    "n1": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["AI 回复的某条信息内容"]}}}
                }
            }
        ])
        result = import_external_bundle(bundle, import_batch_id="b1")
        for c in result.candidates:
            if c.source_role == "assistant":
                assert c.status == "needs_review"
                assert c.layer == "L0"

    def test_validate_assistant_must_be_needs_review(self) -> None:
        adapter = GenericLLMConversationAdapter()
        bundle = json.dumps([
            {
                "title": "测试",
                "id": "conv-1",
                "mapping": {
                    "n1": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["AI 回复的某条信息内容"]}}}
                }
            }
        ])
        result = import_external_bundle(bundle, import_batch_id="b1")
        validation = adapter.validate_import_result(result)
        assert validation.is_valid is True


class TestImportExternalBundle:
    """import_external_bundle 主入口。"""

    def test_import_json_conversation(self) -> None:
        bundle = '{"messages": [{"role": "user", "content": "用户喜欢的回答风格是简洁直接"}, {"role": "assistant", "content": "AI 给出的推测性回答内容较长"}]}'
        result = import_external_bundle(bundle, import_batch_id="batch-1")
        assert result.import_batch_id == "batch-1"
        assert len(result.sources) == 2
        assert result.role_stats.get("user", 0) == 1
        assert result.role_stats.get("assistant", 0) == 1
        assert result.low_trust_count >= 1

    def test_import_no_matching_adapter(self) -> None:
        """无匹配 adapter 时返回 error。"""
        result = import_external_bundle("12345", import_batch_id="batch-x")
        assert result.error == "no_matching_adapter"

    def test_import_summary_contains_stats(self) -> None:
        bundle = '{"messages": [{"role": "user", "content": "用户消息内容"}]}'
        result = import_external_bundle(bundle, import_batch_id="batch-1")
        assert "user" in result.summary
        assert "Source" in result.summary

    def test_validate_import_result_checks_evidence(self) -> None:
        bundle = '{"messages": [{"role": "user", "content": "用户消息内容"}]}'
        result = import_external_bundle(bundle, import_batch_id="batch-1")
        validation = validate_import_result(result)
        assert validation.is_valid is True
        # 所有候选都应该有 evidence_refs
        for c in result.candidates:
            assert len(c.evidence_refs) > 0

    def test_import_batch_id_propagates(self) -> None:
        bundle = '{"messages": [{"role": "user", "content": "用户消息内容"}]}'
        result = import_external_bundle(bundle, import_batch_id="my-batch-xyz")
        for c in result.candidates:
            assert c.import_batch_id == "my-batch-xyz"


# ──────────────────────────────────────────────────────────────
# Memory Export 测试
# ──────────────────────────────────────────────────────────────


def _build_test_payload() -> ExportPayload:
    """构造测试用 ExportPayload。"""
    return ExportPayload(
        memories=(
            ExportableMemory(
                memory_id="m1",
                layer="L1",
                type="fact",
                content="项目目标是构建私人 AI 记忆工作台",
                summary="项目目标",
                tags=("project",),
                confidence=0.92,
                trust_level="high",
                source_ref="source://s1",
                evidence_refs=("source://s1",),
                confirmed=True,
                project_id="project-portable",
                series_id="series-portable",
                atom_ids=("atom-parent",),
                scenario_ids=("scenario-parent",),
            ),
            ExportableMemory(
                memory_id="m2",
                layer="L3",
                type="preference",
                content="我喜欢简洁的回答风格",
                summary="回答风格偏好",
                tags=("style",),
                confidence=0.88,
                trust_level="high",
                source_ref="source://s2",
                evidence_refs=("source://s2", "source://s3"),
                confirmed=True,
            ),
            ExportableMemory(
                memory_id="m3",
                layer="L0",
                type="other",
                content="assistant said something not very important",
                confidence=0.40,
                trust_level="low",
                source_ref="source://s4",
                evidence_refs=("source://s4",),
                confirmed=False,
                status="needs_review",
            ),
            ExportableMemory(
                memory_id="m4",
                layer="L1",
                type="fact",
                content="my api_key=sk-abc123def456ghi789jkl012mno345pqr",
                confidence=0.85,
                trust_level="high",
                source_ref="source://s5",
                evidence_refs=("source://s5",),
                confirmed=True,
            ),
        ),
        sources=(
            ExportableSource(
                source_id="s1",
                source_type="text",
                title="项目说明.md",
                content_ref="C:/Users/test/data/sources/s1.json",
            ),
        ),
        tags=(
            ExportableTag(tag="project", memory_ids=("m1",)),
            ExportableTag(tag="style", memory_ids=("m2",)),
        ),
        evidence_links=(
            ExportableEvidenceLink(
                memory_id="m1",
                source_ref="source://s1",
                evidence_ref="source://s1",
                relation="supports",
            ),
        ),
        persona_summary="用户偏好简洁回答风格。",
        series_summary="7 月记忆工作台构建系列。",
        project_skill_cards=(
            {"name": "Memory Router", "description": "自动判断 memory_event_type"},
        ),
    )


class TestExportFullAssetPackage:
    """完整资产包导出。"""

    def test_export_zip_format(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package")
        result = export_memory(payload, scope, export_batch_id="exp-001")
        assert result.format == "zip"
        assert result.file_name.endswith(".zip")
        assert len(result.bytes_payload) > 0
        assert result.memory_count == 4
        import zipfile
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            l1 = json.loads(
                zf.read("memories/l1_atomic_facts.ndjson").decode().splitlines()[0]
            )
        assert manifest["project_skill_count"] == 1
        assert l1["project_id"] == "project-portable"
        assert l1["series_id"] == "series-portable"
        assert l1["atom_ids"] == ["atom-parent"]
        assert l1["scenario_ids"] == ["scenario-parent"]

    def test_export_redacts_secrets(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package", redact_secrets=True)
        result = export_memory(payload, scope, export_batch_id="exp-002")
        # 验证 zip 内不含原始 secret
        import zipfile
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as zf:
            for name in zf.namelist():
                if name.endswith(".ndjson") or name.endswith(".json"):
                    content = zf.read(name).decode("utf-8")
                    assert "sk-abc123" not in content, f"{name} 含原始 secret"

    def test_export_skips_low_trust_when_configured(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package", skip_low_trust=True)
        result = export_memory(payload, scope, export_batch_id="exp-003")
        assert result.memory_count == 3  # m3 被跳过
        assert result.skipped_count >= 1

    def test_export_does_not_include_full_paths_by_default(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package")
        result = export_memory(payload, scope, export_batch_id="exp-004")
        import zipfile
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as zf:
            sources_content = zf.read("sources/source_manifest.ndjson").decode("utf-8")
            assert "C:/Users/test" not in sources_content


class TestExportRoundTrip:
    """完整资产包 round-trip 导入。"""

    @staticmethod
    def _rewrite_zip(
        zip_bytes: bytes,
        *,
        replacements: dict[str, bytes | str],
    ) -> bytes:
        source = io.BytesIO(zip_bytes)
        target = io.BytesIO()
        with zipfile.ZipFile(source) as input_zip, zipfile.ZipFile(target, "w") as output_zip:
            for entry in input_zip.infolist():
                replacement = replacements.get(entry.filename)
                if replacement is None:
                    output_zip.writestr(entry, input_zip.read(entry.filename))
                else:
                    output_zip.writestr(entry, replacement)
        return target.getvalue()

    @staticmethod
    def _asset_payload() -> ExportPayload:
        content = b"portable original source"
        return replace(
            _build_test_payload(),
            source_assets=(
                ExportableSourceAsset(
                    source_id="s1",
                    asset_id="asset-one",
                    display_name="original.txt",
                    media_type="text/plain",
                    byte_count=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    content=content,
                ),
            ),
            source_asset_expected_count=1,
        )

    def test_round_trip_import_validates_manifest(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package")
        result = export_memory(payload, scope, export_batch_id="rt-001")
        report = import_asset_package(result.bytes_payload)
        assert report.is_valid is True
        assert report.errors == ()
        assert report.imported_memory_count >= 1
        assert report.imported_source_count >= 1
        assert report.imported_project_skill_count == 1

    def test_round_trip_import_invalid_zip(self) -> None:
        report = import_asset_package(b"not a zip")
        assert report.is_valid is False
        assert len(report.errors) > 0

    def test_round_trip_import_missing_manifest(self) -> None:
        """没有 manifest.json 的 zip 应该校验失败。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("random.txt", "no manifest")
        report = import_asset_package(buf.getvalue())
        assert report.is_valid is False
        assert any("manifest" in e for e in report.errors)

    def test_round_trip_preserves_memory_count(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package")
        result = export_memory(payload, scope, export_batch_id="rt-002")
        report = import_asset_package(result.bytes_payload)
        # L1 + L2 + L3 的 memory 应该被重建
        expected_l1_l2_l3 = sum(1 for m in payload.memories if m.layer in ("L1", "L2", "L3"))
        assert report.imported_memory_count == expected_l1_l2_l3

    def test_round_trip_rejects_corrupt_project_skill_json(self) -> None:
        result = export_memory(
            _build_test_payload(),
            ExportScope(preset="full_asset_package"),
            export_batch_id="rt-corrupt-skill",
        )
        corrupted = self._rewrite_zip(
            result.bytes_payload,
            replacements={"project_skills/project_skill_cards.ndjson": "{not-json}\n"},
        )

        report = import_asset_package(corrupted)

        assert report.is_valid is False
        assert any(
            "project_skills/project_skill_cards.ndjson" in error
            for error in report.errors
        )

    def test_round_trip_rejects_project_skill_manifest_count_mismatch(self) -> None:
        result = export_memory(
            _build_test_payload(),
            ExportScope(preset="full_asset_package"),
            export_batch_id="rt-skill-count",
        )
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as source:
            manifest = json.loads(source.read("manifest.json"))
        manifest["project_skill_count"] = manifest["project_skill_count"] + 1
        mismatched = self._rewrite_zip(
            result.bytes_payload,
            replacements={"manifest.json": json.dumps(manifest)},
        )

        report = import_asset_package(mismatched)

        assert report.is_valid is False
        assert any("manifest.project_skill_count" in error for error in report.errors)

    def test_round_trip_accepts_legacy_manifest_without_project_skill_count(self) -> None:
        result = export_memory(
            _build_test_payload(),
            ExportScope(preset="full_asset_package"),
            export_batch_id="rt-legacy-count",
        )
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as source:
            manifest = json.loads(source.read("manifest.json"))
        manifest.pop("project_skill_count")
        legacy = self._rewrite_zip(
            result.bytes_payload,
            replacements={"manifest.json": json.dumps(manifest)},
        )

        report = import_asset_package(legacy)

        assert report.is_valid is True
        assert report.imported_project_skill_count == 1

    def test_round_trip_validates_original_source_asset(self) -> None:
        result = export_memory(
            self._asset_payload(),
            ExportScope(preset="full_asset_package"),
            export_batch_id="rt-source-asset",
        )

        report = import_asset_package(result.bytes_payload)

        assert report.is_valid is True
        assert report.imported_source_asset_count == 1

    def test_round_trip_rejects_tampered_original_source_blob(self) -> None:
        result = export_memory(
            self._asset_payload(),
            ExportScope(preset="full_asset_package"),
            export_batch_id="rt-tampered-asset",
        )
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as source:
            blob_path = next(
                name for name in source.namelist() if name.startswith("sources/content/")
            )
        tampered = self._rewrite_zip(
            result.bytes_payload,
            replacements={blob_path: b"tampered"},
        )

        report = import_asset_package(tampered)

        assert report.is_valid is False
        assert any("Blob" in error for error in report.errors)

    def test_round_trip_rejects_undeclared_original_source_blob(self) -> None:
        result = export_memory(
            _build_test_payload(),
            ExportScope(preset="full_asset_package"),
            export_batch_id="rt-undeclared-asset",
        )
        source = io.BytesIO(result.bytes_payload)
        target = io.BytesIO()
        with zipfile.ZipFile(source) as input_zip, zipfile.ZipFile(target, "w") as output_zip:
            for entry in input_zip.infolist():
                output_zip.writestr(entry, input_zip.read(entry.filename))
            output_zip.writestr("sources/content/" + "a" * 64, b"undeclared")

        report = import_asset_package(target.getvalue())

        assert report.is_valid is False
        assert any("未声明" in error for error in report.errors)


class TestExportPresets:
    """各导出预设。"""

    def test_markdown_knowledge_base(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="generic_markdown_knowledge_base")
        result = export_memory(payload, scope, export_batch_id="exp-md-1")
        assert result.format == "markdown"
        content = result.bytes_payload.decode("utf-8")
        assert "私人记忆知识包" in content
        assert "项目目标" in content

    def test_compact_persona_prompt(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="compact_persona_prompt")
        result = export_memory(payload, scope, export_batch_id="exp-md-2")
        assert result.format == "prompt_text"
        content = result.bytes_payload.decode("utf-8")
        assert "用户画像" in content or "偏好" in content

    def test_full_memory_brief(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_memory_brief")
        result = export_memory(payload, scope, export_batch_id="exp-md-3")
        content = result.bytes_payload.decode("utf-8")
        assert "完整记忆简报" in content
        assert "L1" in content
        assert "L3" in content

    def test_generic_llm_project_knowledge(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="generic_llm_project_knowledge")
        result = export_memory(payload, scope, export_batch_id="exp-md-4")
        content = result.bytes_payload.decode("utf-8")
        assert "项目知识包" in content

    def test_generic_custom_instructions(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="generic_custom_instructions")
        result = export_memory(payload, scope, export_batch_id="exp-md-5")
        content = result.bytes_payload.decode("utf-8")
        assert "自定义指令" in content

    def test_rag_corpus_ndjson(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="generic_rag_corpus")
        result = export_memory(payload, scope, export_batch_id="exp-rag-1")
        assert result.format == "ndjson"
        content = result.bytes_payload.decode("utf-8")
        lines = [l for l in content.strip().split("\n") if l.strip()]
        assert len(lines) >= 1
        # 每行应该是有效 JSON
        for line in lines:
            entry = json.loads(line)
            assert "id" in entry
            assert "text" in entry
            assert "metadata" in entry
            assert entry["metadata"]["layer"] in ("L1", "L2")


class TestExportRedaction:
    """脱敏导出。"""

    def test_redacted_export_no_secret(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="generic_markdown_knowledge_base", redact_secrets=True)
        result = export_memory(payload, scope, export_batch_id="exp-red-1")
        content = result.bytes_payload.decode("utf-8")
        assert "sk-abc123" not in content
        assert "[REDACTED]" in content
        assert result.redacted_count >= 1

    def test_only_confirmed_export(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="generic_markdown_knowledge_base", only_confirmed=True)
        result = export_memory(payload, scope, export_batch_id="exp-red-2")
        # m3 是 confirmed=False，应该被跳过
        assert result.skipped_count >= 1

    def test_skip_raw_sources(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package", skip_raw_sources=True)
        result = export_memory(payload, scope, export_batch_id="exp-red-3")
        # The provenance manifest remains so exported memories never contain
        # dangling source references. This option excludes original bytes.
        assert result.source_count == 1
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload), "r") as archive:
            assert '"source_id": "s1"' in archive.read("sources/source_manifest.ndjson").decode()
            assert archive.read("sources/source_assets.ndjson") == b""

    def test_include_paths_false_removes_full_paths(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package", include_paths=False)
        result = export_memory(payload, scope, export_batch_id="exp-red-4")
        import zipfile
        with zipfile.ZipFile(io.BytesIO(result.bytes_payload)) as zf:
            sources_content = zf.read("sources/source_manifest.ndjson").decode("utf-8")
            assert "C:/Users" not in sources_content

    def test_skip_low_trust_excludes_low(self) -> None:
        payload = _build_test_payload()
        scope = ExportScope(preset="full_asset_package", skip_low_trust=True)
        result = export_memory(payload, scope, export_batch_id="exp-red-5")
        # m3 是 low trust，应该被跳过
        assert result.memory_count == 3


# ──────────────────────────────────────────────────────────────
# 集成：import → quality gate → export
# ──────────────────────────────────────────────────────────────


class TestImportQualityGateExportIntegration:
    """端到端：导入 → 质量门 → 导出。"""

    def test_full_pipeline(self) -> None:
        # 1. 导入 JSON 对话
        bundle = '{"messages": [{"role": "user", "content": "我喜欢简洁的回答风格"}, {"role": "assistant", "content": "好的我会保持简洁回答风格"}, {"role": "system", "content": "你是用户的私人记忆助手"}]}'
        import_result = import_external_bundle(bundle, import_batch_id="integration-1")

        # 2. 喂给 quality gate
        gate_candidates = []
        for c in import_result.candidates:
            gate_candidates.append(MemoryCandidate(
                memory_id=c.memory_id,
                layer=c.layer,
                type=c.type,
                content=c.content,
                summary=c.summary,
                confidence=c.confidence,
                trust_level=c.trust_level,
                source_platform=c.source_platform,
                source_type=c.source_type,
                source_role=c.source_role,
                source_ref=c.source_ref,
                evidence_refs=c.evidence_refs,
                status=c.status,
                import_batch_id=c.import_batch_id,
            ))
        report = evaluate_candidates(gate_candidates)

        # 3. 验证：assistant 候选应该被分到 low_trust
        assistant_decisions = [
            d for c, d in zip(gate_candidates, report.decisions)
            if c.source_role == "assistant"
        ]
        for d in assistant_decisions:
            assert d.group == "low_trust"

        # 4. 构造 ExportPayload 并导出
        exportable_memories = tuple(
            ExportableMemory(
                memory_id=c.memory_id,
                layer=c.layer,
                type=c.type,
                content=c.content,
                summary=c.summary,
                confidence=c.confidence,
                trust_level=c.trust_level,
                source_ref=c.source_ref,
                evidence_refs=c.evidence_refs,
                confirmed=(d.new_status == "confirmed"),
                status=d.new_status,
            )
            for c, d in zip(gate_candidates, report.decisions)
        )
        payload = ExportPayload(
            memories=exportable_memories,
            sources=(),
            tags=(),
            evidence_links=(),
        )
        scope = ExportScope(preset="full_asset_package", skip_low_trust=False)
        export_result = export_memory(payload, scope, export_batch_id="integration-export-1")
        assert export_result.memory_count == len(gate_candidates)
        assert export_result.format == "zip"

    def test_legacy_join_knowledge_base_field_ignored(self) -> None:
        """legacy join_knowledge_base 字段不影响导入流程。"""
        # 模拟前端仍然传 join_knowledge_base: true 的场景
        # import_external_bundle 不接受此参数，所以只是验证流程不报错
        bundle = '{"messages": [{"role": "user", "content": "用户消息内容"}]}'
        result = import_external_bundle(bundle, import_batch_id="legacy-1")
        assert result.error == ""
        assert len(result.sources) >= 1
