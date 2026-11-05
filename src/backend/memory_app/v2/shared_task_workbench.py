from contextlib import contextmanager

from backend.recognition import RecognitionConflict
from backend.recognition.material_logical_scope import read_material_logical_scope
from fastapi import HTTPException

from ..transaction_records import TransactionRecords
from .material_origin_admission import personal_origin
from .material_origin_readers import qualified_record_read
from .shared_auxiliary import _qualified_commit
from .shared_rendered_preparation import _query_current


def redo_query(application, turn):
    from .shared_projects import SharedProjectError
    from .turn_logical_scope import TurnLogicalScope
    try:
        logical = TurnLogicalScope(application).read(turn)
        if logical == 'personal':
            return application.state.workspace_domains.query
        factory = getattr(application.state, 'shared_query_factory', None)
        if factory is None:
            raise HTTPException(409, 'shared_scope_unavailable')
        query = factory.incoming_query(application, turn.payload['project_id'], scope_kind=logical)
        _query_current(query)
        return query
    except SharedProjectError as error:
        raise HTTPException(error.status, str(error)) from None
    except RecognitionConflict as error:
        raise HTTPException(409, str(error)) from None


def require_redo_query(application, turn, *, source_kind='execution', expected_revision=None):
    query = redo_query(application, turn)
    if query.origin_admission is not None:
        from .shared_task_redo import capture_source
        try:
            source = capture_source(query, turn.object_id, source_kind=source_kind)
        except RecognitionConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            if str(error) != 'task_division_not_found':
                raise
            raise HTTPException(404, str(error)) from None
        if expected_revision is not None and source['revision'] != expected_revision:
            raise HTTPException(409, 'task_division_revision_conflicted')
    return query


def continuation_candidates(query, *, project, document_id=None):
    from .outcomes import candidates
    _query_current(query)
    admission = query.origin_admission
    if admission is None or project != admission.lease.project_id:
        raise RecognitionConflict('shared_task_scope_changed')
    with _qualified_commit(admission) as qualification:
        with qualified_record_read(query.receipt_records, validate=qualification._validate) as transaction:
            reader = TransactionRecords(transaction)
            qualification._actor_reader(reader)
            result = []
            for item in candidates(reader, project=project, document_id=document_id):
                scope = read_material_logical_scope(reader, material_type='document',
                    identity=item['document_id'], project_id=project, namespace_id=admission.destination.namespace)
                if (scope is not None and scope['scope_kind'] == 'shared'
                        and scope['execution'] == personal_origin(admission.destination)
                        and scope['origin']['payload']['shared']['namespace'] == query.source_namespace):
                    result.append(item)
            return result


def validate_public_task(query, execution, *, reader):
    from core.ai_kernel.product_admission import bundle_from_payload
    from .part_context import validate_bound_context
    from .shared_task_materials import read_prepared_task_material_item
    _query_current(query)
    request = execution['request']
    if request['scope']['project_id'] != query.origin_admission.lease.project_id:
        raise RecognitionConflict('shared_task_scope_changed')
    current = reader if isinstance(reader, TransactionRecords) else TransactionRecords(reader)
    with _qualified_commit(query.origin_admission) as qualification:
        qualification._actor_reader(current)
        read_prepared_task_material_item(query, request, bundle_from_payload(execution['admission_bundle']), reader=current)
        query.project_constraint_input.validate(request, reader=current)
        validate_bound_context(current, query.models, request, query)


@contextmanager
def publication_scope(application, research, public_id):
    from .shared_task_context import task_query
    records = research.records
    state = records.read('v2_task_executions', public_id)
    public = records.read('v2_turns', public_id)
    if (state is None or public is None or public.payload.get('intent') != 'do'
            or public.payload['project_id'] != state.payload['project_id']
            or public.payload['receipt']['do']['kernel_turn_id'] != state.payload['request']['turn_id']):
        raise RecognitionConflict('shared_task_publication_identity_changed')
    native = application.state.ai_turn_store.get_request(state.payload['request']['turn_id'])
    if native is None:
        # 运行中的准备轮尚未有完成样例；接纳后的样例沿原任务 Root 取得来源事务。
        if public.payload['receipt']['do']['state'] in {'done', 'partial'}:
            raise RecognitionConflict('shared_task_publication_native_missing')
        yield None
        return
    query, _ = task_query(research.query, state.payload['request'])
    if query.origin_admission is None:
        yield None
        return
    with _qualified_commit(query.origin_admission) as qualification:
        yield qualification
