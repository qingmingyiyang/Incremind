from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json

from backend.security.user_context import USER_ACCESS, authorize_user, user_context
from ..server_jobs import ServerJobScheduler, retain_until_finished
from ..server_audit import AdminAudit
from core.storage_provider.persistence_budget import persistence_budget
from .shared_projects import SharedProjectError


_CURRENT = ContextVar('shared_extraction_admission', default=None)


@contextmanager
def _owner_access(users, access):
    audit = AdminAudit(users)
    intent = (audit.intent(access, method='POST', path='/api/v2/server/shared-projects/extractions')
              if access.by == 'admin' and USER_ACCESS.get() != access else None)
    try:
        with user_context(access):
            yield
    except BaseException as error:
        audit.outcome(intent, status=getattr(error, 'status', 500))
        raise
    audit.outcome(intent, status=200)


def payload_bytes(payload, *, formatted=False):
    options = {'indent': 2} if formatted else {'separators': (',', ':')}
    return len((json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, **options)
                + ('\n' if formatted else '')).encode('utf-8'))


class ExtractionAdmission:
    def __init__(self, extraction, actor, destination, application, check):
        self.extraction, self.actor, self.destination = extraction, actor, destination
        self.application, self.check = application, check
        self.active = True

    def guard(self):
        extraction, destination = self.extraction, self.destination
        if not self.active or _CURRENT.get() is not self or extraction._admissions.get(id(self)) is not self:
            raise SharedProjectError('shared_extraction_admission_required', status=409)
        context = extraction.server_context
        child = context.pool._children.get(destination.owner_user_id)
        if (child is None or child.application is not self.application or child.active < 1 or child.retiring is not None
                or self.application.state.server_context is not context
                or self.application.state.server_user_id != destination.owner_user_id
                or self.application.state.recognition_records is not destination.records
                or self.application.state.recognition_documents.namespace_id != destination.namespace):
            raise SharedProjectError('shared_extraction_owner_changed', status=409)
        destination.validate()

    def reserve(self, size=0):
        self.guard()
        self.check(size)

    @contextmanager
    def phase(self, maximum_bytes):
        self.guard()
        cap = self.check.reserve_up_to(maximum_bytes)
        def consume(size):
            self.guard()
            cap.consume(size)
        try:
            with persistence_budget(root=self.destination.root,
                    databases=(self.destination.records.database_path,
                               self.destination.root / '.rebuild-data' / 'structured-records.sqlite3'),
                    consume=consume):
                yield cap
        finally:
            cap.close()


def current_admission(extraction, destination):
    admission = _CURRENT.get()
    if (not isinstance(admission, ExtractionAdmission) or admission.extraction is not extraction
            or admission.destination != destination):
        raise SharedProjectError('shared_extraction_admission_required', status=409)
    admission.guard()
    return admission


async def run_admitted(extraction, operation, actor, owner_user_id, *args, **kwargs):
    from .shared_extraction import PersonalDestination
    context = extraction.server_context
    if (context is None or context.users is not extraction.registry.users
            or not isinstance(context.jobs, ServerJobScheduler) or context.jobs.users is not context.users
            or context.pool is None or context.pool.users is not context.users):
        raise SharedProjectError('shared_extraction_admission_unavailable', status=503)
    if operation not in {'preview', 'adopt', 'reconcile'}:
        raise SharedProjectError('shared_extraction_operation_invalid')
    access = authorize_user(context.users, actor, owner_user_id)
    with _owner_access(context.users, access):
        context.jobs.check_intake(owner_user_id)
        async with context.pool.lease(owner_user_id) as application:
            destination = PersonalDestination.from_application(extraction.registry, actor, application,
                target_user_id=owner_user_id)
            async with context.jobs.intake(owner_user_id) as check:
                admission = ExtractionAdmission(extraction, actor, destination, application, check)
                extraction._admissions[id(admission)] = admission
                token = _CURRENT.set(admission)
                try:
                    admission.reserve()
                    # HTTP 断开保留真实 owner 与额度锁，直到实际文件及 SQLite 写入结束。
                    return await retain_until_finished(asyncio.to_thread(
                        getattr(extraction, operation), actor, destination, *args, **kwargs))
                finally:
                    admission.active = False
                    _CURRENT.reset(token)
                    del extraction._admissions[id(admission)]


class AdmittedSources:
    def __init__(self, source, admission):
        self.source, self.admission = source, admission

    def __getattr__(self, name):
        return getattr(self.source, name)

    def write(self, collection, identity, payload, **kwargs):
        # 每份源和索引投影使用独立有限额度；实际 JSON 正文、metadata 和 SQL 自动旁路逐项扣减。
        with self.admission.phase(16 * payload_bytes(payload, formatted=True) + 4096):
            return self.source.write(collection, identity, payload, **kwargs)
