"""Dual-gated authority seed for the packaged expert-Turn visibility Gate.

This module only seeds the existing production authorities in an isolated E2E
Vault.  It has no production default: Electron must first authorize the mixed
media fixture, and the sidecar must then receive its fresh desktop nonce.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.model_route_context import model_route_provider_context_from_record
from backend.model_routing_profile import ModelRoutingProfileStore
from backend.providers import ProviderRegistry
from core.memory_core import ObjectStoreMemoryStore
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.project_skill_core import ProjectSkillUpdate
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    default_video_research_expert_profile,
)
from core.product_core.model_route_registry import ModelRouteRegistry, ModelRouteRegistryNotFound
from core.product_core.model_route_runtime import ModelRouteRuntimeService


_PROJECT_ID = "default"
_EXPERT_ID = "workbench-question-expert"
_PROVIDER_ID = "expert-turn-e2e-local"
_ROUTE_KEY = "search.answer"
_RUN_TOKEN_ENV = "CHRIPTMAS_E2E_MIXED_MEDIA_RUN_TOKEN"
_EXPERT_TOKEN_ENV = "CHRIPTMAS_E2E_EXPERT_TURN_RUN_TOKEN"
_FIXTURE_NONCE_ENV = "CHRIPTMAS_E2E_MIXED_MEDIA_FIXTURE_NONCE"
_DESKTOP_NONCE_ENV = "CHRIPTMAS_DESKTOP_NONCE"
_PROVIDER_ORIGIN_ENV = "CHRIPTMAS_E2E_EXPERT_TURN_PROVIDER_ORIGIN"


def install_expert_turn_e2e_fixture(root_dir: Path) -> bool:
    """Seed an isolated expert path only when both inherited gates agree."""

    run_token = os.environ.get(_RUN_TOKEN_ENV, "")
    expert_token = os.environ.get(_EXPERT_TOKEN_ENV, "")
    fixture_nonce = os.environ.get(_FIXTURE_NONCE_ENV, "")
    desktop_nonce = os.environ.get(_DESKTOP_NONCE_ENV, "")
    provider_origin = _validated_provider_origin(os.environ.get(_PROVIDER_ORIGIN_ENV, ""))
    if (
        not run_token
        or run_token != expert_token
        or not fixture_nonce
        or fixture_nonce != desktop_nonce
        or provider_origin is None
    ):
        return False

    root = Path(root_dir).resolve()
    _ensure_expert_binding(root)
    _ensure_model_route(root, provider_origin)
    _ensure_workbench_question_evidence(root)
    return True


def _ensure_expert_binding(root: Path) -> None:
    catalog = ExpertCatalog(root)
    expert = catalog.get(_EXPERT_ID)
    if expert is None:
        expert = catalog.create(
            default_video_research_expert_profile() | {
                "expert_id": _EXPERT_ID,
                "role": "项目问答与证据整理",
                "method": "先读取已授权项目资料，再按可追溯证据回答。",
                "skills": [{"skill_id": "workbench-question", "revision": 1}],
                "tools": ["workbench.question.answer"],
                "applicable_tasks": ["workbench.question.answer"],
                "output_contract": "只输出可追溯的项目问答与待审核记忆候选。",
                "differentiation": "仅用于工作台直接问答，不处理媒体任务。",
                "status": "active",
            },
            expected_registry_revision=catalog.registry_revision,
        )
    bindings = ExpertProjectBindingStore(root)
    if bindings.get(_PROJECT_ID, _EXPERT_ID) is None:
        bindings.bind(
            _PROJECT_ID,
            _EXPERT_ID,
            catalog=catalog,
            enabled_expert_revision=int(expert["revision"]),
            selection_mode="manual",
            default=True,
            reason="packaged expert Turn fixture",
            expected_store_revision=bindings.store_revision,
        )


def _ensure_workbench_question_evidence(root: Path) -> None:
    """Publish a tiny, governed evidence chain through the selected authority.

    It exists only in the nonce-gated disposable Vault and lets the ordinary
    direct-question Tool produce a real evidence ref.  The expert proposal
    runtime then has material to create its pending-review candidate; no fake
    tool output or second candidate authority is introduced.
    """

    store, settings = build_rebuild_object_store(root)
    factory = AggregateRepositoryFactory(
        runtime_root=root,
        namespace_id=settings.namespace_id,
        json_store=store,
    )
    resolution = factory.memory_publication_authority_resolution()
    memory = ObjectStoreMemoryStore(store)
    source_refs = [{"source_id": "expert-turn-e2e-source", "locator": "fixture:verified"}]
    payloads = (
        ("atom", {
            "schema_version": "1.0.0", "id": "expert-turn-e2e-atom",
            "source_id": "expert-turn-e2e-source",
            "content": "受控工作台专家验证资料：直接问答必须基于项目证据并生成待审核候选。",
            "atom_type": "fact", "tags": ["expert", "workbench"], "confidence": 1.0,
            "source_refs": source_refs, "revision": 1,
            "created_at": "2026-08-28T00:00:00+00:00", "updated_at": "2026-08-28T00:00:00+00:00",
            "trust_status": "user_confirmed",
        }),
        ("scenario", {
            "schema_version": "1.0.0", "id": "expert-turn-e2e-scenario",
            "title": "工作台专家验证场景", "summary": "用于验证受控专家的直接问答证据链。",
            "atom_ids": ["expert-turn-e2e-atom"], "source_refs": source_refs,
            "tags": ["expert", "workbench"], "series_id": "expert-turn-e2e-series",
            "project_id": _PROJECT_ID, "stale": False, "stale_reason": None, "revision": 1,
            "created_at": "2026-08-28T00:00:00+00:00", "updated_at": "2026-08-28T00:00:00+00:00",
            "trust_status": "user_confirmed",
        }),
        ("series_memory", {
            "schema_version": "1.0.0", "id": "expert-turn-e2e-series-memory",
            "series_id": "expert-turn-e2e-series", "scope": "project",
            "overview": "工作台专家直接问答以受控项目证据为依据。",
            "scenario_ids": ["expert-turn-e2e-scenario"], "source_refs": source_refs,
            "project_ids": [_PROJECT_ID], "stale": False, "stale_reason": None, "revision": 1,
            "created_at": "2026-08-28T00:00:00+00:00", "updated_at": "2026-08-28T00:00:00+00:00",
            "trust_status": "user_confirmed",
        }),
    )
    collections = {
        "atom": "memory_atoms",
        "scenario": "memory_scenarios",
        "series_memory": "memory_series_memory",
    }
    if resolution.records is not None:
        # Fresh packaged Vaults may complete the production SQLite cutover
        # before AI runtime composition.  Seed the already-selected authority
        # in one short CAS transaction instead of writing the retired JSON side.
        with resolution.records.begin() as transaction:
            for layer, payload in payloads:
                object_id = str(payload["id"])
                collection = collections[layer]
                if transaction.read(collection, object_id) is None:
                    transaction.put(collection, object_id, payload, expected_revision=0)
            transaction.commit()
    else:
        for layer, payload in payloads:
            object_id = str(payload["id"])
            if memory.get(layer, object_id) is None:
                memory.publish(layer, payload)
    skills = factory.project_skill_repository()
    if skills.load(_PROJECT_ID) is None:
        source_refs = [{"source_id": "expert-turn-e2e-source", "locator": "fixture:verified"}]
        skills.save(ProjectSkillUpdate(
            project_id=_PROJECT_ID,
            markdown="# 工作台专家验证\n\n直接问答必须基于项目证据并生成待审核候选。",
            structured={
                "schema_version": "1.0.0", "id": "skill-default", "project_id": _PROJECT_ID,
                "name": "工作台专家验证 Skill", "purpose": "提供受治理直接问答的项目证据。",
                "markdown_uri": "crp://default/projects/default/project-skill.md",
                "json_uri": "crp://default/projects/default/project-skill.json",
                "markdown_revision": 1, "json_revision": 1, "required_context": [],
                "output_rules": [{
                    "rule_id": "rule-expert-evidence", "origin": "user",
                    "rule": "直接问答必须基于项目证据并生成待审核候选。",
                    "priority": "must", "source_refs": source_refs, "locked_by_user": True,
                }],
                "style_preferences": {"voice": "直接、具体", "format_defaults": ["Markdown", "来源引用"]},
                "update_rules": {
                    "patch_strategy": "patch_existing_first", "user_edit_policy": "user_wins",
                    "allowed_auto_updates": [],
                },
                "source_refs": source_refs, "evidence_refs": source_refs, "decision_log": [],
                "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
                "revision": 1, "status": "active", "trust_status": "user_confirmed",
                "created_at": "2026-08-28T00:00:00+00:00", "updated_at": "2026-08-28T00:00:00+00:00",
            },
            expected_revision=0,
            reason="packaged expert Turn fixture",
        ))


def _validated_provider_origin(value: str) -> str | None:
    """Accept only this E2E run's anonymous loopback OpenAI-compatible URL."""

    candidate = value.strip().rstrip("/")
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.path != "/v1"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return candidate


def _ensure_model_route(root: Path, provider_origin: str) -> None:
    providers = ProviderRegistry(root)
    try:
        provider = providers.get_readonly(_PROVIDER_ID, fallback={})
    except KeyError:
        provider = providers.create(
            {
                "provider_id": _PROVIDER_ID,
                "name": "packaged-expert-turn-fixture",
                "llm_provider": "openai",
                "base_url": provider_origin,
                "api_path": "/chat/completions",
                "model": "expert-turn-fixture-model",
                "models": ["expert-turn-fixture-model"],
                "enabled": True,
            },
            fallback={},
        )
    if provider.get("base_url") != provider_origin:
        provider = providers.update(
            _PROVIDER_ID,
            {"base_url": provider_origin},
            fallback={},
        )
    registry = ModelRouteRegistry(root)
    try:
        registry.get(_ROUTE_KEY)["route"]
    except ModelRouteRegistryNotFound:
        registry.update(
            _ROUTE_KEY,
            {
                "provider_id": _PROVIDER_ID,
                "model_name": "expert-turn-fixture-model",
                "adapter_kind": "openai-compatible",
                "enabled": True,
                "reason": "packaged expert Turn fixture",
            },
            expected_registry_revision=int(registry.list()["registry_revision"]),
            provider=provider,
            egress_consented=True,
        )
    context = model_route_provider_context_from_record(root, provider)
    runtime = ModelRouteRuntimeService(root)
    status = runtime.status()
    if status.get("mode") != "active" or _ROUTE_KEY not in status.get("route_keys", []):
        preview = runtime.preview(
            route_keys=[_ROUTE_KEY],
            compatibility={_ROUTE_KEY: context},
            providers=[context],
        )
        runtime.activate(
            shadow_token=str(preview["shadow_token"]),
            route_keys=preview["route_keys"],
            expected_runtime_revision=int(preview["runtime_revision"]),
            confirm=True,
            compatibility={_ROUTE_KEY: context},
            providers=[context],
        )
    profile = ModelRoutingProfileStore(root).get()
    if not profile.persisted:
        ModelRoutingProfileStore(root).update(
            expected_revision=profile.profile.revision,
            rules_version=1,
            text_default_tier="standard",
            tier_routes={
                "fast": None,
                "standard": _ROUTE_KEY,
                "deep": None,
                "vision": None,
                "image_generation": None,
            },
        )
