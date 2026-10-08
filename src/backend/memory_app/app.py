"""Web-only API composition for the recognition workbench.

The preserved application remains the base app.  This module adds a small,
explicit local-workbench router without making the legacy routes depend on the
new recognition lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from threading import RLock
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from backend.recognition import (
    MAX_CONTENT_CHARS, Recognition,
    RecognitionConflict, RecognitionError, RecognitionService, WorkScope,
)
from backend.recognition_retrieval import RecognitionRetrievalError
from backend.security.secrets import ServerSecretStoreError
from backend.security.device_auth import ServerDeviceAuth, install_device_authentication, same_origin
from backend.security.device_identity import server_mode
from backend.shared.deployment import DeploymentLayout, resolve_deployment
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.runtime import DocumentRepositoryError
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict

from .model_config import ModelConfiguration, ModelConfigurationError
from .context_adapter import ContextSelectionError
from .retrieval_models import configured_adapter
from .graph import memory_graph
from .graph_view_api import install_graph_view_routes
from .migration_bundle import read_imported_history
from .relations import RelationProposalService
from .restructure_api import install_restructure_routes
from .erasure import ErasureService
from .recall_state import preference
from .v2.recall_preferences import set_preference
from .task_status import get_task_status
from .constraints import ProjectConstraintService
from .turn_dispatch import RecognitionTurnDispatcher
from .source_egress import SourceEgressService, SourcePrivacyInputError, recognition_service
from .workspace import install_workspace_routes
from .v2 import install_v2_routes
from .v2.intake_media import UPLOAD_REQUEST_BYTES
from .storage_authority import resolve_recognition_document_store
from .document_visibility import recognition_document_visible as _document_visible
from .packet_verification import _verify_packet_current, _now
from .turn_installation import install_recognition_turn_capability
from .local_model import install_local_model_routes


_PREFIX = "/api/recognition"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_REQUEST_BYTES = 128 * 1024
# A supplementary Unicode character can occupy 12 bytes in escaped JSON.
# Leave the existing envelope budget for conditions and Markdown identity.
_MAX_RECOGNITION_EDIT_BYTES = 12 * MAX_CONTENT_CHARS + _MAX_REQUEST_BYTES
_MAX_TEXT = 20_000


def create_app(
    *,
    runtime_root: Path | None = None,
    legacy_app: FastAPI | None = None,
    model_configuration: ModelConfiguration | None = None,
    deployment_layout: DeploymentLayout | None = None,
    server_context=None,
) -> FastAPI:
    """Return the preserved API application with the recognition router added."""

    root = (runtime_root or _runtime_root()).resolve()
    layout = deployment_layout or resolve_deployment(root)
    if layout.mode == 'desktop':
        layout = DeploymentLayout('desktop', root)
    elif layout.user_root != root:
        raise ValueError('deployment_runtime_conflict')
    if layout.mode == 'server' and model_configuration is not None:
        # An injected model owner must never have its credential callback
        # overwritten. Default server assembly owns the callback below.
        raise ValueError('server_model_identity_required')
    if server_context is not None:
        if layout.mode != 'server' or layout.server_root != server_context.layout.server_root:
            raise ValueError('deployment_runtime_conflict')
        user_id = root.name
        if server_context.users.root_for(user_id) != root:
            raise ValueError('deployment_runtime_conflict')
    root.mkdir(parents=True, exist_ok=True)
    records, document_namespace = resolve_recognition_document_store(root)
    if server_context is not None:
        from .server_audit import AuditedRecordStore
        records = AuditedRecordStore(records, namespace=document_namespace, user_id=user_id)
    application = legacy_app or _legacy_application(root, server_context=server_context)
    legacy_container = getattr(application.state, "container", None)
    if legacy_container is not None and Path(legacy_container.root_dir).resolve() != root:
        raise RuntimeError("legacy_recognition_runtime_root_mismatch")
    from .cache_sources import prepare_sources
    service = recognition_service(records, cache_invalidation=prepare_sources)
    from .v2.devices import DeviceRegistry
    device_registry = server_context.registry if server_context is not None else DeviceRegistry((layout.server_root or root) / 'server')
    device_auth = server_context.auth if server_context is not None else ServerDeviceAuth(device_registry)
    application.state.deployment = layout
    application.state.device_registry = device_registry
    internal_provider = None
    if server_context is not None:
        internal_provider = lambda url: device_auth.internal_key_for_user(user_id, url)
        application.state.server_context = server_context
        application.state.server_user_id = user_id
        application.state.server_jobs = server_context.jobs
        application.state.server_users = server_context.users
    elif layout.mode == 'server':
        internal_provider = device_auth.internal_key_for
    server_secrets = getattr(getattr(application.state,'container',None),'secret_store',None) if server_context is not None else None
    models = model_configuration or ModelConfiguration(records, root, secrets=server_secrets, internal_local_key_provider=internal_provider,
        local_models_root=server_context.resources.model_root if server_context is not None else None)
    from .v2.embedding_settings import EmbeddingSettings, vector_policy
    embeddings = EmbeddingSettings(records, getattr(models, '_local_models_root', root / 'data' / 'models'))
    application.state.memory_embedding_settings = embeddings
    if isinstance(models, ModelConfiguration):
        models.bind_embedding(embeddings.project, vector_policy)
    documents = SQLiteDocumentRepository(records, namespace_id=document_namespace)
    # A single local server owns task execution. In-flight calls cannot resume
    # after process exit; do not silently issue the model request again.
    with records.begin() as tx:
        for record in tx.list("recognition_tasks"):
            # Old direct calls had no durable Turn identity and cannot be
            # resumed safely. Governed Turn tasks are recovered by their own
            # authority; never blindly resubmit them here.
            if record.payload.get("state") == "running" and not record.payload.get("turn_id"):
                tx.put("recognition_tasks", record.object_id,
                       {**record.payload, "state": "interrupted", "finished_at": _now()}, expected_revision=record.revision)
        tx.commit()
    from .research_sources import ReadRegistry, ReadPlanner
    application.state.source_privacy_registry_wrapper = lambda registry: ReadRegistry(registry,records)
    application.state.source_privacy_planner_wrapper = lambda planner,turns,agents: ReadPlanner(planner,records,turns,agents)
    application.state.recognition_records = records
    application.state.recognition_service = service
    application.state.recognition_turn_installer = install_recognition_turn_capability
    application.state.recognition_models = models
    application.state.recognition_documents = documents
    application.state.recognition_document_namespace = document_namespace
    application.state.recognition_mutation_lock = RLock()
    application.state.recognition_runtime_root = root
    install_local_model_routes(
        application, runtime_root=root,
        generation_allowed=getattr(models, "local_generation_allowed", lambda: False),
        embedding_policy_reader=vector_policy,
    )
    ErasureService(records, root).recover_pending()
    application.add_exception_handler(RecognitionError, _recognition_error)
    application.add_exception_handler(ModelConfigurationError, _model_error)
    application.add_exception_handler(ServerSecretStoreError, _server_secret_error)
    application.add_exception_handler(RecognitionRetrievalError, _retrieval_error)
    application.add_exception_handler(SQLiteUnitOfWorkConflict, _sqlite_conflict)
    application.add_exception_handler(DocumentRepositoryError, _document_error)
    application.add_exception_handler(ContextSelectionError, _context_error)
    if server_context is not None:
        from .server_jobs import QuotaError
        from backend.shared.server_resources import SharedModelUnavailable
        async def quota_error(_request, error):
            return _error(429, str(error))
        async def unavailable_model(_request, error):
            return _error(503, str(error))
        application.add_exception_handler(QuotaError, quota_error)
        application.add_exception_handler(SharedModelUnavailable, unavailable_model)
    if not getattr(application.state, "recognition_origin_guard_installed", False):
        @application.middleware("http")
        async def local_origin_guard(request: Request, call_next):
            if request.url.path.startswith((_PREFIX, "/api/workspace/v1", "/api/v2")) and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                try:
                    maximum = (UPLOAD_REQUEST_BYTES if request.url.path.startswith("/api/workspace/v1/items/file") or request.url.path == "/api/v2/workbench/files"
                               else _recognition_body_limit(request) if request.url.path.startswith(_PREFIX)
                               else 128 * 1024)
                    if int(request.headers.get("content-length", "0")) > maximum:
                        return _error(413, "request_too_large")
                except ValueError:
                    return _error(400, "invalid_content_length")
                origin = request.headers.get("origin")
                if origin and not (same_origin(request) if server_mode(request) else _is_local_origin(origin)):
                    return _error(403, "local_origin_required")
            return await call_next(request)
        application.state.recognition_origin_guard_installed = True
    dispatcher = RecognitionTurnDispatcher(application=application, runtime_root=root, records=records,
                                           mutation_lock=application.state.recognition_mutation_lock)
    application.state.recognition_turn_dispatcher = dispatcher
    recognition_router = _router(service, models, documents, mutation_lock=application.state.recognition_mutation_lock, dispatcher=dispatcher)
    application.include_router(recognition_router)
    domains = install_workspace_routes(application, runtime_root=root, records=records, models=models,
                                       documents=documents, service=service)
    application.state.workspace_domains = domains
    install_v2_routes(application, runtime_root=root, records=records, models=models,
                      documents=documents, service=service, workspace=domains)
    if documents.namespace_id == 'default':
        from .v2.signal_reviews import install_signal_review_routes
        install_signal_review_routes(application, records=records, service=service,
                                     documents=documents, runtime_root=root)
    install_device_authentication(application, registry=device_registry, auth=device_auth)
    from backend.shared.runtime_logging import install_runtime_logging
    install_runtime_logging(application, layout)
    _preload_model_client()
    return application


def _preload_model_client():
    """Import LiteLLM in the background: the first import takes ~10 s and otherwise
    lands inside the first user turn, where routing has an 8 s deadline."""
    import threading

    def load():
        try:
            import litellm  # noqa: F401
        except Exception:
            pass

    threading.Thread(target=load, name="litellm-preload", daemon=True).start()


def _legacy_application(root: Path, *, server_context=None) -> FastAPI:
    # Import only for the production composition.  Tests inject a small base
    # application, while the web entry point retains every old router.
    from backend.api.app import create_app as create_legacy_app
    from backend.api.bootstrap import build_api_container

    if server_context is not None:
        return create_legacy_app(build_api_container(root, shared_resources=server_context.resources))
    return create_legacy_app(build_api_container(root))


def create_server_app(*, layout=None, port=8001):
    """Build one server authority; children are initialized only when selected."""
    from .server_runtime import create_server_application
    from backend.shared.server_resources import resource_context
    layout = layout or resolve_deployment(Path(__file__).resolve().parents[3] / 'runtime')
    if layout.mode != 'server':
        raise ValueError('server_mode_required')
    def child(root, user_id, context):
        config = root / 'config' / 'settings.toml'
        config.parent.mkdir(parents=True, exist_ok=True)
        if not config.exists():
            with config.open('xb') as output:
                output.write((Path(__file__).resolve().parents[3] / 'config/settings.toml.example').read_bytes())
        with resource_context(context.resources):
            return create_app(runtime_root=root, deployment_layout=DeploymentLayout('server', root, layout.server_root), server_context=context)
    return create_server_application(layout, child_factory=child, port=port)


def _runtime_root() -> Path:
    from backend.api.runtime_root_config import resolve_application_runtime_root

    fallback = Path(__file__).resolve().parents[3] / "runtime"
    return resolve_application_runtime_root(fallback)


def _router(service: RecognitionService, models: ModelConfiguration, documents: SQLiteDocumentRepository, *, mutation_lock: RLock,
            dispatcher: RecognitionTurnDispatcher) -> APIRouter:
    router = APIRouter(prefix=_PREFIX)
    relations = RelationProposalService(service.records)
    constraints = ProjectConstraintService(service.records)
    erasure = ErasureService(service.records, service.records.database_path.parent)
    egress = SourceEgressService(service.records)

    def mutate(operation, /, *args, **kwargs):
        # Background Turn commits and user source/config changes share this
        # short synchronous boundary. Never hold it over model/network awaits.
        with mutation_lock:
            return operation(*args, **kwargs)

    install_restructure_routes(router, service.records, mutate=mutate, read_body=_body,
                               scope_for=_scope, revision=_revision, text=_text, service=service, models=models)

    install_graph_view_routes(router, service.records, mutate=mutate, read_body=_body, scope_for=_scope)

    @router.get("/source-policies/{source_type}/{source_id}")
    async def source_policy_get(source_type: str, source_id: str, revision: int, project_id: str = "default"):
        return egress.snapshot(_scope(project_id), [{"type": source_type, "id": source_id, "revision": revision}])

    @router.put("/source-policies/{source_type}/{source_id}")
    async def source_policy_put(source_type: str, source_id: str, request: Request):
        body = await _body(request)
        try:
            return mutate(egress.set_policy, _scope(body.get("project_id")), source_type, source_id,
                          body.get("expected_source_revision"), body.get("expected_policy_revision"),
                          body.get("allowed_purposes"))
        except SourcePrivacyInputError as exc:
            return _error(400, str(exc))


    @router.get("/constraints")
    async def constraints_get(project_id: str = "default"):
        return {"items": constraints.list(_scope(project_id))}

    @router.put("/constraints/{constraint_id}")
    async def constraint_save(constraint_id: str, request: Request):
        body = await _body(request)
        if set(body).difference({"project_id", "expected_revision", "content", "enabled", "valid_from", "valid_until"}):
            raise RecognitionError("project constraint contains unsupported fields")
        revision = body.get("expected_revision")
        if type(revision) is not int or revision < 0:
            raise RecognitionError("expected_revision is invalid")
        return mutate(constraints.upsert, _scope(body.get("project_id")), constraint_id, revision,
                      _text(body.get("content"), "constraint content"), enabled=body.get("enabled", True),
                      valid_from=body.get("valid_from"), valid_until=body.get("valid_until"))

    @router.get("/settings")
    async def settings_get():
        return _public_settings(models)

    @router.put("/settings")
    async def settings_put(request: Request):
        body = await _body(request)
        purpose = _text(body.get("purpose"), "purpose", maximum=32)
        payload = {
            "base_url": body.get("base_url", ""), "model": body.get("model", ""),
            "api_key": body.get("api_key", ""), "allow_remote": body.get("allow_remote", False),
            "clear_api_key": body.get("clear_api_key", False), "expected_revision": body.get("expected_revision"),
            "enabled": body.get("enabled", False),
        }
        return mutate(models.update, purpose, payload)

    @router.put("/settings/generation-mode")
    async def generation_mode_put(request: Request):
        body = await _body(request)
        return mutate(
            models.update_generation_mode,
            mode=body.get("mode"),
            local_enabled=body.get("local_enabled"),
            local_base_url=body.get("local_base_url", models.generation_mode()["local_base_url"]),
            expected_revision=body.get("expected_revision"),
        )

    @router.post("/settings/test")
    async def settings_test(request: Request):
        body = await _body(request)
        purpose = _text(body.get("purpose"), "purpose", maximum=32)
        public = models.public().get(purpose)
        if not public or not public.get("configured"):
            return {"purpose": purpose, "status": "unconfigured", "message": "模型尚未配置，未执行请求。"}
        if purpose != "generation":
            if not hasattr(models, "snapshot"):
                return {"purpose": purpose, "status": "not_verified", "message": "注入服务未提供模型连接。"}
            adapter = configured_adapter(models, purpose)
            if purpose == "embedding":
                vectors = await run_in_threadpool(adapter.embed, ["合成测试：网页优先", "合成测试：检查记忆版本"])
                if not vectors or not vectors[0]:
                    raise RecognitionRetrievalError("embedding response is empty")
                return {"purpose": purpose, "status": "complete", "dimensions": len(vectors[0]), "message": "向量协议连接验证通过；尚不代表召回效果通过。"}
            await run_in_threadpool(adapter.rerank, query="网页原型", candidates=[{"id": "test-web", "content": "浏览器验证原型"}, {"id": "test-garden", "content": "植物浇水"}])
            return {"purpose": purpose, "status": "complete", "message": "重排协议连接验证通过；尚不代表排序效果通过。"}
        await run_in_threadpool(models.complete, [{"role": "user", "content": "Reply only with OK."}], max_tokens=256)
        return {"purpose": purpose, "status": "complete", "message": "生成模型已完成显式连接验证。"}

    @router.get("/workbench")
    async def workbench_get(project_id: str = "default"):
        scope = _scope(project_id)
        # Refresh the durable Turn projection before returning the task list;
        # a reloaded browser can therefore receive the current approval token.
        for task in service.records.list("recognition_tasks"):
            if task.payload.get("project_id") == scope.project_id:
                dispatcher.sync(task_id=task.object_id, scope=scope)
        return _workbench(service, models, scope, document_namespace=documents.namespace_id)

    @router.get("/graph")
    async def graph_get(project_id: str = "default", focus: str | None = None, offset: int = 0, limit: int = 40):
        return memory_graph(service, _scope(project_id), focus=focus, offset=offset, limit=limit)

    @router.get("/relation-proposals")
    async def relation_list(project_id: str = "default"):
        return {"proposals": relations.list(_scope(project_id))}

    @router.get("/recognitions/{recognition_id}/versions")
    async def recognition_versions(recognition_id: str, project_id: str = "default"):
        if service.get_recognition(scope=_scope(project_id), recognition_id=recognition_id) is None:
            raise RecognitionConflict("recognition history is unavailable in this project")
        versions = [dict(record.payload) for record in service.records.list("recognition_versions")
                    if record.payload.get("recognition_id") == recognition_id]
        return {"versions": sorted(versions, key=lambda value: value["version"], reverse=True),
                "imported_versions": read_imported_history(service.records, _scope(project_id), recognition_id)}

    @router.patch("/recognitions/{recognition_id}/recall")
    async def recall_update(recognition_id: str, request: Request):
        body = await _body(request)
        return set_preference(service.records, _scope(body.get("project_id")), recognition_id,
            recognition_revision=body.get("expected_revision"), preference_revision=body.get("expected_preference_revision"), state=body.get("state"))

    @router.post("/recognitions/{recognition_id}/erase-preview")
    async def erase_preview(recognition_id: str, request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        revision = _revision(body.get("expected_revision"))
        preview = asdict(erasure.preview(scope=scope, recognition_id=recognition_id, expected_revision=revision))
        preview_id = "erase-preview-" + uuid4().hex
        _put_new(service.records, "recognition_erasure_previews", preview_id,
                 {**preview, "project_id": scope.project_id, "expected_revision": revision})
        return {**preview, "preview_id": preview_id,
                "boundary": "清理当前数据库及相关向量缓存；历史备份、下载的 Markdown 和外部副本须单独处理。"}

    @router.post("/recognitions/{recognition_id}/erase")
    async def erase_commit(recognition_id: str, request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        preview_id = _text(body.get("preview_id"), "preview_id", maximum=128)
        preview = service.records.read("recognition_erasure_previews", preview_id)
        revision = _revision(body.get("expected_revision"))
        if (preview is None or preview.payload.get("project_id") != scope.project_id
                or preview.payload.get("recognition_id") != recognition_id
                or preview.payload.get("expected_revision") != revision or body.get("confirm") != recognition_id):
            raise RecognitionConflict("erasure requires its current preview and explicit recognition confirmation")
        return asdict(mutate(erasure.erase, scope=scope, recognition_id=recognition_id, expected_revision=revision,
                                    expected_plan=preview.payload["revisions"]))

    @router.post("/relation-proposals")
    async def relation_propose(request: Request):
        body = await _body(request)
        return relations.propose(_scope(body.get("project_id")), body.get("from_id"), body.get("to_id"), body.get("relation"), body.get("evidence"))

    @router.patch("/relation-proposals/{proposal_id}")
    async def relation_review(proposal_id: str, request: Request):
        body = await _body(request)
        decision = {"approve": "approved", "reject": "rejected"}.get(body.get("decision"))
        if decision is None:
            raise RecognitionError("invalid relation decision")
        return relations.review(_scope(body.get("project_id")), proposal_id, _revision(body.get("expected_revision")), decision)

    @router.post("/experiences")
    async def experience_create(request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        content = _text(body.get("content"), "content")
        if "provenance" in body or "source_refs" in body:
            raise RecognitionError("experience provenance is assigned by the server")
        provenance = {"kind": "user_statement", "actor": "local-user"}
        if "occurred_at" in body:
            provenance["occurred_at"] = body["occurred_at"]
        experience_id = service.stage_experience(scope=scope, content=content, provenance=provenance)
        item = next(item for item in service.list_experiences(scope=scope, include_revoked=True) if item.id == experience_id)
        return _experience_payload(item)


    @router.patch("/candidates/{candidate_id}")
    async def candidate_review(candidate_id: str, request: Request):
        body = await _candidate_body(request)
        scope = _scope(body.get("project_id"))
        expected = _revision(body.get("expected_revision"))
        decision = _text(body.get("decision"), "decision", maximum=16)
        if decision == "approve":
            if "content" in body:
                pending = next((item for item in service.list_candidates(scope=scope) if item.id == candidate_id), None)
                if pending is None or body["content"] != pending.content:
                    raise RecognitionConflict("candidate content changed; save and review the draft before publication")
            recognition = mutate(service.publish, scope=scope, candidate_id=candidate_id, expected_revision=expected, reviewer="local-user")
            return _recognition_payload(service, scope, recognition)
        if decision == "reject":
            return _candidate_payload(service.reject_candidate(scope=scope, candidate_id=candidate_id, expected_revision=expected, reviewer="local-user"))
        raise RecognitionError("decision must be approve or reject")

    @router.patch("/candidates/{candidate_id}/draft")
    async def candidate_edit(candidate_id: str, request: Request):
        body = await _candidate_body(request)
        candidate = service.edit_candidate(scope=_scope(body.get("project_id")), candidate_id=candidate_id,
            expected_revision=_revision(body.get("expected_revision")), content=body.get("content"),
            conditions=body.get("conditions"), editor="local-user")
        return _candidate_payload(candidate)

    @router.get("/recognitions/{recognition_id}/markdown")
    async def markdown_export(recognition_id: str, project_id: str = "default"):
        return PlainTextResponse(service.export_markdown(scope=_scope(project_id), recognition_id=recognition_id), media_type="text/markdown")

    @router.post("/recognitions/{recognition_id}/markdown")
    async def markdown_change(recognition_id: str, request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        expected = _revision(body.get("expected_revision"))
        markdown = _text(body.get("markdown"), "markdown", maximum=_MAX_RECOGNITION_EDIT_BYTES)
        mode = _text(body.get("mode"), "mode", maximum=16)
        preview = service.markdown_preview(scope=scope, markdown=markdown)
        if preview.recognition_id != recognition_id or preview.expected_revision != expected:
            raise RecognitionConflict("markdown metadata conflicts with request version")
        if mode == "preview":
            return {"recognition_id": preview.recognition_id, "expected_revision": preview.expected_revision, "changed": preview.changed, "content": preview.content}
        if mode == "commit":
            return _recognition_payload(service, scope, mutate(service.markdown_commit, scope=scope, markdown=markdown))
        raise RecognitionError("markdown mode is invalid")

    @router.delete("/recognitions/{recognition_id}")
    async def recognition_revoke(recognition_id: str, request: Request):
        body = await _body(request)
        if body.get("mode", "revoke") != "revoke":
            raise RecognitionError("only revoke is supported")
        recognition = mutate(service.revoke, scope=_scope(body.get("project_id")), recognition_id=recognition_id, expected_revision=_revision(body.get("expected_revision")), reason="user revoked recognition")
        return _recognition_payload(service, _scope(body.get("project_id")), recognition)



    @router.post("/recognitions/{recognition_id}/split")
    async def recognition_split(recognition_id: str, request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        parts = body.get("parts")
        if not isinstance(parts, list) or not 2 <= len(parts) <= 12:
            raise RecognitionError("split requires between 2 and 12 parts")
        children = mutate(service.split, scope=scope, recognition_id=recognition_id, expected_revision=_revision(body.get("expected_revision")), parts=parts)
        return {"recognitions": [_recognition_payload(service, scope, item) for item in children]}

    @router.post("/recognitions/merge")
    async def recognition_merge(request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        revisions = body.get("expected_revisions")
        if not isinstance(revisions, dict) or not 2 <= len(revisions) <= 12:
            raise RecognitionError("merge requires between 2 and 12 source versions")
        revisions = {_text(key, "recognition id", maximum=128): _revision(value) for key, value in revisions.items()}
        selection = {
            name: _string_list(body[name], name)
            for name in ("source_experience_ids", "source_recognition_ids")
            if name in body
        }
        if "replacement_conditions" in body:
            if not isinstance(body["replacement_conditions"], list):
                raise RecognitionError("replacement_conditions is invalid")
            selection["replacement_conditions"] = body["replacement_conditions"]
        merged = mutate(service.merge, scope=scope, recognition_ids=tuple(revisions),
            expected_revisions=revisions, content=_text(body.get("content"), "content"),
            conditions=body.get("conditions", ()), **selection)
        return _recognition_payload(service, scope, merged)





    @router.get("/tasks/{task_id}")
    async def task_get(task_id: str, project_id: str = "default"):
        dispatcher.sync(task_id=task_id, scope=_scope(project_id))
        return get_task_status(service.records, _scope(project_id), task_id, document_namespace=documents.namespace_id)







    @router.get("/documents/{document_id}")
    async def document_get(document_id: str, project_id: str = "default"):
        scope = _scope(project_id)
        document = documents.read(document_id)
        if document is None or document.get("project_id") != scope.project_id or not _document_visible(service.records, scope, document_id):
            raise RecognitionConflict("document is unavailable in this project")
        return {**document, "markdown": documents.markdown(document_id)}

    @router.post("/tasks/{task_id}/experience")
    async def task_experience(task_id: str, request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        with mutation_lock:
            task = service.records.read("recognition_tasks", task_id)
            if task is None or task.payload.get("project_id") != scope.project_id or task.payload.get("state") != "completed" or task.payload.get("kind") == "restructure":
                raise RecognitionConflict("completed task is unavailable in this project")
            document = documents.read(task.payload["document_id"])
            if document is None or document.get("project_id") != scope.project_id:
                raise RecognitionConflict("task document is unavailable")
            experience_id = f"experience-{task_id}-r{document['revision']}"
            existing = service.records.read("recognition_experiences", experience_id)
            if existing is not None:
                return {"experience_id": experience_id, "source_task_id": task_id, "already_retained": True}
            markdown = documents.markdown(document["id"], revision=document["revision"])
            if markdown is None:
                raise RecognitionConflict("task document revision is unavailable")
            content = ("用户选择保留的模型生成成果，内容尚需人工事实核验。\n"
                       f"任务：{task.payload['input']}\n来源：task://{task_id}；document://{document['id']}?revision={document['revision']}\n\n"
                       + markdown)
            source_refs = [
                {"type": "task", "id": task_id, "revision": task.revision},
                {"type": "document", "id": document["id"], "revision": document["revision"]},
            ]
            if task.payload.get("context_packet_id"):
                packet = service.records.read("recognition_context_packets", task.payload["context_packet_id"])
                if (packet is None or packet.payload.get("project_id") != scope.project_id
                        or packet.payload.get("task_id") != task_id or packet.payload.get("state") != "consumed"):
                    raise RecognitionConflict("task context packet is unavailable")
                source_refs.append({"type": "context_packet", "id": packet.object_id, "revision": packet.revision})
            if task.payload.get("turn_id"):
                source_refs.append({"type": "turn", "id": task.payload["turn_id"]})
            service.stage_experience(
                scope=scope, content=_text(content, "experience content"), experience_id=experience_id,
                provenance={"kind": "model_generated_artifact", "actor": "agent", "source_refs": source_refs},
            )
            return {"experience_id": experience_id, "source_task_id": task_id, "already_retained": False}

    @router.patch("/documents/{document_id}")
    async def document_edit(document_id: str, request: Request):
        body = await _body(request)
        scope = _scope(body.get("project_id"))
        document = documents.read(document_id)
        if document is None or document.get("project_id") != scope.project_id or not _document_visible(service.records, scope, document_id):
            raise RecognitionConflict("document is unavailable in this project")
        repository = SQLiteDocumentRepository(service.records, namespace_id=documents.namespace_id, now=_now())
        markdown = body.get("markdown")
        _text(markdown, "markdown", maximum=40000)
        from .transaction_records import TransactionRecords
        from .v2.outcome_corrections import record_edit
        expected_revision = _revision(body.get("expected_revision"))
        def save_edit():
            with service.records.begin() as tx:
                enlisted = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=documents.namespace_id, now=_now())
                current = enlisted.read(document_id)
                if current is None or current.get('project_id') != scope.project_id:
                    raise RecognitionConflict("document is unavailable in this project")
                saved = enlisted.save_user_edit(document_id, markdown=markdown, expected_revision=expected_revision)
                record_edit(tx, enlisted, scope=scope, document_id=document_id,
                    from_revision=expected_revision, to_revision=saved['revision'], now=enlisted.now)
                tx.commit()
                return saved
        result = mutate(save_edit)
        from .v2.usage import safe_record_usage
        safe_record_usage(service.records, "document", document_id, scope.project_id, 1.0, reset=True)
        return {**result, "markdown": repository.markdown(document_id)}

    return router


def _workbench(service: RecognitionService, models: ModelConfiguration, scope: WorkScope, *, document_namespace="recognition") -> dict[str, object]:
    recognitions = [_recognition_payload(service, scope, item) for item in service.list_recognitions(scope=scope)]
    questions = [_question_payload(item) for item in service.list_questions(scope=scope)]
    configured = bool(models.public()["generation"]["configured"])
    return {
        "projects": [{"id": scope.project_id or "default", "name": scope.project_id or "默认项目"}],
        "experiences": [_experience_payload(item) for item in service.list_experiences(scope=scope)],
        "candidates": [_candidate_payload(item) for item in service.list_candidates(scope=scope)],
        "recognitions": recognitions,
        "mental_models": questions,
        "recent_tasks": [
            {"id": item.object_id, **get_task_status(service.records, scope, item.object_id, document_namespace=document_namespace)}
            for item in service.records.list("recognition_tasks") if item.payload.get("project_id") == scope.project_id
        ],
        "documents": [
            {"id": item.get("id"), "document_id": item.get("id"), "title": item.get("title"), "summary": str(item.get("markdown_uri", ""))}
            for item in documents_for_project(service.records, scope.project_id)
            if isinstance(item.get("id"), str) and _document_visible(service.records, scope, str(item.get("id")))
        ],
        "graph": memory_graph(service, scope),
        "runtime": {"status": "ready", "model_status": "ready" if configured else "unconfigured", "model_configured": configured},
    }


def _recognition_payload(service: RecognitionService, scope: WorkScope, item: Recognition) -> dict[str, object]:
    return {**item.retrieval_projection(), **preference(service.records, scope, item.id), "markdown": service.export_markdown(scope=scope, recognition_id=item.id), "title": item.content[:80], "scope": scope.project_id or "default"}


def _experience_payload(item) -> dict[str, object]:
    return {"id": item.id, "revision": item.revision, "project_id": item.project_id, "content": item.content, "title": item.content[:80], "summary": item.content[:160], "status": item.state, "provenance": item.provenance.to_payload()}


def _candidate_payload(item) -> dict[str, object]:
    return {"id": item.id, "revision": item.revision, "project_id": item.project_id, "content": item.content,
            "conditions": list(item.conditions), "status": item.state,
            "generation": dict(item.generation) if item.generation is not None else None,
            "source_experience_revisions": dict(item.source_experience_revisions),
            "source_recognition_revisions": dict(item.source_recognition_revisions),
            "source_label": f"{len(item.source_experience_ids)} 条经历"}




def _question_payload(item) -> dict[str, object]:
    return {"id": item.id, "revision": item.revision, "project_id": item.project_id,
            "question": item.question, "answer": item.content, "status": item.effective_state,
            "recorded_state": item.state, "evidence_eligible": item.evidence_eligible,
            "evidence_reason": item.evidence_reason, "updated_at": getattr(item, "updated_at", ""),
            "evidence_count": len(item.recognition_ids)}


def documents_for_project(records: SQLiteStructuredRecordStore, project_id: str | None) -> tuple[Mapping[str, object], ...]:
    """Read document projections from the same structured-store authority."""
    return tuple(
        dict(record.payload) for record in records.list("documents")
        if record.payload.get("project_id") == project_id
    )





def _public_settings(models: ModelConfiguration) -> dict[str, object]:
    return models.public()


def _put_new(records: SQLiteStructuredRecordStore, collection: str, object_id: str, payload: Mapping[str, object]) -> None:
    try:
        with records.begin() as uow:
            uow.put(collection, object_id, payload, expected_revision=0)
            uow.commit()
    except SQLiteUnitOfWorkConflict as exc:
        raise RecognitionConflict("record id conflicted") from exc


def _scope(value: object) -> WorkScope:
    project_id = _text(value if value is not None else "default", "project_id", maximum=128)
    return WorkScope("local-user", project_id)


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecognitionError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_json_number(value):
    raise RecognitionError("non-finite JSON number")


async def _candidate_body(request: Request):
    body = await _body(request)
    if "generation" in body:
        raise RecognitionError("candidate generation provenance is assigned by the server")
    return body


def _recognition_body_limit(request: Request) -> int:
    """Keep header and streamed-body limits aligned for editable documents."""
    parts = request.url.path.removeprefix(_PREFIX + "/").split("/")
    candidate_edit = (request.method == "PATCH" and parts[0] == "candidates"
                      and (len(parts) == 2 or len(parts) == 3 and parts[2] == "draft"))
    markdown_edit = (request.method == "POST" and len(parts) == 3
                     and parts[0] == "recognitions" and parts[2] == "markdown")
    return _MAX_RECOGNITION_EDIT_BYTES if candidate_edit or markdown_edit else _MAX_REQUEST_BYTES


async def _body(request: Request, *, maximum=None, strict_json=False):
    maximum = _recognition_body_limit(request) if maximum is None else maximum
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > maximum:
                raise RecognitionError("request_too_large")
        value = json.loads(raw, object_pairs_hook=_unique_json_pairs, parse_constant=_invalid_json_number) if strict_json else json.loads(raw)
    except RecognitionError:
        raise
    except Exception:
        raise RecognitionError("JSON request body is required") from None
    if not isinstance(value, Mapping):
        raise RecognitionError("JSON request body is invalid")
    return value


def _text(value: object, name: str, *, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RecognitionError(f"{name} is invalid")
    return value.strip()


def _string_list(value: object, name: str, *, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecognitionError(f"{name} is invalid")
    result = tuple(_text(item, name, maximum=128) for item in value)
    if len(result) < minimum or len(set(result)) != len(result):
        raise RecognitionError(f"{name} is invalid")
    return result


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RecognitionError("expected_revision is invalid")
    return value


def _is_local_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in _LOCAL_HOSTS and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment


def _token_count(items: Sequence[Mapping[str, object]]) -> int:
    # Conservative upper bound for multilingual text, not a tokenizer estimate.
    return sum(len(str(item.get("content", "")).encode("utf-8")) for item in items)


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


async def _recognition_error(_request: Request, error: RecognitionError) -> JSONResponse:
    return _error(409 if isinstance(error, RecognitionConflict) else 422, str(error))


async def _model_error(_request: Request, error: ModelConfigurationError) -> JSONResponse:
    return _error(409, str(error))


async def _server_secret_error(_request: Request, error: ServerSecretStoreError) -> JSONResponse:
    return _error(503, error.code)


async def _retrieval_error(_request: Request, error: RecognitionRetrievalError) -> JSONResponse:
    return _error(422, str(error))


async def _sqlite_conflict(_request: Request, _error_value: SQLiteUnitOfWorkConflict) -> JSONResponse:
    return _error(409, "record revision conflicted")


async def _document_error(_request: Request, _error_value: DocumentRepositoryError) -> JSONResponse:
    return _error(409, "document write conflicted or violated its contract")


async def _context_error(_request: Request, error: ContextSelectionError) -> JSONResponse:
    return _error(409, str(error))


import os as _os
if _os.environ.get('CHRIPTMAS_DEPLOY') == 'server':
    from backend.shared.lazy_application import LazyApplication
    # Parsing validates the root contract without initializing any user vault.
    _server_layout = resolve_deployment(Path(__file__).resolve().parents[3] / 'runtime')
    app = LazyApplication(lambda: create_server_app(layout=_server_layout))
else:
    app = create_app()
