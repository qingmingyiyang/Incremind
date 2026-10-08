"""Local HTTP orchestration for MCP using the existing domain and Kernel owners."""
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..workspace_contracts import _json, _now, _project, _text
from .external_agent_guard import ExternalAgentGuardError
from .external_context import ExternalContextError
from .external_catalog import ExternalCatalog
from .external_recall import ExternalRecall


def _arguments(body):
    if (not isinstance(body, dict) or set(body) != {'client', 'arguments'}
            or not isinstance(body['client'], str) or body['client'] not in {'claude', 'codex'}
            or not isinstance(body['arguments'], dict)):
        raise HTTPException(400, 'external_agent_request_invalid')
    return body['client'], body['arguments']


def _read_selections(proof, window):
    if window is not None and (not isinstance(window, dict)
            or set(window) != {'start', 'end'}
            or any(type(window[key]) is not int for key in window)
            or not 0 <= window['start'] < window['end']):
        raise HTTPException(400, 'external_agent_window_invalid')
    project = proof['material']['project_id']
    roots = [node for node in proof['snapshot']['nodes']
             if node['type'] in {'original_item', 'original_source'}]
    if not roots:
        raise ExternalContextError('external_context_material_changed')
    # The original frozen source graph supplies every identity and revision.
    return [{'type': node['type'], 'id': node['id'], 'revision': node['source_revision'],
             'project_id': project, 'layer': 'L0', 'windows': [dict(window)] if window else []}
            for node in roots]


def install_mcp_memory_routes(application, *, workspace):
    router = APIRouter(prefix='/api/v2/external-agent/mcp')
    application.state.external_context.bind_catalog(ExternalCatalog(workspace.query,
        owner_id=application.state.external_context.owner_id))
    application.state.external_context.bind_recall(ExternalRecall(workspace.query,
        owner_id=application.state.external_context.owner_id))

    def recall_owned(client, arguments, tool):
        field = 'query' if tool == 'recall' else 'situation'
        allowed = {field, 'project', 'scene'} | ({'budget'} if tool == 'recall' else set())
        if field not in arguments or set(arguments).difference(allowed):
            raise HTTPException(400, 'external_agent_request_invalid')
        text = _text(arguments[field], field)
        project = _project(arguments.get('project', 'default'))
        scene = _text(arguments['scene'], 'scene') if 'scene' in arguments else None
        budget = arguments.get('budget', 3000)
        if type(budget) is not int or not 1 <= budget <= 12000:
            raise HTTPException(400, 'external_agent_request_invalid')
        try:
            runtime = workspace.query.answer_turns._runtime()
            api = application.state.external_context
            turn_id = 'turn-' + uuid4().hex
            request = {'client': client, 'tool': tool, 'query': text, 'budget': budget,
                'scope': {'user_id': api.owner_id, 'project_id': project}}
            api.prepare_recall(turn_id, request, scene=scene, session_id='session-mcp-' + uuid4().hex,
                operation_id='mcp-' + turn_id, idempotency_key=turn_id, created_at=_now())
            return {'turn_id': turn_id, 'result': api.execute(turn_id, runtime=runtime,
                runner=application.state.ai_turn_runner)}
        except (ExternalContextError, ExternalAgentGuardError) as error:
            raise HTTPException(409, str(error)) from None

    def projects_owned(client, arguments):
        if arguments:
            raise HTTPException(400, 'external_agent_request_invalid')
        try:
            runtime = workspace.query.answer_turns._runtime()
            api = application.state.external_context
            turn_id = 'turn-' + uuid4().hex
            call = {'client': client, 'tool': 'projects', 'query': '', 'budget': 3000,
                'scope': {'user_id': api.owner_id, 'project_id': 'default'}}
            api.prepare_projects(turn_id, call, session_id='session-mcp-' + uuid4().hex,
                operation_id='mcp-' + turn_id, idempotency_key=turn_id, created_at=_now())
            return {'turn_id': turn_id, 'result': api.execute(turn_id, runtime=runtime,
                runner=application.state.ai_turn_runner)}
        except (ExternalContextError, ExternalAgentGuardError) as error:
            raise HTTPException(409, str(error)) from None

    def report_use_owned(client, arguments):
        if (set(arguments) != {'turn_id', 'ids'} or not isinstance(arguments['turn_id'], str)
                or not isinstance(arguments['ids'], list)):
            raise HTTPException(400, 'external_agent_request_invalid')
        try:
            api = application.state.external_context
            result = api.report_use(arguments['turn_id'], arguments['ids'], client=client)
            return {'turn_id': arguments['turn_id'], 'result': result}
        except (ExternalContextError, ExternalAgentGuardError) as error:
            raise HTTPException(409, str(error)) from None

    def read_owned(client, arguments):
        identity = arguments.get('id')
        if (set(arguments).difference({'id', 'window'}) or not isinstance(identity, dict)
                or set(identity) != {'turn_id', 'id'}
                or any(not isinstance(identity[key], str) for key in identity)):
            raise HTTPException(400, 'external_agent_request_invalid')
        if 'window' in arguments and arguments['window'] is None:
            raise HTTPException(400, 'external_agent_window_invalid')
        try:
            runtime = workspace.query.answer_turns._runtime()
            api = application.state.external_context
            proof, original = api.delivered_proof(identity['turn_id'], identity['id'], client=client)
            selections = _read_selections(proof, arguments.get('window'))
            turn_id = 'turn-' + uuid4().hex
            call = {**original, 'tool': 'read'}
            api.prepare(turn_id, call, selections, session_id='session-mcp-' + uuid4().hex,
                operation_id='mcp-' + turn_id, idempotency_key=turn_id, created_at=_now(), origin=identity)
            result = api.execute(turn_id, runtime=runtime, runner=application.state.ai_turn_runner)
            return {'turn_id': turn_id, 'result': result}
        except (ExternalContextError, ExternalAgentGuardError) as error:
            raise HTTPException(409, str(error)) from None

    @router.post('/report_use')
    async def report_use(request: Request):
        client, arguments = _arguments(await _json(request))
        return await run_in_threadpool(report_use_owned, client, arguments)

    @router.post('/projects')
    async def projects(request: Request):
        client, arguments = _arguments(await _json(request))
        return await run_in_threadpool(projects_owned, client, arguments)

    @router.post('/recall')
    async def recall(request: Request):
        client, arguments = _arguments(await _json(request))
        return await run_in_threadpool(recall_owned, client, arguments, 'recall')

    @router.post('/methods')
    async def methods(request: Request):
        client, arguments = _arguments(await _json(request))
        return await run_in_threadpool(recall_owned, client, arguments, 'methods')

    @router.post('/read')
    async def read(request: Request):
        client, arguments = _arguments(await _json(request))
        return await run_in_threadpool(read_owned, client, arguments)

    application.include_router(router)
