from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from backend.recognition import RecognitionConflict
from backend.security.user_context import USER_ACCESS
from .server_users import enabled_user


DAILY_SUBJECT = 'shared-daily-maintenance'
_CURRENT = ContextVar('shared_daily_owner', default=None)
_WRITING = ContextVar('shared_daily_publication', default=None)


def _job_subject(registry, application, owner):
    from ..server_runtime import _SERVER_JOB_SUBJECT

    state = application.state
    context, subject = getattr(state, 'server_context', None), _SERVER_JOB_SUBJECT.get()
    if (USER_ACCESS.get() is not None or context is None or context.shared_projects is not registry
            or state.server_user_id != owner or subject is None or subject[0] is not context.pool
            or subject[2] != owner or subject[3] != state.recognition_runtime_root
            or context.jobs.active_for(owner) < 1
            or not context.pool.model_balance_job_current(subject[1], owner, subject[3])):
        raise RecognitionConflict('shared_daily_execution_owner_changed')
    return subject


def _ownership(reader, project_id, owner):
    from .shared_projects import MEMBERS, PROJECTS, SharedProjectError, member_id

    project = reader.read(PROJECTS, project_id)
    member = reader.read(MEMBERS, member_id(project_id, owner))
    account = enabled_user(reader, owner)
    if (project is None or project.payload.get('state') != 'active'
            or project.payload.get('owner_user_id') != owner or member is None
            or member.payload.get('role') != 'owner' or member.payload.get('left_at') is not None
            or account is None):
        raise SharedProjectError('shared_daily_owner_changed', status=409)
    return project.revision, member.revision, account.revision


@dataclass(frozen=True, eq=False)
class SharedDailyActor:
    registry: object
    application: object
    project_id: str
    user_id: str
    revisions: tuple[int, int, int]
    _subject: tuple
    # 复用来源证明的标识槽位，设备认证永远不接纳此后台主体。
    device_id = DAILY_SUBJECT

    @property
    def revision(self):
        return self.revisions[0]

    def require_application(self, registry, application, owner):
        if (_CURRENT.get() is not self or registry is not self.registry or application is not self.application
                or owner != self.user_id or _job_subject(registry, application, owner) != self._subject):
            raise RecognitionConflict('shared_daily_execution_owner_changed')

    def authorize(self, registry, reader, project_id, *, action, authority):
        from .shared_projects import SharedAccess, SharedProjectError

        self.require_application(registry, self.application, self.user_id)
        if (project_id != self.project_id or action not in {'read', 'write'}
                or action == 'write' and _WRITING.get() is not self):
            raise SharedProjectError('shared_daily_scope_forbidden', status=403)
        if _ownership(reader, project_id, self.user_id) != self.revisions:
            raise SharedProjectError('shared_daily_owner_changed', status=409)
        access = SharedAccess(project_id, self.user_id, 'owner', *self.revisions, 'auto')
        if authority is not None and access.authority != authority:
            raise SharedProjectError('shared_authority_changed', status=409)
        return access

    @contextmanager
    def publication(self):
        self.require_application(self.registry, self.application, self.user_id)
        token = _WRITING.set(self)
        try:
            yield
        finally:
            _WRITING.reset(token)

    def destination(self, registry, application, owner):
        from .server_deletion import _directory
        from .shared_extraction import PersonalDestination

        self.require_application(registry, application, owner)
        state = application.state
        root = Path(state.recognition_runtime_root).absolute()
        observed = _directory(root)
        destination = PersonalDestination(root, state.recognition_records, state.recognition_documents,
            state.workspace_domains, (observed.st_dev, observed.st_ino), state.recognition_documents.namespace_id, owner)
        destination.validate_application(registry, application)
        return destination


@contextmanager
def daily_owner(registry, application, project_id, owner):
    if _CURRENT.get() is not None:
        raise RecognitionConflict('shared_daily_execution_owner_changed')
    subject = _job_subject(registry, application, owner)
    with registry.records.begin_read() as reader:
        revisions = _ownership(reader, project_id, owner)
    actor = SharedDailyActor(registry, application, project_id, owner, revisions, subject)
    token = _CURRENT.set(actor)
    try:
        yield actor
    finally:
        _CURRENT.reset(token)


def actor_for_shared_lease(registry, devices, application, lease, request):
    if lease.device_id == DAILY_SUBJECT:
        actor = _CURRENT.get()
        if (actor is None or request.get('desired_outcome') != 'memory.consolidate'
                or request.get('scope', {}).get('project_id') != lease.project_id
                or actor.project_id != lease.project_id or actor.user_id != lease.actor_user_id
                or actor.revision != lease.device_revision
                or actor.revisions != (lease.project_revision, lease.member_revision, lease.account_revision)):
            raise RecognitionConflict('shared_daily_execution_owner_changed')
        actor.require_application(registry, application, lease.actor_user_id)
        return actor
    actor = devices.identity_for_device(lease.device_id, lease.actor_user_id)
    if actor is None or actor.revision != lease.device_revision:
        raise RecognitionConflict('material_origin_identity_changed')
    return actor
