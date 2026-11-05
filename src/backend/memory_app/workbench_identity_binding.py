from copy import deepcopy
from pathlib import Path

from backend.security.audited_records import AuditedRecordStore
from backend.security.user_context import USER_ACCESS
from core.ai_kernel.product_admission import ProductAdmissionItem, seal_admission


KIND = 'workbench-steer-binding-v1'


def execution_owner(application, records, documents):
    state = application.state
    namespace = documents.namespace_id
    if isinstance(records,AuditedRecordStore):
        access = USER_ACCESS.get()
        if (getattr(state,'server_user_id',None) != records.user_id
                or records.namespace != namespace):
            raise ValueError('workbench_identity_owner_changed')
        if access is None:
            return _background_owner(application, records, documents)
        if access.target_user_id != records.user_id:
            raise ValueError('workbench_identity_owner_changed')
        owner = records.user_id
    else:
        if USER_ACCESS.get() is not None or getattr(state,'server_context',None) is not None:
            raise ValueError('workbench_identity_owner_unavailable')
        owner = 'local-user'
    return {'owner_user_id':owner,'namespace_id':namespace,'origin_kind':'personal'}


def _background_owner(application, records, documents):
    from backend.shared.server_resources import RESOURCE_POOL
    from .server_runtime import _SERVER_JOB_SUBJECT
    state = application.state
    context, resources, job = getattr(state, 'server_context', None), RESOURCE_POOL.get(), _SERVER_JOB_SUBJECT.get()
    root = Path(state.recognition_runtime_root).resolve(strict=True)
    if (context is None or resources is not context.resources or job is None
            or job[0] is not context.pool or job[2] != records.user_id or job[3] != root
            or state.recognition_records is not records or state.recognition_documents is not documents):
        raise ValueError('workbench_identity_background_owner_unavailable')
    current_records, current_models = resources.server_model_wire_owner_for(root)
    if current_records is not records or current_models is not state.recognition_models:
        raise ValueError('workbench_identity_background_owner_changed')
    return {'owner_user_id': records.user_id, 'namespace_id': documents.namespace_id, 'origin_kind': 'personal'}


def capture_identity(application, records, documents, *, project, scene, thread_id, turn_id, parent_turn_id=None):
    owner = execution_owner(application,records,documents)
    namespace = owner['namespace_id']
    parent = None
    siblings = [turn_id]
    part = None
    planned = None
    if parent_turn_id is not None:
        row = records.read('v2_turns',parent_turn_id)
        if row is None or row.payload.get('intent') != 'multi':
            raise ValueError('workbench_identity_parent_unavailable')
        siblings = row.payload.get('part_turn_ids')
        if (not isinstance(siblings,list) or len(siblings) != len(set(siblings))
                or turn_id not in siblings):
            raise ValueError('workbench_identity_part_changed')
        part = siblings.index(turn_id)
        if parent_turn_id.startswith('turn-route-'):
            from .route_correction_scopes import planned_child_scope
            planned = planned_child_scope(records, parent_turn_id, turn_id)
            if (planned is None or planned['target'] != {'project_id': project, 'scene': scene,
                    'namespace': f"personal/{owner['owner_user_id']}/{namespace}", 'thread_id': thread_id}
                    or planned['part_index'] != part):
                raise ValueError('workbench_identity_planned_scope_changed')
        elif row.payload['project_id'] != project or row.payload['thread_id'] != thread_id:
            raise ValueError('workbench_identity_parent_scope_changed')
        parent = {'id':parent_turn_id,'thread_id':row.payload['thread_id'],
            'project_id':row.payload['project_id'],'revision':row.revision,
            'uri':f'crp://{namespace}/v2_turns/{parent_turn_id}','part_turn_ids':list(siblings)}
    return {**owner,
        'public_turn_id':parent_turn_id or turn_id,'target_turn_id':turn_id,
        'thread_id':thread_id,'project_id':project,'scene':scene,'part':part,
        'part_turn_ids':list(siblings),'parent':parent,
        **({'planned_scope': deepcopy(planned)} if planned is not None else {})}


def identity_item(request, identity, store):
    if request['scope']['project_id'] != identity['project_id']:
        raise ValueError('workbench_identity_project_changed')
    reference = store.immutable_payload_reference(request['turn_id'],KIND)
    ref = {'kind':'session_event','object_id':request['turn_id'],'uri':reference}
    scope = {key:identity[key] for key in ('project_id','owner_user_id','namespace_id','origin_kind')}
    scope['turn_id'] = request['turn_id']
    return ProductAdmissionItem(KIND,ref,scope,{**deepcopy(identity),'turn_id':request['turn_id']})


def prepare_admission(application, request, identity, store, *, admission_prepare=None):
    state = application.state
    owner = execution_owner(application,state.recognition_records,state.recognition_documents)
    expected = {**owner,'turn_id':request['turn_id'],'project_id':request['scope']['project_id']}
    if identity is not None and any(identity.get(key) != owner[key] for key in owner):
        raise ValueError('workbench_identity_owner_changed')
    items = (identity_item(request,identity,store),) if identity is not None else ()
    if admission_prepare is not None:
        if not callable(admission_prepare):
            raise ValueError('product_admission_adapter_invalid')
        callback_request,callback_identity = deepcopy(request),deepcopy(identity)
        additions = admission_prepare(callback_request,callback_identity)
        if callback_request != request or callback_identity != identity:
            raise ValueError('product_admission_adapter_changed_input')
        if not isinstance(additions,(tuple,list)) or any(not isinstance(item,ProductAdmissionItem) for item in additions):
            raise ValueError('product_admission_adapter_items_invalid')
        items = (*items,*additions)
    constraints = getattr(state, 'project_constraint_input', None)
    if constraints is not None:
        items = constraints.augment(request, store, items)
    if any(item.scope != expected for item in items):
        raise ValueError('product_admission_adapter_owner_changed')
    return seal_admission(request,items,store) if items else None
