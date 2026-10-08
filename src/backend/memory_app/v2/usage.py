"""Revision-independent usage of published insights and their documents."""
from datetime import datetime, timezone
import logging
import math

from fastapi import APIRouter, HTTPException, Request, Response

from backend.recognition import WorkScope
from ..workspace_contracts import _json, _project
from ..source_egress import validate_product_draft_source
from .insights import resolve_insight


_LOGGER = logging.getLogger(__name__)


from backend.shared.memory_sidecars import (
    UsageUnavailable, UsageStorage, utc_now, timestamp, half_life_days,
    decayed_score, recall_weight,
)


class UsageService(UsageStorage):
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
                InsightLinks(self.records, recognition_service(self.records, product_draft_validator=validate_product_draft_source)).spread(project_id, target.object_id, self)
            except Exception as error:
                _LOGGER.warning("usage_spread_failed exception_type=%s", type(error).__name__)


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


def record_answer_usage(records, chosen, citations, project):
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
        events[key] = 1.0
    for (kind, identity, own_project), weight in events.items():
        safe_record_usage(records, kind, identity, own_project, weight, event_kind='citation')
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
        if (set(body) != {'project_id', 'kind', 'id'} or not isinstance(body['kind'], str)
                or body['kind'] not in {'insight', 'document', 'summary', 'note'}):
            raise HTTPException(400, 'invalid_usage_fields')
        project, identity = _project(body['project_id']), _project(body['id'])
        try:
            UsageService(records).record_usage(body['kind'], identity, project, 1.0, event_kind='open')
        except UsageUnavailable:
            raise HTTPException(404, 'usage_object_not_found') from None
        return Response(status_code=204)

    application.include_router(router)
