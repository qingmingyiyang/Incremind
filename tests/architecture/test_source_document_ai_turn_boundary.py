from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src/backend/api/source_document_ai_runtime.py"
COMPOSITION = ROOT / "src/backend/memory_app/kernel/ai_runtime.py"
PRODUCT = ROOT / "src/core/product_core/source_template_document.py"
LEGACY_ROUTE = ROOT / "src/backend/api/routes/product/source_documents.py"


def test_source_document_ai_is_composed_as_one_approval_gated_turn_outcome() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    composition = COMPOSITION.read_text(encoding="utf-8")

    assert 'SOURCE_DOCUMENT_DRAFT_OUTCOME = "source.document.draft"' in runtime
    assert 'SOURCE_EVIDENCE_CAPABILITY = "source.evidence.read"' in runtime
    assert 'DOCUMENT_DRAFT_PROPOSE_CAPABILITY = "document.draft.propose"' in runtime
    assert 'egress_purpose="document_draft"' in composition
    assert "SourceDocumentDraftPlanner(" in composition
    assert "DOCUMENT_DRAFT_PROPOSE_CAPABILITY," in composition
    assert '"write",\n            True,\n            "receipt_required"' in composition


def test_source_document_ai_keeps_model_and_memory_writes_out_of_product_seams() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    product = PRODUCT.read_text(encoding="utf-8")
    writer = product[product.index("class ApprovedSourceDocumentDraftWriter") : product.index("class CreateMediaOutputTemplateDocument")]

    assert "ModelGatewayPort" in runtime
    assert '"role": "system"' in runtime
    assert '"role": "user"' in runtime
    assert "complete_json(" not in writer
    assert '"memory_candidates"' not in writer
    assert '"long_term_memory_publication"' in product
    assert 'expected_revision=expected_source_revision' in writer


def test_provider_template_legacy_route_is_only_a_turn_action_adapter() -> None:
    routes = LEGACY_ROUTE.read_text(encoding="utf-8")
    endpoint = routes[
        routes.index("async def provider_source_template_document") :
        routes.index("async def media_output_template_document")
    ]

    assert "CreateProviderEnhancedSourceTemplateDocument" not in endpoint
    assert "_build_deepseek_provider_for_rebuild" not in endpoint
    assert "get_or_build_ai_runtime" in endpoint
    assert "SOURCE_DOCUMENT_DRAFT_OUTCOME" in endpoint
    assert "DOCUMENT_DRAFT_PROPOSE_CAPABILITY" in endpoint
    assert '"type": "approve"' in endpoint
