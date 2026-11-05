import asyncio

from fastapi import HTTPException, Request
from backend.recognition import RecognitionConflict
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.connection_scope import create_scoped_task
from ..workspace_contracts import _json
from .route_corrections import RouteCorrections


def install_route_correction_routes(router, *, application, records, instance, execute, view=None, examples=None, classify_do=None, shared_execute=None, remember_binding_factory=None, shared_capture=None):
    from .route_example_origins import RouteExampleOrigins
    origins = examples.origin_for if examples is not None else RouteExampleOrigins(application)
    domain = RouteCorrections(records, instance=instance, origins=origins, view=view, examples=examples,
        shared_execute=shared_execute, remember_binding_factory=remember_binding_factory, shared_capture=shared_capture)
    tasks = application.state.workbench_tasks

    def failure(error):
        known = {'route_correction_shared_owner_unavailable', 'route_correction_remember_owner_unavailable',
            'route_correction_source_unfinished', 'route_correction_already_replaced', 'route_correction_revision_changed',
            'route_correction_operation_exists', 'route_original_birth_changed', 'record_lineage_invalid'}
        known.update({'route_correction_missing_child_requires_review', 'route_correction_missing_file_requires_review',
            'route_correction_missing_do_requires_review', 'route_correction_execution_closed'})
        detail = str(error) if str(error) in known else None
        if isinstance(error, (RecognitionConflict, SQLiteUnitOfWorkConflict)):
            return HTTPException(409, detail or 'route_correction_changed')
        return HTTPException(400, detail or 'route_correction_invalid')

    @router.get('/turns/{turn_id}/route-correction')
    async def read_correction(turn_id: str, project_id: str):
        try:
            return domain.read(turn_id, project_id)
        except (ValueError, RecognitionConflict) as error:
            raise failure(error) from None

    @router.get('/route-corrections/operations/{operation_id}')
    async def read_operation(operation_id: str):
        try:
            return domain.operation(operation_id)
        except ValueError as error:
            raise failure(error) from None

    @router.post('/route-corrections/operations/{operation_id}/cancel')
    async def cancel_operation(operation_id: str, request: Request):
        if await _json(request) != {}:
            raise HTTPException(400, 'route_correction_invalid')
        try:
            return domain.operation(operation_id, cancel=True)
        except (ValueError, SQLiteUnitOfWorkConflict) as error:
            raise failure(error) from None

    @router.post('/turns/{turn_id}/route-correction')
    async def correct(turn_id: str, request: Request):
        body = await _json(request)
        if not isinstance(body, dict) or set(body) != {'project_id', 'operation_id', 'expected_revision', 'source_revision', 'parts'}:
            raise HTTPException(400, 'route_correction_invalid')
        try:
            identity = domain.claim(turn_id, **body)
        except (ValueError, RecognitionConflict, SQLiteUnitOfWorkConflict) as error:
            raise failure(error) from None
        task = create_scoped_task(domain.execute(identity, execute, classify_do=classify_do))
        tasks.add(task)
        def finished(done):
            tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        # 受理与执行分开；断开HTTP不取消已持久的新轮，查询只读同一操作。
        await asyncio.sleep(0)
        return domain.operation(identity)

    @router.post('/turns/{turn_id}/route-example/delete')
    async def delete_example(turn_id: str, request: Request):
        body = await _json(request)
        if examples is None or not isinstance(body, dict) or set(body) != {'project_id', 'expected_revision', 'operation_id'}:
            raise HTTPException(400, 'route_correction_invalid')
        try:
            examples.remove(turn_id, **body)
        except (ValueError, RecognitionConflict, SQLiteUnitOfWorkConflict) as error:
            raise failure(error) from None
        return domain.operation(body['operation_id'])

    return domain
