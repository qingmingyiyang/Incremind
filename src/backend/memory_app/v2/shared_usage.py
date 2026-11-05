import logging
import sqlite3

from backend.recognition import RecognitionConflict
from backend.recognition.external_turn_facts import completed_answer_facts
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..transaction_records import TransactionRecords
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_readers import qualified_record_read
from .shared_auxiliary import _qualified_commit
from .shared_rendered_preparation import _query_current
from .shared_runtime import SharedWriteRecords
from .usage import UsageService, UsageUnavailable, record_answer_usage
from .bookshelf import relearn_cited
from .shared_projects import SharedProjectError


_LOGGER = logging.getLogger(__name__)


def record_shared_answer_usage(query, chosen, citations, *, turn_id):
    try:
        usage = SharedUsage(query)
        usage.answer(chosen, citations, turn_id=turn_id)
    except (ValueError, OSError, sqlite3.Error, SQLiteUnitOfWorkConflict) as error:
        _LOGGER.warning('shared_answer_usage_unavailable exception_type=%s', type(error).__name__)


class SharedUsage:
    def __init__(self, query):
        _query_current(query)
        self.query, self.admission = query, query.origin_admission

    def _profile_usage(self, candidate, *, turn_id):
        records = self.admission.destination.records
        record_answer_usage(records, [candidate], [{'n': 1}], 'me')
        relearn_cited(records, [candidate], [{'n': 1}], turn_id=turn_id)

    def _target(self, kind, identity):
        admission = self.admission
        origins = BoundMaterialOrigins(admission, admission.lease, {})
        with _qualified_commit(admission) as scope:
            with qualified_record_read(admission.destination.records, validate=scope._validate) as actor:
                reader = scope.reader_for(origins, origins.shared, actor_reader=TransactionRecords(actor))
                role = admission.registry.authorize(scope._central, admission.actor, admission.lease.project_id).role
                canonical, target = UsageService(TransactionRecords(reader), source_owner=origins.shared)._object(
                    reader, kind, identity, admission.lease.project_id)
                return role, canonical, target

    def record(self, kind, identity, *, event_kind):
        role, canonical, target = self._target(kind, identity)
        if target is None or role == 'viewer':
            return None
        admission, lease = self.admission, self.admission.lease
        writer = SharedWriteRecords(admission.registry, admission.actor, lease.project_id,
            authority={'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                'account_revision': lease.account_revision}, action='use', object_kind=canonical,
            object_id=target.object_id,
            object_title=str(target.payload.get('title') or target.payload.get('content') or target.object_id)[:80])
        with writer.begin() as tx:
            records = TransactionRecords(tx)
            value = UsageService(records, source_owner=writer).record_usage(
                kind, identity, lease.project_id, 1.0, event_kind=event_kind)
            tx.commit()
            return value

    def answer(self, chosen, citations, *, turn_id):
        admission = self.admission
        facts = completed_answer_facts(admission.destination.records,
            project_id=admission.lease.project_id, turn_id=turn_id)
        if facts['result'].get('chosen') != chosen or facts['answer']['citations'] != citations:
            raise RecognitionConflict('shared_usage_answer_changed')
        cited, events, shared, shared_citations = {value['n'] for value in citations}, set(), [], []
        for number, candidate in enumerate(chosen, 1):
            if number not in cited or candidate['layer'] not in {'L3', 'L2', 'L1', 'inspiration'}:
                continue
            if candidate.get('project_id') == 'me':
                self._profile_usage(candidate, turn_id=turn_id)
                continue
            if candidate.get('project_id') != admission.lease.project_id:
                raise RecognitionConflict('shared_usage_material_scope_changed')
            kind = ('inspiration' if candidate['layer'] == 'inspiration'
                else 'insight' if candidate['layer'] == 'L3' else 'document')
            identity = candidate['entry']['document_id' if kind == 'document' else 'id']
            events.add((kind, identity))
            shared.append(candidate)
            shared_citations.append({'n': len(shared)})
        try:
            for kind, identity in sorted(events):
                self.record(kind, identity, event_kind='citation')
            if shared and any(candidate.get('bookshelf_restore') for candidate in shared):
                lease = admission.lease
                role, _canonical, _target = self._target(*next(iter(events)))
                if role == 'viewer':
                    return
                writer = SharedWriteRecords(admission.registry, admission.actor, lease.project_id,
                    authority={'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                        'account_revision': lease.account_revision}, action='revive', object_kind='memory',
                    object_id=lease.project_id, object_title='资料恢复')
                with writer.begin() as tx:
                    relearn_cited(TransactionRecords(tx), shared, shared_citations)
                    tx.commit()
        except (SharedProjectError, RecognitionConflict, SQLiteUnitOfWorkConflict, UsageUnavailable) as error:
            # 计数在原答案提交之后发生，当前统计资格变化不改写本人已经完成的答案事实。
            _LOGGER.warning('shared_answer_usage_unavailable exception_type=%s', type(error).__name__)
