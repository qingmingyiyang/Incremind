from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from core.storage_provider.record_lineage import HEAD_COLLECTIONS, capture_lineage, verify_lineage, RecordLineageError
from backend.security.user_context import USER_ACCESS, UserError, authorize_user
from ..server_audit import AUDIT, AUDIT_REQUEST
from ..transaction_records import TransactionRecords


PROOFS = 'v2_audit_object_proofs'


class Birth(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    collection: Literal['documents', 'v2_projects', 'recognitions']
    object_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
    fact_id: StrictStr = Field(pattern=r'^[0-9a-f]{32}$')


class ObjectReference(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    type: Literal['document', 'project', 'insight']
    namespace: StrictStr = Field(min_length=1, max_length=128)
    project_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
    object_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
    revision: StrictInt = Field(gt=0)
    lineage: Birth

    @model_validator(mode='after')
    def qualified_kind(self):
        if (self.lineage.collection != {'document': 'documents', 'project': 'v2_projects', 'insight': 'recognitions'}[self.type]
                or self.lineage.object_id != self.object_id or self.type == 'project' and self.project_id != self.object_id):
            raise ValueError('admin_audit_reference_invalid')
        return self


class PreferenceEffect(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    collection: Literal['recognition_recall_preferences']
    object_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
    revision: StrictInt = Field(gt=0)


class DomainProof(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    intent_id: StrictStr
    actor_user_id: StrictStr
    actor_device_id: StrictStr
    target_user_id: StrictStr
    action: Literal['document.edit', 'document.restore', 'project.create', 'project.update', 'insight.forget', 'insight.restore-recall']
    business_state: Literal['committed']
    objects: list[ObjectReference] = Field(min_length=1, max_length=1)
    effect: PreferenceEffect | None = None

    @model_validator(mode='after')
    def qualified_effect(self):
        ref = self.objects[0]
        if (ref.type != self.action.split('.')[0]
                or (self.effect is not None) != (ref.type == 'insight')
                or self.effect is not None and self.effect.object_id != ref.object_id):
            raise ValueError('admin_audit_effect_invalid')
        return self


def _same_db(reader, records):
    if Path(TransactionRecords(reader).database_path).resolve() != Path(records.database_path).resolve():
        raise ValueError('admin_audit_binding_invalid')


def _reference(reader, records, documents, project_id, document_id, revision, *, writing):
    from core.document_engine import SQLiteDocumentRepository
    _same_db(reader, records)
    current = SQLiteDocumentRepository(TransactionRecords(reader), namespace_id=documents.namespace_id).read(document_id)
    if current is None or current.get('project_id') != project_id or current.get('revision') != revision:
        raise ValueError('admin_audit_object_changed')
    if writing:
        lineage = capture_lineage(reader, 'documents', document_id)
    else:
        head = reader.read(HEAD_COLLECTIONS['documents'], document_id)
        if head is None:
            return None
        lineage = {'collection': 'documents', 'object_id': document_id, 'fact_id': head.payload.get('fact_id')}
        verify_lineage(reader, lineage)
    return ObjectReference(type='document', namespace=documents.namespace_id, project_id=project_id,
        object_id=document_id, revision=revision, lineage=Birth.model_validate(lineage)).model_dump()


def record_document_write(tx, *, records, documents, project_id, document_id, revision, restoring):
    binding = AUDIT_REQUEST.get()
    if binding is None:
        return
    binding.validate(records, method='PATCH', path='/api/v2/library/documents/{document_id}')
    if binding.application.state.recognition_documents is not documents:
        raise ValueError('admin_audit_binding_invalid')
    ref = _reference(tx, records, documents, project_id, document_id, revision, writing=True)
    _save_proof(tx, binding, 'document.restore' if restoring else 'document.edit', ref)


def _save_proof(tx, binding, action, ref, *, effect=None):
    access = binding.access
    proof = DomainProof(schema_version=1, intent_id=binding.intent,
        actor_user_id=access.caller.user_id, actor_device_id=access.caller.device_id,
        target_user_id=access.target_user_id, action=action,
        business_state='committed', objects=[ObjectReference.model_validate(ref)], effect=effect)
    tx.put(PROOFS, binding.intent, proof.model_dump(exclude_none=True), expected_revision=0)


def record_project_write(tx, *, records, project_id, revision, creating):
    binding = AUDIT_REQUEST.get()
    if binding is None:
        return
    binding.validate(records, method='POST' if creating else 'PATCH',
        path='/api/v2/projects' if creating else '/api/v2/projects/{project_id}')
    _same_db(tx, records)
    namespace = getattr(records, 'namespace', None)
    current = tx.read('v2_projects', project_id)
    if not isinstance(namespace, str) or not namespace or current is None or current.revision != revision:
        raise ValueError('admin_audit_object_changed')
    ref = ObjectReference(type='project', namespace=namespace, project_id=project_id, object_id=project_id,
        revision=revision, lineage=Birth.model_validate(capture_lineage(tx, 'v2_projects', project_id)))
    _save_proof(tx, binding, 'project.create' if creating else 'project.update', ref.model_dump())


def record_insight_preference(tx, *, records, scope, recognition, preference):
    binding = AUDIT_REQUEST.get()
    if binding is None:
        return
    binding.validate(records, method='POST', path='/api/v2/library/insights/{insight_id}/forget')
    _same_db(tx, records)
    namespace = getattr(records, 'namespace', None)
    current = tx.read('recognitions', recognition.object_id)
    effect = tx.read('recognition_recall_preferences', recognition.object_id)
    if (not isinstance(namespace, str) or not namespace or current != recognition or effect != preference
            or current.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
            or current.payload.get('state') != 'active' or effect.payload.get('user_id') != scope.user_id
            or effect.payload.get('project_id') != scope.project_id
            or effect.payload.get('state') not in {'forgotten', 'normal'}):
        raise ValueError('admin_audit_object_changed')
    ref = ObjectReference(type='insight', namespace=namespace, project_id=scope.project_id,
        object_id=current.object_id, revision=current.revision,
        lineage=Birth.model_validate(capture_lineage(tx, 'recognitions', current.object_id)))
    _save_proof(tx, binding, 'insight.forget' if effect.payload['state'] == 'forgotten' else 'insight.restore-recall',
        ref.model_dump(), effect=PreferenceEffect(collection='recognition_recall_preferences',
            object_id=effect.object_id, revision=effect.revision))


def observe_document_read(*, records, documents, project_id, document_id, revision):
    binding = AUDIT_REQUEST.get()
    if binding is None:
        return
    binding.validate(records, method='GET', path='/api/v2/library/documents/{document_id}')
    if binding.application.state.recognition_documents is not documents:
        raise ValueError('admin_audit_binding_invalid')
    with records.begin_read() as reader:
        ref = _reference(reader, records, documents, project_id, document_id, revision, writing=False)
    if ref is not None:
        if binding.observations:
            raise ValueError('admin_audit_observation_invalid')
        binding.observations.append({'action': 'document.read', 'object': ref})


def _intent(audit, access, identity):
    authorize_user(audit.users, access.caller, access.target_user_id)
    row = audit.records.read(AUDIT, identity)
    if (row is None or row.payload.get('phase') != 'intent'
            or not (audit.users.can(access.caller, action='manage_users')
                    or row.payload.get('target_user_id') == access.caller.user_id)):
        raise HTTPException(404, 'audit_not_found')
    return row.payload


async def object_details(application, audit, identity):
    access = USER_ACCESS.get()
    intent = _intent(audit, access, identity)
    target_access = authorize_user(audit.users, access.caller, intent['target_user_id'])
    pool = application.state.server_user_pool
    result = {'audit_id': identity, 'business_state': 'unknown', 'action': None, 'objects': []}
    from .library import LibraryRead
    async with pool.lease(target_access.target_user_id) as child:
        state = child.state
        root = audit.users.root_for(target_access.target_user_id).resolve()
        if (state.server_user_id != target_access.target_user_id
                or Path(state.recognition_runtime_root).resolve() != root
                or not Path(state.recognition_records.database_path).resolve().is_relative_to(root)):
            raise UserError('user_forbidden')
        records, documents = state.recognition_records, state.recognition_documents
        proof_row = records.read(PROOFS, identity)
        references = []
        if proof_row is not None:
            proof = DomainProof.model_validate(proof_row.payload)
            if (proof_row.revision != 1 or proof.intent_id != identity
                    or any(getattr(proof, key) != intent[key] for key in (
                        'actor_user_id', 'actor_device_id', 'target_user_id'))):
                raise ValueError('admin_audit_proof_invalid')
            result.update(business_state=proof.business_state, action=proof.action)
            references = proof.objects
        else:
            outcomes = [row for row in audit.records.list(AUDIT)
                if row.payload.get('phase') == 'outcome' and row.payload.get('intent_id') == identity
                and all(row.payload.get(key) == intent[key] for key in (
                    'actor_user_id', 'actor_device_id', 'target_user_id'))]
            if len(outcomes) == 1:
                observations = outcomes[0].payload.get('observations', [])
                if (not isinstance(observations, list) or len(observations) > 1
                        or any(set(item) != {'action', 'object'} or item['action'] != 'document.read' for item in observations)):
                    raise ValueError('admin_audit_observation_invalid')
                references = [ObjectReference.model_validate(item['object']) for item in observations]
                if references:
                    result['action'] = 'document.read'
        read = LibraryRead(records, state.recognition_service, documents, state.workspace_domains)
        for ref in references:
            item = {'type': ref.type, 'project_id': ref.project_id, 'object_id': ref.object_id,
                'recorded_revision': ref.revision, 'state': 'unavailable'}
            if (ref.type == 'document' and ref.namespace == documents.namespace_id
                    and ref.lineage.collection == 'documents' and ref.lineage.object_id == ref.object_id):
                docs = read.docs(ref.project_id, include_archived=True)
                document = docs.get(ref.object_id)
                with records.begin_read() as reader:
                    try:
                        verify_lineage(reader, ref.lineage.model_dump())
                    except RecordLineageError:
                        document = None
                    current = reader.read('documents', ref.object_id)
                    if (document is not None and current is not None
                            and current.payload.get('project_id') == ref.project_id
                            and current.payload.get('revision') == document.get('revision')
                            and current.payload.get('title') == document.get('title')
                            and type(document.get('revision')) is int and document['revision'] >= ref.revision):
                        item.update(state='available', current_title=document['title'], current_revision=document['revision'])
            if (ref.type == 'project' and ref.namespace == getattr(records, 'namespace', None)
                    and ref.lineage.collection == 'v2_projects' and ref.lineage.object_id == ref.object_id
                    and ref.project_id == ref.object_id):
                from .projects import _view
                with records.begin_read() as reader:
                    try:
                        verify_lineage(reader, ref.lineage.model_dump())
                    except RecordLineageError:
                        current = None
                    else:
                        current = reader.read('v2_projects', ref.object_id)
                    if current is not None and current.revision >= ref.revision:
                        project = _view(reader, current)
                        item.update(state='available', current_title=project['name'], current_revision=current.revision)
            if (ref.type == 'insight' and ref.namespace == getattr(records, 'namespace', None)
                    and ref.lineage.collection == 'recognitions' and ref.lineage.object_id == ref.object_id):
                from backend.recognition import WorkScope
                from backend.security.user_context import user_context
                from .insights import insight_view
                with user_context(target_access):
                    view = insight_view(records, WorkScope('local-user', ref.project_id), ref.object_id,
                                        service=state.recognition_service)
                with records.begin_read() as reader:
                    try:
                        verify_lineage(reader, ref.lineage.model_dump())
                    except RecordLineageError:
                        current = None
                    else:
                        current = reader.read('recognitions', ref.object_id)
                    if (view is not None and current is not None and view['id'] == ref.object_id
                            and current.payload.get('scope') == {'user_id': 'local-user', 'project_id': ref.project_id}
                            and current.revision == view['revision'] and current.revision >= ref.revision):
                        item.update(state='available', current_label='认识 ' + ref.object_id, current_revision=current.revision)
            result['objects'].append(item)
        _intent(audit, access, identity)
        if (not state.server_context.registry.is_current(access.caller)
                or authorize_user(audit.users, access.caller, intent['target_user_id']) != target_access):
            raise UserError('user_unauthorized')
    return result
