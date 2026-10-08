"""Best-effort activity sidecars and Beijing calendar-week counts."""
from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from fastapi import APIRouter

from ..workspace_contracts import _project
from .usage import utc_now

_LOGGER = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')


from backend.shared.memory_sidecars import record_activity


def week_stats(records, project_id=None, *, now=utc_now):
    local = now().astimezone(BEIJING)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    result = {'week_start':start.isoformat(), 'remember':0, 'confirm':0, 'forget':0, 'forget_auto':0}
    for row in records.list('v2_activity'):
        event = row.payload
        if project_id is not None and event.get('project_id') != project_id:
            continue
        if event.get('kind') not in ('remember', 'confirm', 'forget'):
            continue
        if event.get('by') == 'auto':
            if event['kind'] != 'forget':
                continue
            bucket = 'forget_auto'
        else:
            bucket = event['kind']
        try:
            # T10.6 already persists created_at; do not migrate its event payloads.
            at = datetime.fromisoformat((event.get('at') or event.get('created_at')).replace('Z', '+00:00'))
            if at.tzinfo is not None and start <= at < end:
                result[bucket] += 1
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
    return result


def install_stats_routes(application, *, records, now=utc_now):
    application.state.document_record_activity = record_activity
    router = APIRouter(prefix='/api/v2/stats')

    @router.get('/week')
    def week(project_id: str | None = None):
        return week_stats(records, _project(project_id) if project_id is not None else None, now=now)

    application.include_router(router)
