"""Phase 2: Provider-enhanced classifier integration into OrchestrateWorkbenchAutoIntake.

These tests verify that the unified auto-intake orchestrator calls the authorized
main-model provider enhancer when local confidence is low, when input contains
multiple links, or when enhancement is forced. They also verify that provider
failures degrade gracefully and that no secret material leaks into responses.
"""

from __future__ import annotations

import re
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    OrchestrateWorkbenchAutoIntake,
    WorkbenchInputClassificationResult,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _source_ids(store: JsonObjectStore) -> list[str]:
    return [str(item.get("id")) for item in store.list("sources") if item.get("id")]


def _enhanced_result(
    local: WorkbenchInputClassificationResult,
    *,
    input_type: str | None = None,
    confidence: float = 0.92,
    route: str | None = None,
) -> WorkbenchInputClassificationResult:
    """Build a provider-enhanced result that mimics EnhanceWorkbenchInputClassification._merge."""
    return WorkbenchInputClassificationResult(
        status="provider_enhanced",
        input_type=input_type or local.input_type,
        input_type_label=local.input_type_label,
        intent=local.intent,
        intent_label=local.intent_label,
        route=route or local.route,
        confidence=confidence,
        needs_user_confirmation=confidence < 0.72,
        target_intake=local.target_intake,
        workflow_steps=local.workflow_steps,
        structured_output_plan=local.structured_output_plan,
        memory_layer_update_plan=local.memory_layer_update_plan,
        suggested_next_actions=local.suggested_next_actions,
        media_required_capability=local.media_required_capability,
        auto_workflow=local.auto_workflow,
        child_inputs=local.child_inputs,
        provider_enhancement_recommended=confidence < 0.72,
        provider_enhancement_reason="Provider 已增强前置输入分类。",
        recommended_provider_role="intake_main_model",
        provider_boundary="provider_enhanced_by:intake-main-model",
        privacy_boundary="Provider payload is redacted and must not include API keys, cookies, tokens, passwords, authorization headers or raw absolute local paths.",
        classifier_version="workbench-input-classifier-provider-v1",
        classifier_prompt_id=local.classifier_prompt_id,
        classifier_prompt_revision=local.classifier_prompt_revision,
        classifier_prompt_source=local.classifier_prompt_source,
    )


def _make_orchestrator(
    store: JsonObjectStore,
    *,
    enhance_classification=None,
    force_provider_enhancement: bool = False,
    fetch_html: str = "<html><body><p>个人 AI 记忆工作台 资料库 记忆 四层</p></body></html>",
) -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id="default"),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: fetch_html,
        namespace_id="default",
        enhance_classification=enhance_classification,
        force_provider_enhancement=force_provider_enhancement,
    )


def test_low_confidence_file_triggers_enhancer_and_high_confidence_proceeds(tmp_path: Path) -> None:
    """Unknown file type has confidence 0.72 < 0.8, so enhancer should be called.

    After enhancement raises confidence to 0.92, needs_user_confirmation becomes False
    and the orchestrator proceeds to capture the file Source instead of returning
    needs_confirmation.
    """
    store = _store(tmp_path)
    enhance_calls: list[dict[str, object]] = []

    def enhance(**kwargs):
        enhance_calls.append(kwargs)
        local = kwargs["local_result"]
        return _enhanced_result(local, confidence=0.92)

    orchestrator = _make_orchestrator(store, enhance_classification=enhance)

    result = orchestrator.execute(
        media_type="application/x-unknown",
        file_name="mystery.dat",
        add_to_knowledge_base=True,
    )

    assert len(enhance_calls) == 1, "low-confidence file must trigger enhancer"
    assert enhance_calls[0]["media_type"] == "application/x-unknown"
    assert enhance_calls[0]["file_name"] == "mystery.dat"
    assert result.status == "accepted", "enhanced high-confidence must proceed to capture"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.input_type == "file"
    assert item.needs_user_confirmation is False
    assert item.source_id.startswith("source-")


def test_multi_link_bookmark_collection_triggers_enhancer(tmp_path: Path) -> None:
    """Bookmark collection (multiple links) has child_inputs, so enhancer should be called."""
    store = _store(tmp_path)
    enhance_calls: list[dict[str, object]] = []

    def enhance(**kwargs):
        enhance_calls.append(kwargs)
        local = kwargs["local_result"]
        return _enhanced_result(local, confidence=0.9)

    orchestrator = _make_orchestrator(store, enhance_classification=enhance)

    result = orchestrator.execute(
        content="https://example.com/a\nhttps://example.com/b",
        add_to_knowledge_base=True,
    )

    assert len(enhance_calls) == 1, "multi-link input must trigger enhancer"
    assert result.status == "accepted"
    assert len(result.items) == 2
    for item in result.items:
        assert item.input_type == "webpage"


def test_enhancer_value_error_degrades_to_local_classification(tmp_path: Path) -> None:
    """When the provider returns forbidden material or schema is rejected, the
    orchestrator must degrade to the local classification instead of crashing.
    For a low-confidence file, degradation means the needs_confirmation path
    surfaces the low-confidence state to the user.
    """
    store = _store(tmp_path)

    def raise_enhancer(**kwargs):
        raise ValueError("provider output includes forbidden secret markers")

    orchestrator = _make_orchestrator(store, enhance_classification=raise_enhancer)

    result = orchestrator.execute(
        media_type="application/x-unknown",
        file_name="mystery.dat",
        add_to_knowledge_base=True,
    )

    assert result.status == "needs_confirmation", "degraded low-confidence must surface needs_confirmation"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.needs_user_confirmation is True
    assert item.source_id == ""
    assert _source_ids(store) == [], "degraded low-confidence must not persist a Source"


def test_default_orchestrator_without_enhancer_skips_enhancement(tmp_path: Path) -> None:
    """When enhance_classification is None (default), the orchestrator must not
    attempt any provider call and must behave exactly like the pre-Phase-2 path.
    """
    store = _store(tmp_path)
    orchestrator = _make_orchestrator(store, enhance_classification=None)

    result = orchestrator.execute(
        media_type="application/x-unknown",
        file_name="mystery.dat",
        add_to_knowledge_base=True,
    )

    assert result.status == "needs_confirmation"
    assert result.items[0].needs_user_confirmation is True


def test_force_provider_enhancement_triggers_enhancer_even_for_high_confidence(tmp_path: Path) -> None:
    """A high-confidence webpage link (0.9) would not normally trigger enhancement,
    but force_provider_enhancement=True must still call the enhancer.
    """
    store = _store(tmp_path)
    enhance_calls: list[dict[str, object]] = []

    def enhance(**kwargs):
        enhance_calls.append(kwargs)
        local = kwargs["local_result"]
        return _enhanced_result(local, confidence=0.95)

    orchestrator = _make_orchestrator(
        store,
        enhance_classification=enhance,
        force_provider_enhancement=True,
    )

    result = orchestrator.execute(
        content="https://example.com/article",
        add_to_knowledge_base=True,
    )

    assert len(enhance_calls) == 1, "force_provider_enhancement must trigger enhancer even for high confidence"
    assert result.status == "accepted"
    assert result.items[0].input_type == "webpage"


def test_high_confidence_single_link_does_not_trigger_enhancer(tmp_path: Path) -> None:
    """A single high-confidence webpage link (0.9, no child_inputs) must NOT
    trigger the enhancer to avoid unnecessary provider calls.
    """
    store = _store(tmp_path)
    enhance_calls: list[dict[str, object]] = []

    def enhance(**kwargs):
        enhance_calls.append(kwargs)
        local = kwargs["local_result"]
        return _enhanced_result(local, confidence=0.95)

    orchestrator = _make_orchestrator(store, enhance_classification=enhance)

    result = orchestrator.execute(
        content="https://example.com/article",
        add_to_knowledge_base=True,
    )

    assert len(enhance_calls) == 0, "high-confidence single link must not trigger enhancer"
    assert result.status == "accepted"


def test_enhanced_response_contains_no_secret_or_local_path(tmp_path: Path) -> None:
    """The enhanced response must not leak API keys, cookies, authorization
    headers, passwords or local absolute paths even when the provider is invoked.
    """
    store = _store(tmp_path)

    def enhance(**kwargs):
        local = kwargs["local_result"]
        return _enhanced_result(local, confidence=0.92)

    orchestrator = _make_orchestrator(store, enhance_classification=enhance)

    result = orchestrator.execute(
        media_type="application/x-unknown",
        file_name="mystery.dat",
        add_to_knowledge_base=True,
    )

    from core.product_core import serialize_workbench_auto_intake_result

    payload = serialize_workbench_auto_intake_result(result)
    encoded = str(payload)
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", encoded), "no API key material in enhanced response"
    assert not re.search(r"[A-Za-z]:\\\\", encoded), "no Windows absolute path in enhanced response"
    assert "password=" not in encoded.lower()
    assert "cookie:" not in encoded.lower()
    assert "authorization:" not in encoded.lower()


def test_enhancer_payload_does_not_receive_secrets(tmp_path: Path) -> None:
    """The real EnhanceWorkbenchInputClassification must redact API keys, cookies,
    authorization headers and local paths before sending to the provider.

    This wires the real enhancer with a mock provider that captures the user_payload.
    The orchestrator passes raw content to the enhancer, but the enhancer's
    _safe_preview must redact sk- patterns and local paths before they reach the
    provider. This is the privacy boundary for Phase 2.
    """
    from collections.abc import Mapping

    from core.product_core import EnhanceWorkbenchInputClassification

    store = _store(tmp_path)
    captured_payload: dict[str, object] = {}

    class _CapturingProvider:
        def complete_json(self, *, system_prompt: str, user_payload: Mapping[str, object]) -> Mapping[str, object]:
            captured_payload.update(user_payload)
            return {
                "input_type": "file",
                "intent": "knowledge_supplement",
                "route": "file_intake",
                "confidence": 0.92,
                "workflow_steps": ["save_original_asset", "extract_readable_content"],
                "reason": "Provider 已增强前置输入分类。",
            }

    def enhance(**kwargs):
        return EnhanceWorkbenchInputClassification().execute(
            **kwargs,
            provider=_CapturingProvider(),
            provider_name="intake-main-model",
        )

    orchestrator = _make_orchestrator(store, enhance_classification=enhance)

    orchestrator.execute(
        content="sk-1234567890abcdef some text with secret",
        media_type="application/x-unknown",
        file_name="mystery.dat",
        add_to_knowledge_base=True,
    )

    content_preview = str(captured_payload.get("content_preview", ""))
    encoded = str(captured_payload)
    assert "sk-1234567890abcdef" not in encoded, "raw API key must not appear anywhere in provider payload"
    assert "sk-1234567890abcdef" not in content_preview, "raw API key must be redacted before provider"
    assert "[REDACTED_API_KEY]" in content_preview, "API key must be replaced with redaction marker"
    # The privacy_boundary documentation string legitimately contains the words
    # "cookies" and "authorization" as instruction text. What we actually forbid
    # is real cookie/auth *values* — i.e. `cookie=...`, `cookie: ...`,
    # `authorization=...`, `authorization: ...` patterns carrying secret material.
    assert not re.search(r"(?i)\bcookie\s*[:=]\s*\S", encoded), "no real cookie value may reach the provider"
    assert not re.search(r"(?i)\bauthorization\s*[:=]\s*\S", encoded), "no real authorization value may reach the provider"
    assert not re.search(r"(?i)\bpassword\s*[:=]\s*\S", encoded), "no real password value may reach the provider"
