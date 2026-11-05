from copy import deepcopy
from dataclasses import dataclass
from .contracts import _ERROR_CODE, validate_turn_request


MANIFEST_KIND = 'product-admission-manifest-v1'
ADMISSION_KINDS = frozenset({'workbench-steer-binding-v1','project-constraints-v1','material-origin-v1',
    'shared-task-materials-v1', 'shared-task-redo-v1'})


def _validate_shared_redo(request, item):
    if item.kind != 'shared-task-redo-v1':
        return
    value = item.payload
    fields = {'schema_version', 'turn_id', 'execution', 'origin_ref', 'source'}
    source_fields = {'public_id', 'source_kind', 'revision', 'items', 'task_text', 'scene', 'guard',
        'native_request', 'native_identity', 'origin_ref', 'material_proof_ref', 'materials', 'proof_refs', 'source_proofs'}
    source = value.get('source') if isinstance(value, dict) else None
    if (request['desired_outcome'] != 'project.task' or not isinstance(value, dict) or set(value) != fields
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['turn_id'] != request['turn_id'] or not isinstance(value['execution'], dict)
            or not isinstance(value['origin_ref'], dict) or not isinstance(source, dict)
            or set(source) not in (source_fields, source_fields | {'failed_execution'})
            or source['source_kind'] not in {'sample', 'execution'} or type(source['revision']) is not int
            or source['revision'] < 1 or not isinstance(source['public_id'], str) or not source['public_id']
            or not isinstance(source['task_text'], str) or not source['task_text']
            or source['scene'] is not None and not isinstance(source['scene'], str)
            or source['items'] is not None and not isinstance(source['items'], list)
            or not isinstance(source['guard'], dict)
            or set(source['guard']) != {'turn_id', 'turn_revision', 'execution_revision', 'sample_revision'}
            or source['guard']['turn_id'] != source['public_id']
            or any(type(source['guard'][key]) is not int or source['guard'][key] < 1
                for key in ('turn_revision', 'execution_revision'))
            or source['guard']['sample_revision'] is not None and (
                type(source['guard']['sample_revision']) is not int or source['guard']['sample_revision'] < 1)
            or not isinstance(source['material_proof_ref'], str) or not source['material_proof_ref']
            or not isinstance(source['origin_ref'], dict) or not isinstance(source['materials'], list)
            or not isinstance(source['source_proofs'], list) or not isinstance(source['proof_refs'], list)):
        raise ValueError('invalid_shared_task_redo_admission')
    if 'failed_execution' in source:
        failure = source['failed_execution']
        if (not isinstance(failure, dict) or set(failure) != {'terminal_sequence', 'error_code'}
                or type(failure['terminal_sequence']) is not int or failure['terminal_sequence'] < 2
                or not isinstance(failure['error_code'], str) or not _ERROR_CODE.fullmatch(failure['error_code'])):
            raise ValueError('invalid_shared_task_redo_admission')
    elif not source['proof_refs']:
        raise ValueError('invalid_shared_task_redo_admission')
    original = validate_turn_request(source['native_request'])
    if (original['desired_outcome'] != 'project.task' or original['turn_id'] == request['turn_id']
            or original['scope'] != request['scope'] or source['native_identity'] != admission_identity(original)):
        raise ValueError('invalid_shared_task_redo_admission')
    if (any(not isinstance(proof, dict) or set(proof) != {'basis_ref', 'turn_id', 'input_ref'}
            or any(not isinstance(proof[key], str) or not proof[key] for key in proof)
            for proof in source['proof_refs']) or any(not isinstance(proof, dict) for proof in source['source_proofs'])):
        raise ValueError('invalid_shared_task_redo_admission')
    for material in source['materials']:
        if (not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                or type(material['revision']) is not int or material['revision'] < 1
                or any(not isinstance(material[key], str) or not material[key]
                    for key in ('type', 'id', 'project_id', 'namespace'))):
            raise ValueError('invalid_shared_task_redo_admission')


@dataclass(frozen=True)
class ProductAdmissionItem:
    kind: str
    ref: dict
    scope: dict
    payload: dict


@dataclass(frozen=True)
class ProductAdmissionBundle:
    manifest: dict
    items: tuple[ProductAdmissionItem, ...]


def admission_identity(request):
    return deepcopy({key:request[key] for key in
        ('turn_id','session_id','operation_id','idempotency_key','scope','input','privacy')})


def seal_admission(request, items, store):
    validate_turn_request(request)
    if not items or len({item.kind for item in items}) != len(items):
        raise ValueError('invalid_product_admission_items')
    identities = []
    refs = deepcopy(request['input']['refs'])
    for item in items:
        if (item.kind not in ADMISSION_KINDS or set(item.scope) != {'turn_id','project_id','owner_user_id','namespace_id','origin_kind'}
                or item.scope['turn_id'] != request['turn_id'] or item.scope['project_id'] != request['scope']['project_id']
                or item.scope['origin_kind'] != 'personal' or not item.scope['owner_user_id'] or not item.scope['namespace_id']
                or set(item.ref) != {'kind','object_id','uri'} or item.ref['kind'] != 'session_event'
                or item.ref['object_id'] != request['turn_id'] or not isinstance(item.payload,dict)):
            raise ValueError('invalid_product_admission_scope')
        _validate_shared_redo(request, item)
        if any(ref.get('uri') == item.ref['uri'] for ref in refs):
            raise ValueError('duplicate_product_admission_reference')
        refs.append(deepcopy(item.ref))
        identities.append({'kind':item.kind,'ref':deepcopy(item.ref),'scope':deepcopy(item.scope)})
    reference = store.immutable_payload_reference(request['turn_id'],MANIFEST_KIND)
    if any(ref.get('uri') == reference for ref in refs):
        raise ValueError('product_admission_already_sealed')
    refs.append({'kind':'session_event','object_id':request['turn_id'],'uri':reference})
    request['input']['refs'] = refs
    validate_turn_request(request)
    manifest = {'request_identity':admission_identity(request),'items':identities}
    return ProductAdmissionBundle(manifest,tuple(deepcopy(item) for item in items))


def validate_manifest(request, manifest, *, persisted=False):
    if (not isinstance(manifest,dict) or set(manifest) != {'request_identity','items'}
            or manifest['request_identity'] != admission_identity(request)
            or not isinstance(manifest['items'],list) or not manifest['items']):
        raise ValueError('product_admission_request_changed')
    logical_manifest = {'kind':'session_event','object_id':request['turn_id'],
        'uri':f"crp://session/{request['turn_id']}/{MANIFEST_KIND}"}
    if request['input']['refs'].count(logical_manifest) != 1:
        raise ValueError('invalid_product_admission_manifest_reference')
    kinds,uris = set(),set()
    for item in manifest['items']:
        if (not isinstance(item,dict) or set(item) != ({'kind','ref','scope','payload_ref'} if persisted else {'kind','ref','scope'})
                or item['kind'] not in ADMISSION_KINDS or item['kind'] in kinds
                or not isinstance(item['ref'],dict) or set(item['ref']) != {'kind','object_id','uri'}
                or item['ref'].get('kind') != 'session_event' or item['ref'].get('object_id') != request['turn_id']
                or item['ref'].get('uri') in uris or request['input']['refs'].count(item['ref']) != 1
                or not isinstance(item['ref'].get('uri'),str) or not item['ref']['uri']
                or set(item['scope']) != {'turn_id','project_id','owner_user_id','namespace_id','origin_kind'}
                or item['scope']['turn_id'] != request['turn_id']
                or item['scope']['project_id'] != request['scope']['project_id']
                or item['scope']['origin_kind'] != 'personal'
                or any(not isinstance(item['scope'][key],str) or not item['scope'][key] for key in ('owner_user_id','namespace_id'))
                or persisted and (not isinstance(item['payload_ref'],str) or not item['payload_ref'])):
            raise ValueError('invalid_product_admission_manifest')
        kinds.add(item['kind'])
        uris.add(item['ref']['uri'])


def validate_bundle(request, bundle):
    if not isinstance(bundle,ProductAdmissionBundle):
        raise ValueError('invalid_product_admission_bundle')
    validate_manifest(request,bundle.manifest)
    if len(bundle.items) != len(bundle.manifest['items']):
        raise ValueError('product_admission_items_changed')
    for item,declared in zip(bundle.items,bundle.manifest['items']):
        if (not isinstance(item,ProductAdmissionItem) or item.kind != declared['kind']
                or item.ref != declared['ref'] or item.scope != declared['scope'] or not isinstance(item.payload,dict)):
            raise ValueError('product_admission_items_changed')
        _validate_shared_redo(request, item)
    kinds = {item.kind for item in bundle.items}
    if 'shared-task-redo-v1' in kinds and not {'workbench-steer-binding-v1', 'project-constraints-v1',
            'material-origin-v1', 'shared-task-materials-v1'}.issubset(kinds):
        raise ValueError('shared_task_redo_admission_dependencies_missing')


def bundle_to_payload(bundle):
    return {'manifest':deepcopy(bundle.manifest),'items':[{'kind':item.kind,'ref':deepcopy(item.ref),
        'scope':deepcopy(item.scope),'payload':deepcopy(item.payload)} for item in bundle.items]}


def bundle_from_payload(value):
    if (not isinstance(value,dict) or set(value) != {'manifest','items'} or not isinstance(value['items'],list)
            or any(not isinstance(item,dict) or set(item) != {'kind','ref','scope','payload'} for item in value['items'])):
        raise ValueError('invalid_product_admission_payload')
    return ProductAdmissionBundle(deepcopy(value['manifest']),tuple(ProductAdmissionItem(**deepcopy(item)) for item in value['items']))
