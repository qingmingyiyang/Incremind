"""External-client intake through the existing original and pending owners."""
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from core.storage_provider.record_lineage import capture_lineage

from backend.recognition import WorkScope
from ..source_egress import recognition_service
from backend.recognition.external_input_dependencies import COLLECTION, freeze_references
from backend.shared.secret_detection import redact_secrets
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _now, _project, _text
from .external_agent_guard import ExternalAgentGuardError
from .projects import assign_scene


INTAKES = 'v2_external_agent_intakes'
_TOOLS = {'remember', 'propose_insight'}


def _invalid():
    raise HTTPException(400, 'external_agent_request_invalid')


def _arguments(value, tool):
    if (not isinstance(value, dict) or set(value) != {'client', 'arguments'}
            or not isinstance(value['client'], str) or value['client'] not in {'claude', 'codex'}
            or not isinstance(value['arguments'], dict) or tool not in _TOOLS):
        _invalid()
    arguments = value['arguments']
    allowed = ({'text', 'url', 'project', 'scene'} if tool == 'remember'
               else {'text', 'conditions', 'project', 'scene', 'evidence_ids'})
    if set(arguments) - allowed:
        _invalid()
    if tool == 'remember':
        if ('text' in arguments) == ('url' in arguments):
            _invalid()
    elif 'text' not in arguments or 'project' not in arguments:
        _invalid()
    try:
        project = _project(arguments.get('project', 'inbox'))
        scene = arguments.get('scene')
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            _invalid()
        for field in ('text', 'url'):
            if field in arguments:
                _text(arguments[field], field)
        if 'url' in arguments and redact_secrets(arguments['url']) != arguments['url']:
            _invalid()
    except HTTPException:
        _invalid()
    conditions = arguments.get('conditions', [])
    if (not isinstance(conditions, list)
            or any(not isinstance(condition, str) or not condition.strip() for condition in conditions)):
        _invalid()
    evidence = arguments.get('evidence_ids', [])
    if (not isinstance(evidence, list) or any(not isinstance(ref, dict)
            or set(ref) != {'turn_id', 'id'} or any(not isinstance(ref[key], str) or not ref[key]
                for key in ref) for ref in evidence)):
        _invalid()
    return value['client'], arguments, project, scene.strip() if scene is not None else None


def _receipt(tx, identity, *, owner_id, client, tool, project, object_id, revision, scene):
    payload = {'schema_version': 1, 'owner_id': owner_id, 'client': client, 'tool': tool,
        'project_id': project, 'object_id': object_id, 'object_revision': revision, 'created_at': _now()}
    if scene is not None:
        payload['scene'] = scene
    tx.put(INTAKES, identity, payload, expected_revision=0)


class _ClientItems:
    """Enlist the actual item owner only after the existing intake acquired text."""

    def __init__(self, items, *, records, reserve_write, owner_id, client, scene):
        self.items, self.records, self.reserve_write = items, records, reserve_write
        self.owner_id, self.client, self.scene = owner_id, client, scene

    def create(self, project, kind, title, source_text, **extra):
        receipt_id = 'external-intake-' + uuid4().hex
        with self.records.begin() as tx:
            self.reserve_write(tx, receipt_id=receipt_id, client=self.client,
                tool='remember', project_id=project)
            enlisted = TransactionRecords(tx)
            items = self.items.with_records(enlisted)
            item = items.create(project, kind, redact_secrets(title), redact_secrets(source_text), **extra)
            if self.scene is not None:
                assign_scene(enlisted, 'item', item['id'], project, self.scene)
            _receipt(tx, receipt_id, owner_id=self.owner_id, client=self.client, tool='remember',
                project=project, object_id=item['id'], revision=item['revision'], scene=self.scene)
            tx.commit()
        return {'receipt_id': receipt_id, 'item_id': item['id'], 'revision': item['revision'],
            'project_id': project, 'client': self.client, 'state': item['status'], 'verified': False}


def install_mcp_intake_routes(application, *, workspace, records, service, reserve_write):
    """Install writes with the original owner's explicitly enlisted quota method."""
    api = application.state.external_context
    if (api.records is not records or service.records is not records
            or workspace.items.records is not records or not callable(reserve_write)):
        raise ValueError('external_context_owner_changed')
    owner_id = api.owner_id
    router = APIRouter(prefix='/api/v2/external-agent/mcp')

    async def inputs(request, tool):
        try:
            value = await request.json()
        except Exception:
            _invalid()
        return _arguments(value, tool)

    @router.post('/remember')
    async def remember(request: Request):
        client, arguments, project, scene = await inputs(request, 'remember')
        try:
            # Admission precedes even the public-link acquisition. The quota
            # owner rechecks it again in the actual writer transaction.
            api.guard.check_write(client=client, tool='remember', project_id=project)
            # Intake keeps URL validation, DNS pinning and media classification.
            # Its call to this thin create delegate follows acquisition, so no
            # writer transaction remains open while awaiting the public URL.
            intake = workspace.intake.with_items(_ClientItems(workspace.items,
                records=records, reserve_write=reserve_write, owner_id=owner_id,
                client=client, scene=scene), workspace.intake.models)
            result = await (intake.add_link({'project_id': project, 'url': arguments['url']})
                if 'url' in arguments else intake.add_text({'project_id': project,
                    'text': redact_secrets(arguments['text'])}))
        except ExternalAgentGuardError as error:
            raise HTTPException(409, str(error)) from None
        except HTTPException:
            _invalid()
        except Exception:
            raise HTTPException(409, 'external_context_unavailable') from None
        return {'turn_id': None, 'result': result}

    @router.post('/propose_insight')
    async def propose_insight(request: Request):
        client, arguments, project, scene = await inputs(request, 'propose_insight')
        receipt_id = 'external-intake-' + uuid4().hex
        try:
            scope = WorkScope(owner_id, project)
            content = redact_secrets(arguments['text'])
            conditions = [redact_secrets(condition) for condition in arguments.get('conditions', [])]
            with records.begin() as tx:
                reserve_write(tx, receipt_id=receipt_id, client=client,
                    tool='propose_insight', project_id=project)
                enlisted = TransactionRecords(tx)
                writer = recognition_service(enlisted, cache_invalidation=service.cache_invalidation)
                evidence = arguments.get('evidence_ids', [])
                references = freeze_references(tx, scope, client, evidence,
                    _sql_validator=writer._external_input_validator)[0] if evidence else None
                experience = writer.stage_experience(scope=scope, content=content,
                    provenance={'kind': 'user_statement', 'actor': client},
                    **({'experience_id': 'experience-external-v1-' + uuid4().hex} if evidence else {}))
                if references is not None:
                    own_identity = capture_lineage(tx, 'recognition_experiences', experience)
                    tx.put(COLLECTION, experience, {'schema_version': 2, 'owner_id': owner_id, 'client': client,
                        'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
                        'experience_id': experience, 'experience_revision': 1,
                        'ownexperience_identity': own_identity, 'references': references}, expected_revision=0)
                candidate = writer.propose_in_uow(tx, scope=scope, content=content,
                    source_experience_ids=[experience], conditions=conditions)
                if scene is not None:
                    assign_scene(enlisted, 'candidate', candidate.id, project, scene)
                _receipt(tx, receipt_id, owner_id=owner_id, client=client, tool='propose_insight',
                    project=project, object_id=candidate.id, revision=candidate.revision, scene=scene)
                tx.commit()
        except ExternalAgentGuardError as error:
            raise HTTPException(409, str(error)) from None
        except Exception:
            raise HTTPException(409, 'external_context_unavailable') from None
        return {'turn_id': None, 'result': {'receipt_id': receipt_id, 'candidate_id': candidate.id,
            'revision': candidate.revision, 'project_id': project, 'client': client, 'state': candidate.state}}

    application.include_router(router)
