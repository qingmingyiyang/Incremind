"""Daily recall transitions; original bodies and content revisions stay untouched."""
from uuid import uuid4

from core.storage_provider import SQLiteUnitOfWorkConflict
from ..recall_state import COLLECTION
from .usage import decayed_score, timestamp, utc_now
from .interference import COLLECTION as INTERFERENCE, recovery_allowed
from .policies import get
from .policies.types import ForgetInput


class AutoForget:
    def __init__(self, records, *, now=utc_now):
        self.records, self.now = records, now

    def run(self):
        now = self.now()
        changed = 0
        for kind, collection in (('insight', 'recognitions'), ('document', 'documents')):
            for target in self.records.list(collection):
                payload = target.payload
                if kind == 'insight' and payload.get('state') != 'active':
                    continue
                if kind == 'document' and payload.get('status') == 'archived':
                    continue
                if (now - timestamp(payload.get('created_at'), now)).total_seconds() < 30 * 86400:
                    continue
                project = payload.get('scope', {}).get('project_id') if kind == 'insight' else payload.get('project_id')
                if not isinstance(project, str):
                    continue
                usage = self.records.read('v2_usage_' + kind, target.object_id)
                if usage and usage.payload.get('project_id') != project:
                    continue
                score = decayed_score(usage.payload if usage else {
                    'project_id':project, 'score':1.0, 'count':1, 'updated_at':payload['created_at'],
                }, now)
                recall_collection = COLLECTION if kind == 'insight' else 'v2_document_recall'
                previous = self.records.read(recall_collection, target.object_id)
                if (kind == 'insight' and previous and (previous.payload.get('project_id') != project
                        or previous.payload.get('user_id') != payload['scope']['user_id'])):
                    continue
                old = previous.payload.get('state', 'normal') if previous else 'normal'
                # Forgotten memories require explicit restoration or bookshelf use.
                if old == 'forgotten':
                    continue
                interference = self.records.read(INTERFERENCE, target.object_id) if kind == 'insight' else None
                can_recover = recovery_allowed(interference, usage)
                state = get('forget')(ForgetInput(score, old, kind, project, can_recover))
                if old == state:
                    continue
                try:
                    with self.records.begin() as tx:
                        current = tx.read(collection, target.object_id)
                        current_usage = tx.read('v2_usage_' + kind, target.object_id)
                        current_recall = tx.read(recall_collection, target.object_id)
                        current_interference = tx.read(INTERFERENCE, target.object_id) if kind == 'insight' else None
                        if (current != target or current_usage != usage or current_recall != previous
                                or current_interference != interference):
                            continue
                        recall = {'state':state, 'by':'auto', 'changed_at':now.isoformat()}
                        if kind == 'insight':
                            recall.update(id=target.object_id, project_id=project, user_id=payload['scope']['user_id'])
                        tx.put(recall_collection, target.object_id, recall,
                               expected_revision=previous.revision if previous else 0)
                        event = 'forget' if state == 'forgotten' else 'cool' if state == 'cooled' else 'revive'
                        tx.put('v2_activity', 'activity-' + uuid4().hex, {
                            'kind':event, 'by':'auto', 'project_id':project, 'object_kind':kind,
                            'object_id':target.object_id, 'created_at':now.isoformat(),
                        }, expected_revision=0)
                        tx.commit()
                        changed += 1
                except SQLiteUnitOfWorkConflict:
                    continue
        return changed
