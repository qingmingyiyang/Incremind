from contextlib import contextmanager
from datetime import datetime, timezone
import logging

from pydantic import BaseModel, ConfigDict, Field

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.document_engine import SQLiteDocumentRepository
from ..document_recognition import CANDIDATE_SOURCE_CONSTRAINTS, ensure_document_experience
from ..original_sources import resolve_turn_material
from ..server_jobs import QuotaError, _IntakeBudget
from ..source_egress import SourceEgressService, recognition_service
from ..transaction_records import TransactionRecords
from .consolidation import PatternOutput
from .consolidation_events import original_identities
from .layers import facts_of, summary_of
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_readers import qualified_record_read
from .policies import get, override
from .policies.pipelines import versions_for_turn
from .policies.shared_consolidation import v1 as _POLICY
from .privacy import egress_allowed, is_private_project, privacy_revision
from .shared_auxiliary import SharedAuxiliaryTurn, _qualified_commit
from .shared_daily_authority import daily_owner
from .shared_organize import SharedIntakeRecords
from .shared_projects import PROJECTS, SharedProjectError
from .usage import timestamp


RUNS = 'v2_shared_daily_runs'
_POLICY_VERSION = '@1'
_LOGGER = logging.getLogger(__name__)


class _SharedPattern(PatternOutput):
    document_ids: list[str] = Field(min_length=2, max_length=_POLICY.max_documents)


class _Patterns(BaseModel):
    model_config = ConfigDict(extra='forbid')
    patterns: list[_SharedPattern] = Field(max_length=8)


def _document_allowed(reader, document, project):
    if document is None or document.get('project_id') != project or document.get('status') == 'archived':
        return False
    row = reader.read('v2_document_recall', document['id'])
    return not (row and row.payload.get('state') == 'forgotten' and row.payload.get('by', 'user') == 'user')


def _recent_documents(reader, documents, project, now):
    return sorted((document for document in documents.list()
        if _document_allowed(reader, document, project)
        and now - timestamp(document.get('created_at'), now) <= _POLICY.recent),
        key=lambda document: (document.get('created_at') or '', document['id']), reverse=True)


def _due(row, owner, project_revision, now):
    if row is None:
        return True
    payload = row.payload
    if payload['status'] == 'completed':
        return False
    if (payload['owner_user_id'], payload['project_revision']) != (owner, project_revision):
        return True
    return payload['status'] == 'running' and now - timestamp(payload['started_at'], now) >= _POLICY.lease


def _configuration(models):
    row = models.public_for_purpose('generation')
    return {key: row.get(key) for key in ('configured', 'enabled', 'allow_remote', 'revision',
        'provider', 'model', 'base_url', 'server_model_binding')}


class SharedDailyConsolidation:
    def __init__(self, context, *, now=lambda: datetime.now(timezone.utc)):
        self.context, self.now = context, now
        self.registry, self.factory = context.shared_projects, context.shared_query_factory

    def _projects(self, owner):
        return self.registry.records.list_matching(PROJECTS, owner_user_id=owner, state='active')

    def _time(self):
        return self.now().astimezone(timezone.utc)

    def due(self, owner):
        now = self._time()
        return any(self._project_due(project, owner, now) for project in self._projects(owner))

    def _project_due(self, project, owner, now):
        with self.registry.roots.lease(project.object_id):
            data = self.registry.roots._data(project.object_id)
            return (_due(data.records.read(RUNS, now.date().isoformat()), owner, project.revision, now)
                and not is_private_project(data.records, project.object_id)
                and len(_recent_documents(data.records, data.documents, project.object_id, now)) >= 2)

    def run_due(self, application, owner):
        now = self._time()
        for project in self._projects(owner):
            try:
                if self._project_due(project, owner, now):
                    with daily_owner(self.registry, application, project.object_id, owner) as actor:
                        self._run_project(self.factory.query(application, actor, project.object_id), now)
            except Exception as error:
                _LOGGER.warning('shared_daily_consolidation_rejected exception_type=%s', type(error).__name__)

    @contextmanager
    def _write(self, query, day):
        admission = query.origin_admission
        lease = admission.lease
        jobs = self.context.jobs
        # 容量锁只覆盖本次写事务，调用模型期间所有权仍可转让。
        with admission.actor.publication(), jobs.storage_guard((lease.actor_user_id,)):
            budget = _IntakeBudget(jobs, lease.actor_user_id)
            cap = budget.reserve_up_to(1048576)
            try:
                writer = SharedIntakeRecords(query, lease.actor_user_id, cap, action='consolidate',
                    identity='daily-' + day, title='每日整理')
                with writer.begin() as tx:
                    yield tx
            finally:
                cap.close()
                budget.active = False

    def _facts(self, query, now):
        admission = query.origin_admission
        project = admission.lease.project_id
        scope = WorkScope('local-user', project)
        with _qualified_commit(admission) as bound, qualified_record_read(admission.destination.records,
                validate=bound._validate) as actor_reader:
            origins = BoundMaterialOrigins(admission, admission.lease, {})
            reader = bound.reader_for(origins, origins.shared, actor_reader=TransactionRecords(actor_reader))
            documents = SQLiteDocumentRepository(TransactionRecords(reader), namespace_id=bound._data.namespace)
            authority = SourceEgressService(bound._data.records, reader=reader)
            facts, size = [], 0
            if not egress_allowed(reader, query.models, project, 'generation'):
                return facts, privacy_revision(reader)
            for document in _recent_documents(reader, documents, project, now):
                identity, revision = document['id'], document['revision']
                descriptor = {'type': 'document', 'id': identity, 'revision': revision, 'project_id': project}
                try:
                    _, roots = resolve_turn_material(reader, scope, descriptor)
                    snapshot = authority.snapshot(scope, roots)
                    authority.require(snapshot, 'generation')
                    originals = original_identities(bound._data.records, snapshot)
                except RecognitionConflict:
                    continue
                if not originals:
                    continue
                markdown = documents.markdown(identity, revision=revision)
                if not isinstance(markdown, str):
                    raise RecognitionConflict('shared_daily_document_changed')
                content = '\n'.join(part for part in (summary_of(markdown)[0], *facts_of(markdown)) if part) or markdown
                title = document['title']
                length = len(content) + len(title)
                if size + length > _POLICY.max_fact_chars:
                    continue
                size += length
                facts.append({'material': {**descriptor, 'namespace': origins.shared.namespace},
                    'snapshot': snapshot, 'originals': originals, 'text': content, 'title': title})
                if len(facts) >= _POLICY.max_documents:
                    break
            return facts, privacy_revision(reader)

    def _reserve(self, query, now, facts, privacy, *, run):
        lease, day = query.origin_admission.lease, now.date().isoformat()
        proof = [[fact['material'], fact['snapshot']] for fact in facts]
        payload = {'project_id': lease.project_id, 'owner_user_id': lease.actor_user_id,
            'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
            'account_revision': lease.account_revision, 'started_at': now.isoformat(), 'proof': proof,
            'privacy_revision': privacy, 'policy_version': _POLICY_VERSION,
            'status': 'running' if run else 'skipped', 'candidate_ids': []}
        with self._write(query, day) as tx:
            prior = tx.read(RUNS, day)
            if not _due(prior, lease.actor_user_id, lease.project_revision, now):
                return None
            if prior is not None and prior.payload['status'] == 'running' and (
                    prior.payload['owner_user_id'], prior.payload['project_revision']) == (
                        lease.actor_user_id, lease.project_revision):
                # 同一所有者接续旧请求，来源、隐私或资格变化时保留已派发结果的不确定性。
                if any(prior.payload[key] != payload[key] for key in ('proof', 'privacy_revision', 'policy_version',
                        'member_revision', 'account_revision')):
                    payload['status'] = 'failed'
            history = tx.list(RUNS)
            if any(row.object_id != day and row.payload['proof'] == proof
                    and row.payload['status'] == 'running'
                    and not _due(row, lease.actor_user_id, lease.project_revision, now) for row in history):
                return None
            if any(row.payload['status'] == 'completed' and row.payload['proof'] == proof for row in history):
                payload['status'] = 'skipped'
            # 日状态与候选处于同一数据库，只给候选写入共享项目活动。
            saved = tx.transaction.put(RUNS, day, payload, expected_revision=prior.revision if prior else 0)
            tx.commit()
            return saved if payload['status'] == 'running' else None

    def _validate(self, query, reservation, facts, configuration):
        admission = query.origin_admission
        query.validate_retrieval_origin()
        records = query.records
        if (records.read(RUNS, reservation.object_id) != reservation
                or privacy_revision(records) != reservation.payload['privacy_revision']
                or _configuration(query.models) != configuration
                or not egress_allowed(records, query.models, admission.lease.project_id, 'generation')):
            raise RecognitionConflict('shared_daily_run_changed')
        for fact in facts:
            material = fact['material']
            current = query.documents.read(material['id'])
            if not _document_allowed(records, current, admission.lease.project_id) or current['revision'] != material['revision']:
                raise RecognitionConflict('shared_daily_document_changed')

    def _finish(self, reservation, *, status, candidate_ids=(), tx):
        current = tx.read(RUNS, reservation.object_id)
        if current != reservation:
            raise RecognitionConflict('shared_daily_run_changed')
        tx.transaction.put(RUNS, current.object_id, {**current.payload, 'status': status,
            'candidate_ids': list(candidate_ids), 'finished_at': self._time().isoformat()}, expected_revision=current.revision)

    def _run_project(self, query, now):
        admission = query.origin_admission
        project, day = admission.lease.project_id, now.date().isoformat()
        if (not _due(query.records.read(RUNS, day), admission.lease.actor_user_id, admission.lease.project_revision, now)
                or is_private_project(query.records, project)
                or len(_recent_documents(query.records, query.documents, project, now)) < 2):
            return
        configuration = _configuration(query.models)
        ready = (configuration['configured'] is True and configuration['enabled'] is not False
                 and configuration['allow_remote'] is True)
        if ready:
            query.answer_turns._runtime()
        facts, privacy = self._facts(query, now) if ready else ([], privacy_revision(query.records))
        originals = set().union(*(fact['originals'] for fact in facts))
        reservation = self._reserve(query, now, facts, privacy,
            run=ready and len(facts) >= 2 and len(originals) >= 2)
        if reservation is None:
            return
        try:
            with override(**versions_for_turn('memory.consolidate')):
                self._generate(query, reservation, facts, configuration)
        except Exception:
            try:
                with self._write(query, day) as tx:
                    self._finish(reservation, status='failed', tx=tx)
                    tx.commit()
            except (RecognitionError, SharedProjectError, QuotaError) as error:
                _LOGGER.warning('shared_daily_status_write_rejected exception_type=%s', type(error).__name__)
            raise

    def _generate(self, query, reservation, facts, configuration):
        admission = query.origin_admission
        project = admission.lease.project_id
        source = {fact['material']['id']: fact for fact in facts}
        policy = get('shared_consolidation', version=reservation.payload['policy_version'])
        turn = SharedAuxiliaryTurn(admission, query.models, kind='memory.consolidate', project=project,
            key={'shared_daily': reservation.object_id, 'documents': list(source), 'policy': _POLICY_VERSION},
            materials=[fact['material'] for fact in facts],
            validate=lambda: self._validate(query, reservation, facts, configuration),
            automatic={'producer': 'consolidation', 'mode': 'project', 'scene': None,
                'anchors': [{'type': 'document', 'id': identity} for identity in source]})
        messages = policy(source, constraints=CANDIDATE_SOURCE_CONSTRAINTS)
        output, _ = turn.generate(messages, response_model=_Patterns, max_tokens=policy.max_output_tokens)
        for pattern in output.patterns:
            identities = set(pattern.document_ids)
            if (not pattern.text.strip() or len(identities) != len(pattern.document_ids) or not identities <= source.keys()
                    or not get('consolidate')(None, operation='accept', kind='pattern',
                        source_count=len(set().union(*(source[identity]['originals'] for identity in identities))))):
                raise RecognitionConflict('shared_daily_pattern_sources_invalid')

        def existing():
            current = query.records.read(RUNS, reservation.object_id)
            return current.payload['candidate_ids'] if current and current.payload['status'] == 'completed' else None

        def write():
            with self._write(query, reservation.object_id) as tx, _qualified_commit(admission) as bound:
                with admission.destination.records.begin() as actor_reader:
                    turn.validate_commit(actor_reader)
                    # 候选、原件资格和日状态在同一共享写事务内提交。
                    if (_configuration(query.models) != configuration
                            or privacy_revision(tx) != reservation.payload['privacy_revision']
                            or not egress_allowed(tx, query.models, project, 'generation')):
                        raise RecognitionConflict('shared_daily_run_changed')
                    records = TransactionRecords(tx)
                    documents = SQLiteDocumentRepository(records, namespace_id=bound._data.namespace)
                    service = recognition_service(records, source_owner=query.material_owner)
                    scope = WorkScope('local-user', project)
                    candidates = []
                    for number, pattern in enumerate(output.patterns, 1):
                        experience_ids = []
                        for identity in pattern.document_ids:
                            document = documents.read(identity)
                            if (not _document_allowed(tx, document, project)
                                    or document['revision'] != source[identity]['material']['revision']):
                                raise RecognitionConflict('shared_daily_document_changed')
                            experience, _ = ensure_document_experience(documents, service, project, identity)
                            experience_ids.append(experience)
                        candidate = service.propose_in_uow(tx, scope=scope, content=pattern.text.strip(),
                            conditions=pattern.conditions, source_experience_ids=experience_ids,
                            candidate_id=f'pattern-{turn.turn_id}-{number}')
                        tx.transaction.put('v2_insight_patterns', candidate.id, {'kind': 'pattern', 'project_id': project,
                            'document_ids': list(pattern.document_ids), 'source_candidate_ids': []}, expected_revision=0)
                        candidates.append(candidate.id)
                    self._finish(reservation, status='completed', candidate_ids=candidates, tx=tx)
                    tx.commit()
                    return candidates

        turn.propose(key='shared-daily', write=write, existing=existing)
