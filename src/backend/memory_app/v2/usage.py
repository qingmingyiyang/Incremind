"""Revision-independent usage of published insights and their documents."""
from datetime import datetime, timezone
import logging
import math

from fastapi import APIRouter, HTTPException, Request, Response

from backend.recognition import RecognitionConflict, WorkScope
from ..workspace_contracts import _json, _project
from ..source_egress import validate_product_draft_source
from ..transaction_records import TransactionRecords
from .insights import resolve_insight


_LOGGER = logging.getLogger(__name__)


from backend.shared.memory_sidecars import (
    UsageUnavailable, UsageStorage, utc_now, timestamp, half_life_days,
    decayed_score, recall_weight,
)


class UsageService(UsageStorage):
    def __init__(self, records, *, now=utc_now, source_owner=None):
        super().__init__(records, now=now)
        self.source_owner = records if source_owner is None else source_owner

    def _object(self, reader, kind, identity, project):
        if kind == 'inspiration':
            row = reader.read('recognition_experiences', identity)
            if (row is None or row.payload.get('scope') != {'user_id': 'local-user', 'project_id': project}
                    or row.payload.get('state') != 'active'):
                raise UsageUnavailable('usage_object_unavailable')
            return 'inspiration', row
        if kind == 'insight':
            row = resolve_insight(reader, WorkScope('local-user', project), identity)
            if row is None:
                raise UsageUnavailable('usage_object_unavailable')
            if row.payload.get('state') == 'pending':
                return None, None
            if row.payload.get('state') != 'active':
                raise UsageUnavailable('usage_object_unavailable')
            return 'insight', row
        return super()._object(reader, kind, identity, project)

    def _after_usage(self, canonical, target, project_id, *, count, initialize):
        if canonical == "insight" and count and not initialize:
            from .links import InsightLinks
            from ..source_egress import recognition_service
            try:
                service = recognition_service(self.records, product_draft_validator=validate_product_draft_source,
                    **({'source_owner': self.source_owner} if isinstance(self.records, TransactionRecords) else {}))
                InsightLinks(self.records, service, source_owner=self.source_owner).spread(project_id, target.object_id, self)
            except Exception as error:
                _LOGGER.warning("usage_spread_failed exception_type=%s", type(error).__name__)


def read_usage_count(records, kind, identity, project):
    if kind not in {'insight', 'document', 'inspiration'}:
        raise ValueError('invalid_usage_kind')
    row = records.read('v2_usage_' + kind, identity)
    if row is None or row.payload.get('project_id') != project:
        return None
    value = row.payload
    count, older, events = value.get('count'), value.get('older_count'), value.get('events')
    maximum = 9007199254740991
    if (type(count) is not int or not 1 <= count <= maximum or type(older) is not int
            or not 0 <= older <= maximum or not isinstance(events, list) or not 1 <= len(events) <= 64
            or older and len(events) != 64):
        return None
    counted = {'use', 'reset', 'open', 'citation'}
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or set(event) != {'at', 'kind'}
                or event.get('kind') not in counted | {'initialize', 'spread'}
                or timestamp(event.get('at'), None) is None
                or event['kind'] == 'initialize' and (index != 0 or older)):
            return None
    recorded = sum(event['kind'] in counted for event in events)
    # 原写入者的 count 包含固定确认基数；传播只改变分值，历史裁剪仍保留真实累计。
    uses = count - 1
    return uses if recorded <= uses <= recorded + older else None


def safe_record_usage(records, kind, identity, project, weight, **options):
    try:
        return UsageService(records).record_usage(kind, identity, project, weight, **options)
    except Exception as error:
        # Storage exceptions can contain private data; never log their messages.
        _LOGGER.warning('usage_write_failed kind=%s exception_type=%s', kind, type(error).__name__)
        return None


def initialize_usage(records, kind, identity, project):
    try:
        return UsageService(records).initialize(kind, identity, project)
    except Exception as error:
        _LOGGER.warning('usage_write_failed kind=%s exception_type=%s', kind, type(error).__name__)
        return None


def record_answer_usage(records, chosen, citations, project, *, turn_id=None, thread_id=None):
    """One strongest event per object, after the answer receipt has committed."""
    cited = {citation['n'] for citation in citations}
    events = {}
    for number, candidate in enumerate(chosen, 1):
        if number not in cited:
            continue
        layer, entry = candidate['layer'], candidate['entry']
        if layer not in {'L3', 'L2', 'L1', 'inspiration'}:
            continue
        kind = 'inspiration' if layer == 'inspiration' else 'insight' if layer == 'L3' else 'document'
        identity = entry['id'] if kind in {'insight', 'inspiration'} else entry['document_id']
        own_project = candidate.get('project_id', project)
        key = kind, identity, own_project
        events[key] = (1.0, entry.get('revision'))
    for (kind, identity, own_project), (weight, revision) in events.items():
        def recorded(tx, canonical, target, now):
            if (own_project == project and turn_id is not None and thread_id is not None
                    and type(revision) is int and revision > 0):
                from .citation_refs import append_citations
                if target.object_id != identity:
                    raise ValueError('citation_target_alias_changed')
                append_citations(tx, project=project, turn_id=turn_id, thread_id=thread_id,
                    use='answer', refs=[{'kind': canonical, 'id': identity, 'revision': revision}],
                    now=now.isoformat())
        safe_record_usage(records, kind, identity, own_project, weight, event_kind='citation', on_recorded=recorded)
        if kind == 'insight' and own_project == 'me' and project != 'me':
            # Global persona usage is stored in me; activation reaches this question's own project only.
            from .links import InsightLinks
            from ..source_egress import recognition_service
            try:
                InsightLinks(records, recognition_service(records, product_draft_validator=validate_product_draft_source)).spread(project, identity, UsageService(records), only_project=project)
            except Exception as error:
                _LOGGER.warning('usage_spread_failed exception_type=%s', type(error).__name__)


def install_usage_routes(application, *, records):
    application.state.document_record_usage = safe_record_usage
    router = APIRouter(prefix='/api/v2/usage')

    @router.post('/open', status_code=204)
    async def opened(request: Request):
        body = await _json(request)
        if (set(body) not in ({'project_id', 'kind', 'id'}, {'project_id', 'scope_kind', 'kind', 'id'})
                or not isinstance(body['kind'], str)
                or body['kind'] not in {'insight', 'document', 'summary', 'note'}):
            raise HTTPException(400, 'invalid_usage_fields')
        project, identity = _project(body['project_id']), _project(body['id'])
        from .turn_logical_scope import logical_scope
        try:
            scope_kind = logical_scope(body.get('scope_kind', 'personal'))
        except ValueError:
            raise HTTPException(400, 'invalid_scope_kind') from None
        try:
            factory = getattr(application.state, 'shared_query_factory', None)
            if scope_kind == 'shared' and factory is None:
                raise HTTPException(409, 'shared_scope_unavailable')
            query = (factory.incoming_query(application, project, scope_kind=scope_kind)
                if factory is not None else None)
            if query is not None and query.origin_admission is not None:
                from .shared_usage import SharedUsage
                SharedUsage(query).record(body['kind'], identity, event_kind='open')
            else:
                UsageService(records).record_usage(body['kind'], identity, project, 1.0, event_kind='open')
        except UsageUnavailable:
            raise HTTPException(404, 'usage_object_not_found') from None
        except RecognitionConflict:
            raise HTTPException(409, 'shared_usage_source_changed') from None
        except ValueError as error:
            from .shared_projects import SharedProjectError
            if isinstance(error, SharedProjectError):
                raise HTTPException(error.status, str(error)) from None
            raise
        return Response(status_code=204)

    application.include_router(router)
