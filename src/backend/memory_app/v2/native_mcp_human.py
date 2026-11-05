from uuid import uuid4
from typing import Literal
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.storage_provider.record_lineage import capture_lineage, verify_lineage, RecordLineageError
from backend.recognition import WorkScope
from ..source_egress import recognition_service
from ..transaction_records import TransactionRecords
from .devices import _credential, _digest
from .native_mcp_scope import NativeMcpError
from .policies import get
from .usage import UsageService
from backend.shared.memory_sidecars import record_activity


NONCES = 'v2_native_mcp_human_review_nonces'


class HumanNonce(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal[1]
    actor_user_id: str
    device_id: str
    device_revision: int = Field(gt=0)
    target_user_id: str
    project_id: str
    candidate_id: str
    candidate_revision: int = Field(gt=0)
    candidate_fact_id: str = Field(pattern='^[0-9a-f]{32}$')
    action: Literal['confirm', 'discard']
    expires_at: int = Field(gt=0)
    issuer_instance: str = Field(pattern='^[0-9a-f]{32}$')
    consumed: bool
    result_id: str | None
    result_revision: int | None = Field(gt=0)

    @classmethod
    def parse(cls, value):
        if not isinstance(value, Mapping) or type(value.get('schema_version')) is not int:
            raise NativeMcpError('native_mcp_human_nonce_invalid')
        try:
            parsed = cls.model_validate(value)
        except ValidationError:
            raise NativeMcpError('native_mcp_human_nonce_invalid') from None
        if (type(parsed.schema_version) is not int
                or parsed.consumed and (parsed.result_id is None or parsed.result_revision is None)
                or not parsed.consumed and (parsed.result_id is not None or parsed.result_revision is not None)):
            raise NativeMcpError('native_mcp_human_nonce_invalid')
        return parsed.model_dump()


class HumanReview:
    def __init__(self, credentials, service):
        self.credentials, self.service = credentials, service
        self.instance = uuid4().hex
        self.policy = get('native_mcp_auth', version='@1')()

    def _authorize(self, actor, target):
        with self.credentials.records.begin_read() as tx:
            self.credentials.full_caller(tx, actor)
            self.credentials._target(tx, actor, target)

    def _candidate(self, tx, *, project, candidate_id, revision):
        if type(revision) is not int or revision < 1 or not isinstance(candidate_id, str) or not isinstance(project, str):
            raise NativeMcpError('native_mcp_candidate_unavailable')
        row = tx.read('recognition_candidates', candidate_id)
        if (row is None or row.revision != revision or row.payload.get('state') != 'pending'
                or row.payload.get('scope') != {'user_id': 'local-user', 'project_id': project}):
            raise NativeMcpError('native_mcp_candidate_unavailable')
        return row

    def prepare(self, records, *, actor, target, project, candidate_id, revision, action):
        self._authorize(actor, target)
        if not isinstance(action, str) or action not in {'confirm', 'discard'}:
            raise NativeMcpError('native_mcp_human_action_invalid')
        nonce = _credential()
        with records.begin() as tx:
            self._candidate(tx, project=project, candidate_id=candidate_id, revision=revision)
            lineage = capture_lineage(tx, 'recognition_candidates', candidate_id)
            value = {'schema_version': 1, 'actor_user_id': actor.user_id, 'device_id': actor.device_id,
                'device_revision': actor.revision, 'target_user_id': target, 'project_id': project,
                'candidate_id': candidate_id, 'candidate_revision': revision, 'candidate_fact_id': lineage['fact_id'],
                'action': action, 'expires_at': self.credentials.now() + self.policy['human_seconds'],
                'issuer_instance': self.instance, 'consumed': False, 'result_id': None, 'result_revision': None}
            self._put(tx, 'nonce-' + _digest(nonce), value, expected_revision=0)
            tx.commit()
        return {'nonce': nonce, 'expires_at': value['expires_at'], 'action': action,
            'candidate_id': candidate_id, 'candidate_revision': revision}

    def _put(self, tx, object_id, value, *, expected_revision):
        # 共享事务只开放具名旁路写入，普通事务沿用原 put。
        value = HumanNonce.parse(value)
        writer = getattr(tx, 'put_human_review_nonce', None)
        return (writer(object_id, value, expected_revision=expected_revision) if writer is not None
            else tx.put(NONCES, object_id, value, expected_revision=expected_revision))

    def commit(self, records, *, actor, target, project, candidate_id, revision, action, nonce):
        self._authorize(actor, target)
        digest = _digest(nonce)
        if digest is None:
            raise NativeMcpError('native_mcp_human_nonce_invalid')
        with records.begin() as tx:
            row = tx.read(NONCES, 'nonce-' + digest)
            if row is None:
                raise NativeMcpError('native_mcp_human_nonce_invalid')
            value = HumanNonce.parse(row.payload)
            expected = {'actor_user_id': actor.user_id, 'device_id': actor.device_id,
                'device_revision': actor.revision, 'target_user_id': target, 'project_id': project,
                'candidate_id': candidate_id, 'candidate_revision': revision, 'action': action,
                'issuer_instance': self.instance, 'schema_version': 1}
            if (any(value.get(key) != item for key, item in expected.items())
                    or value['expires_at'] <= self.credentials.now()):
                raise NativeMcpError('native_mcp_human_nonce_invalid')
            try:
                verify_lineage(tx, {'collection': 'recognition_candidates', 'object_id': candidate_id,
                    'fact_id': value['candidate_fact_id']})
            except RecordLineageError:
                raise NativeMcpError('native_mcp_candidate_changed') from None
            if value['consumed']:
                return {'id': value['result_id'], 'revision': value['result_revision'], 'action': action}
            self._candidate(tx, project=project, candidate_id=candidate_id, revision=revision)
            self._authorize(actor, target)
            enlisted = TransactionRecords(tx)
            service = recognition_service(enlisted, cache_invalidation=self.service.cache_invalidation,
                source_owner=records)
            operation = service.publish if action == 'confirm' else service.reject_candidate
            saved = operation(scope=WorkScope('local-user', project), candidate_id=candidate_id,
                expected_revision=revision, reviewer=actor.user_id)
            if action == 'confirm':
                UsageService(enlisted).initialize('insight', saved.id, project)
                record_activity(enlisted, 'confirm', project, saved.id, event_id='confirm-' + saved.id)
            self._put(tx, row.object_id, {**value, 'consumed': True, 'result_id': saved.id,
                'result_revision': saved.revision}, expected_revision=row.revision)
            tx.commit()
            return {'id': saved.id, 'revision': saved.revision, 'action': action}
