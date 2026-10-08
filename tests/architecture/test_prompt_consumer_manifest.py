from __future__ import annotations

import ast

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "core" / "product_core" / "source_template_document.py"
CLASSIFIER = ROOT / "src" / "core" / "product_core" / "workbench_input_classifier.py"
WORKBENCH_AI = ROOT / "src" / "backend" / "api" / "workbench_ai_runtime.py"
SOURCE_DOCUMENT_AI = ROOT / "src" / "backend" / "api" / "source_document_ai_runtime.py"


def test_structuring_prompt_refs_strip_content_and_do_not_claim_provider_execution() -> None:
    catalog = (ROOT / "src/backend/api/routes/product/developer_prompt_catalog.py").read_text(encoding="utf-8")
    block = catalog[catalog.index("def _developer_studio_prompt_refs") : catalog.index("def _developer_studio_prompts")]
    routes = (ROOT / "src/backend/api/routes/product/source_content.py").read_text(encoding="utf-8")
    assert 'if key in {"id", "revision", "source", "stage_id", "model_profile_id"}' in block
    assert '"content"' not in block
    structure_call = routes[routes.index("async def source_content_structure") : routes.index("@router", routes.index("async def source_content_structure") + 1)]
    for prompt_id in ("pt-classification", "pt-summary", "pt-tags", "pt-longterm-organize"):
        assert prompt_id in structure_call


def test_only_source_document_turn_runtime_consumes_the_four_document_prompt_bodies() -> None:
    routes = (ROOT / "src/backend/api/routes/product/source_documents.py").read_text(encoding="utf-8")
    templates = TEMPLATES.read_text(encoding="utf-8")
    source_document_ai = SOURCE_DOCUMENT_AI.read_text(encoding="utf-8")
    provider_route = routes[routes.index("async def provider_source_template_document") : routes.index("@router", routes.index("async def provider_source_template_document") + 1)]
    for prompt_id in ("pt-title", "pt-detail-summary", "pt-longterm-organize", "pt-output-validate"):
        assert prompt_id in source_document_ai
        assert prompt_id not in provider_route
    assert "resolve_active_prompt(config, prompt_id)" in source_document_ai
    assert "source_document_ai_system_prompt(evidence)" in source_document_ai
    assert "CreateProviderEnhancedSourceTemplateDocument" not in provider_route
    assert "_build_deepseek_provider_for_rebuild" not in provider_route
    assert "get_or_build_ai_runtime" in provider_route
    assert 'lines.append(f"- {prompt_id}: {content}")' in templates
    deterministic = templates[templates.index("class CreateSourceTemplateDocument") : templates.index("class CreateProviderEnhancedSourceTemplateDocument")]
    assert "_prompt_refs(prompt_context)" in deterministic
    assert "_provider_prompt_lines(prompt_context)" not in deterministic


def test_input_understanding_content_is_bounded_append_only_and_uses_model_route() -> None:
    classifier = CLASSIFIER.read_text(encoding="utf-8")
    classifier_runtime = (ROOT / "src/backend/api/workbench_input_classifier_runtime.py").read_text(encoding="utf-8")
    auto_intake_runtime = (ROOT / "src/backend/api/workbench_auto_intake_runtime.py").read_text(encoding="utf-8")
    assert 'return f"{base}\\n\\nDeveloper Studio 输入理解提示词：\\n{custom}"' in classifier
    assert "_safe_preview(content, limit=2400)" in classifier
    assert "resolve_active_prompt(config, INPUT_CLASSIFIER_PROMPT_ID)" in classifier_runtime
    assert "Legacy classifier provider enhancement is retired" in classifier_runtime
    classifier_ai = (ROOT / "src/backend/api/workbench_input_classifier_ai_runtime.py").read_text(encoding="utf-8")
    assert "load_turn_model_routing_binding" in classifier_ai
    assert "begin_nested_model_call" in classifier_ai
    assert "classifier_runtime.active_prompt()" in auto_intake_runtime


def test_answer_prompt_is_detailed_bounded_and_uses_search_answer_route() -> None:
    runtime = WORKBENCH_AI.read_text(encoding="utf-8")
    composition = (ROOT / "src" / "backend" / "memory_app" / "kernel" / "ai_runtime.py").read_text(encoding="utf-8")
    assert 'resolve_active_prompt(config, "pt-answer")' in runtime
    assert 'developer_instruction=resolve_workbench_answer_instruction(store)' in composition
    assert 'capability="structured"' in runtime
    assert any(isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("Answer only from the supplied evidence.") for node in ast.walk(ast.parse(runtime)))
    assert '"output": {"answer": "string", "citations": ["evidence reference"]}' in runtime
    assert "_validated_model_answer" in runtime
    assert "citations" in runtime and "item not in allowed" in runtime
    assert "_selected_route_key(model_result)" in runtime
    assert 'return "search.answer"' in runtime


def test_test_lab_resolves_exact_prompt_snapshot_and_pipeline_uses_built_in_prompt() -> None:
    routes = (ROOT / "src/backend/api/routes/product/developer_test_lab.py").read_text(encoding="utf-8")
    resolver = routes[routes.index("def _test_lab_prompt_snapshot") : routes.index('@router.post("/api/rebuild/developer-studio/test-lab")')]
    endpoint = routes[routes.index("async def developer_studio_test_lab") : len(routes)]
    assert 'if source == "draft":' in resolver
    assert "resolve_active_prompt(config, prompt_id)" in resolver
    assert 'prompt_content = _default_test_lab_system_prompt(test_type)' in endpoint
    assert '"system_prompt": prompt_content' in endpoint
    assert '"desired_outcome": DEVELOPER_STUDIO_TEST_LAB_OUTCOME' in endpoint
    assert 'expected_config_revision=product_http._required_body_int(body, "expected_config_revision")' in endpoint
