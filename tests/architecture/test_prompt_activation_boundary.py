from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "src" / "backend" / "api" / "routes" / "product" / "source_documents.py"
ACTIVATION = ROOT / "src" / "core" / "product_core" / "prompt_activation.py"
SOURCE_DOCUMENT_AI = ROOT / "src" / "backend" / "api" / "source_document_ai_runtime.py"


def test_activation_units_exactly_match_verified_production_prompt_ids() -> None:
    module = ast.parse(ACTIVATION.read_text(encoding="utf-8"))
    assignment = next(
        node for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "PROMPT_ACTIVATION_UNITS"
    )
    units = ast.literal_eval(assignment.value)

    assert units == {
        "companion.chat": ("pt-companion-character",),
        "intake.classification": ("pt-input-understanding",),
        "source.template-document": (
            "pt-title",
            "pt-detail-summary",
            "pt-longterm-organize",
            "pt-output-validate",
        ),
    }


def test_production_consumers_use_active_helpers_and_metadata_consumers_use_drafts() -> None:
    routes = ROUTES.read_text(encoding="utf-8")
    source_template = routes[
        routes.index("async def source_template_document"):
        routes.index("async def provider_source_template_document")
    ]
    provider_template = routes[
        routes.index("async def provider_source_template_document"):
        routes.index("async def media_output_template_document")
    ]
    media_template = routes[
        routes.index("async def media_output_template_document"):
        routes.index("async def source_content_qa_recall")
    ]
    source_document_ai = SOURCE_DOCUMENT_AI.read_text(encoding="utf-8")

    assert "_developer_studio_prompts(" in source_template
    assert "_developer_studio_draft_prompts(" not in source_template
    assert "_developer_studio_prompts(" not in provider_template
    assert "_developer_studio_draft_prompts(" not in provider_template
    assert "_active_prompt_context(self._store)" in source_document_ai
    assert "resolve_active_prompt(config, prompt_id)" in source_document_ai
    assert '"source": "developer_studio_active"' in source_document_ai
    assert "_developer_studio_draft_prompts(" in media_template
    assert '"pt-video-summary"' in media_template

    catalog = (ROOT / "src/backend/api/routes/product/developer_prompt_catalog.py").read_text(encoding="utf-8")
    active_helper = catalog[
        catalog.index("def _developer_studio_prompt("):
        catalog.index("def _developer_studio_draft_prompt(")
    ]
    assert "resolve_active_prompt(config, prompt_id)" in active_helper
    assert '"source": "developer_studio_active"' in active_helper
