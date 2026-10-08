"""Existing activity and usage sidecar storage shared by legacy and v2 routes."""
from datetime import datetime, timezone
import logging
import math
from uuid import uuid4

from backend.recognition.correction_events import record_correction as _record_correction

_LOGGER = logging.getLogger('backend.memory_app.v2.usage')


def record_correction(transaction, previous, current, *, object_kind, event_type='edit', after=None,
                      event_id=None, at=None, turn_id=None):
    """保留旧入口与调用时的时钟替换点，事实写入由领域叶唯一实现。"""
    return _record_correction(transaction, previous, current, object_kind=object_kind,
        event_type=event_type, after=after, event_id=event_id, at=at, turn_id=turn_id,
        clock=utc_now)

class UsageUnavailable(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value, fallback):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else fallback
    except (AttributeError, TypeError, ValueError):
        return fallback


def half_life_days(count, project_id):
    from backend.memory_app.v2.policies import get
    from backend.memory_app.v2.policies.types import StrengthInput
    return get('strength')(StrengthInput(count, project_id)).half_life_days


def decayed_score(payload, now):
    from backend.memory_app.v2.policies import get
    from backend.memory_app.v2.policies.types import StrengthInput
    return get('strength')(StrengthInput(payload['count'], payload['project_id'],
        payload['score'], timestamp(payload['updated_at'], now), now)).score


def recall_weight(records, kind, id):
    if kind == 'insight':
        candidate = records.read('recognition_candidates', id)
        if candidate and candidate.payload.get('state') == 'published':
            id = candidate.payload['recognition_id']
        row = records.read('recognition_recall_preferences', id)
    elif kind in {'document', 'summary', 'note'}:
        row = records.read('v2_document_recall', id)
    else:
        return 1.0
    return {'normal': 1.0, 'cooled': .5, 'forgotten': 0.0}.get(row.payload.get('state'), 1.0) if row else 1.0


class UsageStorage:
    def __init__(self, records, *, now=utc_now):
        self.records, self.now = records, now

    def recall_weight(self, kind, id):
        return recall_weight(self.records, kind, id)

    def _object(self, reader, kind, identity, project):
        if kind in {'source', 'candidate'}:
            return None, None
        if kind in {'document', 'summary', 'note'}:
            row = reader.read('documents', identity)
            if row is None or row.payload.get('project_id') != project:
                raise UsageUnavailable('usage_object_unavailable')
            return 'document', row
        raise ValueError('invalid_usage_kind')

    def record_usage(self, kind, id, project_id, weight, *, reset=False, count=True, event_kind=None):
        return self._record_usage(kind, id, project_id, weight, reset=reset, count=count, event_kind=event_kind)

    def initialize(self, kind, id, project_id):
        return self._record_usage(kind, id, project_id, 1.0, reset=True, count=True, initialize=True)

    def _record_usage(self, kind, id, project_id, weight, *, reset, count, initialize=False, event_kind=None):
        if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(weight) or weight < 0 or type(reset) is not bool or type(count) is not bool):
            raise ValueError('invalid_usage_weight')
        if event_kind is not None and (not isinstance(event_kind, str) or not event_kind.strip()):
            raise ValueError('invalid_usage_event_kind')
        now = self.now()
        with self.records.begin() as tx:
            canonical, target = self._object(tx, kind, id, project_id)
            if target is None:
                return None
            collection = 'v2_usage_' + canonical
            current = tx.read(collection, target.object_id)
            if current is not None and current.payload.get('project_id') != project_id:
                raise UsageUnavailable('usage_object_unavailable')
            if initialize and current is not None:
                return dict(current.payload)
            baseline = (current.payload if current is not None else {
                'project_id': project_id, 'score': 1.0, 'count': 1,
                'updated_at': target.payload.get('created_at') or now.isoformat(),
            })
            score = float(weight) if reset else decayed_score(baseline, now) + weight
            # Confirmation creates the baseline; editing/restoring is a later use.
            uses = 1 if initialize else baseline['count'] + int(count)
            events = [*baseline.get('events', []), {'at': now.isoformat(),
                'kind': event_kind or ('initialize' if initialize else 'spread' if not count else 'reset' if reset else 'use')}]
            older_count = baseline.get('older_count', 0) + max(0, len(events) - 64)
            updated = tx.put(collection, target.object_id, {
                'project_id': project_id, 'score': score, 'count': uses, 'updated_at': now.isoformat(),
                'events': events[-64:], 'older_count': older_count,
            }, expected_revision=current.revision if current is not None else 0)
            tx.commit()
        self._after_usage(canonical, target, project_id, count=count, initialize=initialize)
        return dict(updated.payload)

    def _after_usage(self, canonical, target, project_id, *, count, initialize):
        """Product subclasses may spread activation after the committed write."""
        return None


def safe_record_document_usage(records, kind, identity, project, weight, **options):
    try:
        return UsageStorage(records).record_usage(kind, identity, project, weight, **options)
    except Exception as error:
        # Storage exceptions can contain private data; never log their messages.
        _LOGGER.warning('usage_write_failed kind=%s exception_type=%s', kind, type(error).__name__)
        return None


def record_activity(records, kind, project_id, object_id, *, event_id=None, at=None):
    """A failed statistical write must never change the primary operation."""
    try:
        identity = event_id or 'activity-' + uuid4().hex
        with records.begin() as tx:
            existing = tx.read('v2_activity', identity)
            if existing is not None:
                return dict(existing.payload)
            row = tx.put('v2_activity', identity, {
                'kind':kind, 'project_id':project_id, 'object_id':object_id,
                'at':(at or utc_now()).isoformat(),
            }, expected_revision=0)
            tx.commit()
        return dict(row.payload)
    except Exception as error:
        logging.getLogger('backend.memory_app.v2.stats').warning('activity_write_failed exception_type=%s', type(error).__name__)
        return None
