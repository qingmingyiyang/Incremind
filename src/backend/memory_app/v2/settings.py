"""Safe settings read models and a privacy-only compare-and-swap boundary."""
from dataclasses import asdict
import asyncio
import logging
import json
import math
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.security.user_context import USER_ACCESS, UserError
from backend.security.device_identity import server_mode
from core.product_core.cloud_asr_provider_settings import GetCloudAsrProviderSettings, TOKENHUB_ASR_SECRET_REF
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from core.document_engine import SQLiteDocumentRepository
from ..source_egress import SourceEgressService
from ..workspace_contracts import _json, _project, _now
from ..model_config import ModelConfiguration, ModelConfigurationError
from .privacy import is_private_project, privacy_revision, set_private_project_in_transaction
from .external_agent_settings import ExternalAgentSettingsError, external_agent_settings, replace_external_agent_settings
from .provider_store_settings import ProviderStoreSettings
from .mcp_connection import connection_metadata
from .external_proxy_settings import ExternalProxySettingsError, external_proxy_settings, replace_external_proxy_settings
from .embedding_settings import EmbeddingSettings, EmbeddingSettingsError, vector_policy


def _privacy(reader):
    return {"revision": privacy_revision(reader), "private_projects": sorted(
        row.object_id for row in reader.list("v2_private_scopes")
        if is_private_project(reader, row.object_id))}


def _private_sources(records):
    authority = SourceEgressService(records)
    result = []
    from ..original_sources import all_originals
    from types import SimpleNamespace
    sources = [(kind,row) for kind,collection in (("experience","recognition_experiences"),("recognition","recognitions"))
               for row in records.list(collection)]
    sources.extend((kind,SimpleNamespace(object_id=identity,payload=body))
                   for kind,identity,body in all_originals(records)
                   if body.get("identity_method") != "workspace_confirmation")
    for kind,row in sources:
        raw_scope = row.payload.get("scope", {})
        if not isinstance(raw_scope, dict):
            continue
        scope = WorkScope(raw_scope.get("user_id", row.payload.get("user_id", "local-user")),
                          raw_scope.get("project_id", row.payload.get("project_id", "default")))
        try:
            with records.begin() as reader:
                nodes = {}
                effective = authority._effective(reader, scope, kind, row.object_id, nodes, (), None)
                if effective:
                    continue
                # Reuse the service's policy override to read the inherited
                # ceiling, including frozen artifact permissions. This does
                # not save a policy or duplicate provenance authorization.
                purposes = frozenset(("generation", "embedding", "rerank"))
                ceiling = authority._effective(reader, scope, kind, row.object_id, {}, (),
                                                (kind, row.object_id, purposes))
                inherited = ceiling != purposes
        except (RecognitionError, RecognitionConflict):
            # Broken historical provenance has no actionable policy projection.
            continue
        root = nodes[(kind, row.object_id)]
        title = row.payload.get("title")
        result.append({"source_id": row.object_id, "title": title if isinstance(title, str) and title else row.object_id,
            "type": kind, "project_id": scope.project_id, "policy_revision": root["policy_revision"],
            "source_revision": root["source_revision"], "inherited": inherited})
    return result


def _number(value):
    return value if type(value) in (int, float) and value >= 0 and math.isfinite(value) else None


def _receipt(at, purpose, target, items=None, usage=None, duration=None, model_cost=None):
    target = target if isinstance(target, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    incoming = _number(usage.get("input_tokens", usage.get("prompt_tokens")))
    outgoing = _number(usage.get("output_tokens", usage.get("completion_tokens")))
    return {"at": at if isinstance(at, str) else None, "purpose": purpose,
        "model": target.get("model") if isinstance(target.get("model"), str) else None,
        "items": items, "usage": {"input": incoming, "output": outgoing} if usage else None,
        "duration": _number(duration), 'model_cost': model_cost}


def _receipts(records, limit, *, runtime_root=None):
    result = []
    from .external_context import delivery_receipts
    if runtime_root is not None:
        result.extend(_receipt(row['at'], '外部 agent', {}, row['items'])
                      for row in delivery_receipts(records, runtime_root))
    for row in records.list('v2_egress_receipts'):
        payload = row.payload
        result.append(_receipt(payload.get('at'), '认识', {'model': payload.get('model')},
            payload.get('items'), payload.get('usage')))
    from ..kernel.receipt_projection import kernel_call_groups, PURPOSES, aggregate_usage, aggregate_cost
    from .task_egress import task_call_groups
    try:
        kernel = kernel_call_groups(runtime_root, records=records)
        groups = task_call_groups(records, runtime_root)
    except (OSError, ValueError, sqlite3.Error):
        raise HTTPException(503, 'task_receipts_unavailable') from None
    covered = set()
    ask_ids = set()
    organized = {}
    for group in kernel:
        if group['kind'] not in PURPOSES:
            continue
        calls = group['calls']
        covered.update((call['turn_id'], call['model_request_id']) for call in calls)
        saved = (group['answer'] or {}).get('receipt', {}).get('ask', {})
        if saved.get('egress_receipt_id'):
            ask_ids.add(saved['egress_receipt_id'])
        if group['kind'] == 'memory.organize':
            for material in group['request'].get('privacy', {}).get('material_refs', []):
                if material.get('type') == 'original_item':
                    organized.setdefault(material['id'], []).append(group['request'].get('created_at', ''))
        usage = aggregate_usage(calls)
        if usage and usage.get('observed_only'):
            usage = None
        models = sorted({call['model_id'] for call in calls if call.get('model_id')})
        result.append(_receipt(max(call['completed_at'] for call in calls), PURPOSES[group['kind']],
            {'model': ' · '.join(models) or None}, usage=usage,
            duration=sum(call['duration_ms'] for call in calls) / 1000, model_cost=aggregate_cost(calls)))
    for calls in groups:
        calls = [call for call in calls if (call['turn_id'], call['model_request_id']) not in covered]
        if not calls:
            continue
        usage = aggregate_usage(calls)
        if usage and usage.get('observed_only'):
            usage = None
        models = sorted({call['model_id'] for call in calls if call.get('model_id')})
        result.append(_receipt(max(call['completed_at'] for call in calls), '干活',
            {'model': ' · '.join(models) or None}, usage=usage,
            duration=sum(call['duration_ms'] for call in calls) / 1000, model_cost=aggregate_cost(calls)))
    for row in records.list("workspace_ask_receipts"):
        payload = row.payload
        if row.object_id in ask_ids:
            continue
        target = payload.get("target", {})
        if not isinstance(target, dict) or target.get("execution_location") != "remote":
            continue
        sources = payload.get("sources")
        result.append(_receipt(payload.get("created_at"), "问", target,
            len(sources) if isinstance(sources, list) else None, payload.get("usage")))
    for row in records.list("workspace_items"):
        receipts = row.payload.get("remote_processing_receipts", [])
        if not isinstance(receipts, list):
            continue
        for index, receipt in enumerate(receipts):
            if not isinstance(receipt, dict):
                continue
            for purpose, key in (("整理", "generation"), ("转写", "asr")):
                following = receipts[index + 1].get('consented_at', '') if index + 1 < len(receipts) and isinstance(receipts[index + 1], dict) else ''
                if key == 'generation' and any(receipt.get('consented_at', '') <= at and (not following or at < following)
                        for at in organized.get(row.object_id, [])):
                    continue
                if isinstance(receipt.get(key), dict):
                    result.append(_receipt(receipt.get("consented_at"), purpose, receipt[key], 1))
    return sorted(result, key=lambda row: row["at"] or "", reverse=True)[:limit]


class _IntegrityRecords(SQLiteStructuredRecordStore):
    """Reuse domain reads without schema initialization or journal changes."""

    def _connect(self):
        connection = sqlite3.connect(self.database_path.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        return connection


def _integrity(runtime_root, records, documents, workspace):
    root = Path(runtime_root).resolve()
    counts = dict(sqlite_corrupt=0, source_file_missing=0, source_without_document=0)
    for path in root.rglob('*'):
        if not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        with path.open('rb') as stream:
            sqlite_file = stream.read(16) == b'SQLite format 3\x00'
        if not sqlite_file and path.suffix.lower() not in {'.sqlite', '.sqlite3', '.db'}:
            continue
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    counts['sqlite_corrupt'] += 1
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            if getattr(exc, 'sqlite_errorcode', 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise HTTPException(503, 'integrity_unavailable') from None
            counts['sqlite_corrupt'] += 1

    reader = _IntegrityRecords(records.database_path)
    repository = SQLiteDocumentRepository(reader, namespace_id=documents.namespace_id)
    try:
        docs = repository.list(include_archived=True)
        items = reader.list('workspace_items')
        reviews = reader.list('workspace_review_intents')
    except sqlite3.DatabaseError:
        # A damaged authoritative database is already reported by quick_check;
        # its unreadable relationships cannot be inspected or repaired here.
        if not counts['sqlite_corrupt']:
            raise HTTPException(503, 'integrity_unavailable') from None
    else:
        source_store = workspace.query.source_store
        sources = {source['id'] for source in source_store.list('sources')}
        sources.update(row.object_id for row in items)
        linked = {ref['source_id'] for doc in docs for ref in doc.get('source_refs', [])
                  if ref.get('source_id')}
        linked.update(row.object_id for row in items if row.payload.get('draft'))
        linked.update(row.payload['source_id'] for row in reviews
                      if row.payload.get('source_id') and row.payload.get('draft_markdown'))
        paths = {row.object_id: {Path(row.payload['original_path'])} for row in items
                 if row.payload.get('original_path')}
        for ref in source_store.list('authorized_file_refs'):
            if ref.get('source_id') and ref.get('path'):
                paths.setdefault(ref['source_id'], set()).add(Path(ref['path']))
        assets = {asset['id']: asset for asset in source_store.list('workbench_original_assets')}
        originals_root = (root / 'library' / 'assets' / 'originals').resolve()
        for link in source_store.list('source_asset_links'):
            asset = assets.get(link.get('asset_id'), {})
            vault_ref = asset.get('vault_ref')
            if link.get('source_id') and isinstance(vault_ref, str):
                path = (root / 'library' / vault_ref).resolve()
                if path.is_relative_to(originals_root):
                    paths.setdefault(link['source_id'], set()).add(path)
        counts['source_file_missing'] = sum(
            any(not path.is_file() for path in paths.get(identity, ())) for identity in linked)
        counts['source_without_document'] = len(sources - linked)
    problems = [{'code': code, 'count': count} for code, count in counts.items() if count]
    return {'ok': not problems, 'checked_at': _now(), 'problems': problems}


def install_settings_routes(application, *, runtime_root, records, models, documents=None, workspace=None):
    router = APIRouter(prefix="/api/v2/settings")
    provider_store = ProviderStoreSettings(records, models)
    embeddings = getattr(application.state, 'memory_embedding_settings', None)
    if embeddings is None:
        embeddings = EmbeddingSettings(records, getattr(models, '_local_models_root', runtime_root / 'data' / 'models'))
        application.state.memory_embedding_settings = embeddings
    if isinstance(models, ModelConfiguration):
        models.bind_embedding(embeddings.project, vector_policy)
    index_tasks = set()

    async def index_local_vectors():
        jobs = getattr(application.state, 'memory_daily_jobs', None)
        index = getattr(application.state, 'memory_embedding_index', None)
        if jobs is not None and index is not None:
            try:
                # 仍由原调度器承担服务器队列与用户配额，不另建后台队列。
                await jobs._run_callback(index.run)
            except Exception as error:
                logging.getLogger(__name__).warning('local_vector_index_failed exception_type=%s', type(error).__name__)

    def start_vector_index():
        task = asyncio.create_task(index_local_vectors())
        index_tasks.add(task)
        task.add_done_callback(index_tasks.discard)

    @application.on_event('shutdown')
    async def close_vector_install():
        await run_in_threadpool(embeddings.close)
        # 安装线程刚投递的索引任务先进入本应用的生命周期，再等待终态。
        await asyncio.sleep(0)
        if index_tasks:
            await asyncio.gather(*tuple(index_tasks))

    def authorize_vector_install(request):
        if not server_mode(request):
            return
        access = USER_ACCESS.get()
        users = getattr(application.state, 'server_users', None)
        if access is None or users is None:
            raise HTTPException(403, 'embedding_install_forbidden')
        try:
            users.authorize_claim(access.caller, access.target_user_id, action='manage_resources')
        except UserError:
            raise HTTPException(403, 'embedding_install_forbidden') from None

    def can_install_vectors(request):
        try:
            authorize_vector_install(request)
        except HTTPException:
            return False
        return True

    @router.patch('/embedding-mode')
    async def settings_embedding_mode(request: Request):
        body = await _json(request)
        if set(body) != {'mode', 'expected_revision'}:
            raise HTTPException(400, 'embedding_mode_invalid')
        try:
            embeddings.update_mode(**body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'embedding_mode_revision_conflict') from None
        except EmbeddingSettingsError as error:
            raise HTTPException(400, str(error)) from None
        if body['mode'] == 'local':
            start_vector_index()
        return models.public()['embedding']

    @router.post('/embedding/install')
    async def settings_embedding_install(request: Request):
        authorize_vector_install(request)
        body = await _json(request)
        if set(body) != {'expected_revision'}:
            raise HTTPException(400, 'embedding_install_invalid')
        loop = asyncio.get_running_loop()
        try:
            result = embeddings.install(models.public()['embedding'], **body,
                on_ready=lambda: loop.call_soon_threadsafe(start_vector_index))
        except EmbeddingSettingsError as error:
            raise HTTPException(409 if str(error) == 'embedding_mode_revision_conflict' else 400, str(error)) from None
        return JSONResponse(status_code=202, content=result)
    # Reuse the installed legacy safe projection, without importing backend.api.
    asr_endpoint = next((route.endpoint for route in application.routes
        if getattr(route, "path", None) == "/api/rebuild/settings/cloud-asr-provider"
        and "GET" in getattr(route, "methods", set())), None)

    @router.get("")
    async def settings_get(request: Request):
        if asr_endpoint is not None:
            response = await asr_endpoint(application.state.container)
            asr = json.loads(response.body)
        else:
            # A minimal injected legacy app still reads the same core settings.
            store = JsonObjectStore(runtime_root / ".rebuild-data", namespace_id="default")
            asr = asdict(GetCloudAsrProviderSettings(store).execute())
            asr.update(has_api_key=models.secrets.has_secret(TOKENHUB_ASR_SECRET_REF),
                       chunk_duration_seconds=60, chunk_overlap_seconds=5)
        public = models.public()
        public['embedding'] = {**public['embedding'], 'local': {
            **public['embedding'].get('local', {}), 'can_install': can_install_vectors(request)}}
        for purpose in ('generation', 'embedding', 'rerank', 'vision'):
            public[purpose] = {**public[purpose], 'pricing': models.model_prices(purpose)}
        if 'search' in public:
            public['search'] = {**public['search'], 'pricing':models.model_prices('search')}
        public['generation']['fast_model'] = models.fast_model()
        try:
            external = external_agent_settings(records)
        except ExternalAgentSettingsError:
            raise HTTPException(503, 'external_agent_settings_invalid') from None
        try:
            proxy = external_proxy_settings(records)
        except ExternalProxySettingsError:
            raise HTTPException(503, 'external_proxy_settings_invalid') from None
        return {"model": {**public, "asr": asr}, "privacy": _privacy(records), "external_agent": external,
            'external_proxy':proxy}

    @router.patch('/external-proxy')
    async def settings_external_proxy(request: Request):
        body = await _json(request)
        if set(body) != {'record_conversations','expected_revision'}:
            raise HTTPException(400, 'external_proxy_settings_invalid')
        revision = body.pop('expected_revision')
        try:
            return replace_external_proxy_settings(records, body, expected_revision=revision)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'external_proxy_revision_conflict') from None
        except ExternalProxySettingsError:
            raise HTTPException(400, 'external_proxy_settings_invalid') from None

    @router.get('/external-agent/connection')
    async def settings_external_connection(request: Request, project_id: str = Query('default')):
        return connection_metadata(request, project_id)

    @router.post('/external-agent/snapshot')
    async def settings_external_snapshot(request: Request):
        from backend.security.device_identity import server_mode
        from starlette.concurrency import run_in_threadpool
        from .external_snapshot import generate_snapshot
        from .external_agent_guard import ExternalAgentGuardError
        from .external_context import ExternalContextError
        body = await _json(request)
        if (server_mode(request) or set(body) != {'client', 'project_id', 'budget'}
                or type(body['client']) is not str or body['client'] not in {'claude', 'codex'}
                or type(body['budget']) is not int or not 1 <= body['budget'] <= 12000):
            raise HTTPException(400, 'external_snapshot_invalid')
        project = _project(body['project_id'])
        try:
            return await run_in_threadpool(generate_snapshot, application, workspace,
                client=body['client'], project_id=project, budget=body['budget'])
        except (ExternalAgentGuardError, ExternalContextError) as error:
            raise HTTPException(409, str(error)) from None

    @router.patch('/external-agent')
    async def settings_external_agent(request: Request):
        body = await _json(request)
        if set(body) != {'allow_remote', 'include_profile', 'daily_limit', 'clients', 'expected_revision'}:
            raise HTTPException(400, 'external_agent_settings_invalid')
        revision = body.pop('expected_revision')
        try:
            return replace_external_agent_settings(records, body, expected_revision=revision)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'external_agent_revision_conflict') from None
        except ExternalAgentSettingsError:
            raise HTTPException(400, 'external_agent_settings_invalid') from None

    @router.get('/provider-store')
    async def settings_provider_store_get():
        return provider_store.get()

    @router.put('/provider-store')
    async def settings_provider_store_put(request: Request):
        body = await _json(request)
        if set(body) != {'enabled', 'expected_revision', 'expected_generation_revision', 'expected_mode_revision'}:
            raise HTTPException(400, 'provider_store_invalid')
        try:
            return provider_store.update(**body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'provider_store_revision_conflict') from None
        except ModelConfigurationError as error:
            code = str(error)
            raise HTTPException(409 if code == 'provider_store_configuration_changed' else 400, code) from None

    @router.patch('/fast-model')
    async def settings_fast_model(request: Request):
        body = await _json(request)
        if set(body) != {'model', 'expected_revision', 'expected_generation_revision', 'expected_mode_revision'}:
            raise HTTPException(400, 'fast_model_invalid')
        try:
            return models.update_fast_model(**body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'fast_model_revision_conflict') from None
        except ModelConfigurationError as error:
            code = str(error)
            raise HTTPException(409 if code == 'fast_model_configuration_changed' else 400, code) from None

    @router.patch('/vision-mode')
    async def settings_vision_mode(request: Request):
        body = await _json(request)
        if set(body) != {'mode', 'expected_revision'}:
            raise HTTPException(400, 'vision_mode_invalid')
        try:
            return models.update_vision_mode(**body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'vision_mode_revision_conflict') from None
        except ModelConfigurationError as error:
            raise HTTPException(400, str(error)) from None

    @router.patch('/model-prices')
    async def settings_model_prices(request: Request):
        body = await _json(request)
        if set(body) != {'purpose', 'rates', 'expected_revision', 'expected_configuration_revision'}:
            raise HTTPException(400, 'model_price_invalid')
        try:
            return models.update_model_prices(body['purpose'], body['rates'],
                expected_revision=body['expected_revision'],
                expected_configuration_revision=body['expected_configuration_revision'])
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'model_price_revision_conflict') from None
        except ModelConfigurationError as error:
            code = str(error)
            raise HTTPException(409 if code == 'model_price_configuration_changed' else 400, code) from None

    @router.patch("/privacy")
    async def settings_privacy(request: Request):
        body = await _json(request)
        if set(body) != {"private_projects", "expected_revision"}:
            raise HTTPException(400, "invalid_privacy_fields")
        wanted, revision = body["private_projects"], body["expected_revision"]
        if (not isinstance(wanted, list) or type(revision) is not int or revision < 0
                or any(not isinstance(value, str) for value in wanted)):
            raise HTTPException(400, "invalid_privacy")
        wanted = {_project(value) for value in wanted}
        with records.begin() as tx:
            current = _privacy(tx)
            if current["revision"] != revision:
                return JSONResponse(status_code=409, content={"detail": "privacy_revision_conflict", "current": current})
            if any(tx.read("v2_projects", value) is None for value in wanted):
                raise HTTPException(400, "project_not_found")
            for project_id in sorted(set(current["private_projects"]) | wanted):
                row = tx.read("v2_private_scopes", project_id)
                set_private_project_in_transaction(tx, project_id, project_id in wanted, row.revision if row else 0)
            result = _privacy(tx)
            tx.commit()
        return result

    @router.get("/private-sources")
    def private_sources_get():
        return _private_sources(records)

    @router.get('/integrity')
    def integrity_get():
        if documents is None or workspace is None:
            raise HTTPException(503, 'integrity_unavailable')
        try:
            return _integrity(runtime_root, records, documents, workspace)
        except OSError:
            raise HTTPException(503, 'integrity_unavailable') from None

    @router.get("/egress-receipts")
    def receipts_get(limit: int = Query(50, ge=1, le=500)):
        return _receipts(records, limit, runtime_root=runtime_root)

    application.include_router(router)
