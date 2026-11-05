from contextlib import contextmanager, ExitStack, nullcontext

from ..server_jobs import QuotaError
from .shared_projects import MEMBERS, OPERATIONS, PROJECTS, SharedProjectError, member_id, segment
from .server_users import enabled_user
from .server_deletion import _directory, _inspect_tree


class SharedStorage:
    def __init__(self, registry, jobs):
        if (jobs.users is not registry.users or jobs.related_storage_roots is not None
                or registry.storage is not None):
            raise ValueError('shared_storage_owner_invalid')
        self.registry, self.jobs = registry, jobs
        registry.storage = self
        jobs.related_storage_roots = self.related_roots

    @contextmanager
    def related_roots(self, user_id, *, reader=None, include_project=None):
        registry = self.registry
        central = registry.records.begin_read() if reader is None else nullcontext(reader)
        with central as tx, ExitStack() as stack:
            rows = [row for row in tx.list(PROJECTS)
                    if row.payload['state'] != 'deleted'
                    and (row.payload['owner_user_id'] == user_id or row.object_id == include_project)]
            retained = []
            for row in sorted(rows, key=lambda value: value.object_id):
                try:
                    root = registry.roots.path(row.object_id)
                except FileNotFoundError:
                    if row.payload['state'] == 'provisioning':
                        continue
                    raise SharedProjectError('shared_root_unavailable', status=409) from None
                stack.enter_context(registry.roots.lease(row.object_id))
                _inspect_tree(root)
                data = registry.roots._data(row.object_id)
                self._validate_data(row.object_id, root, data)
                retained.append((row.object_id, root, data))
            yield tuple(root for _, root, _ in retained)
            for project_id, root, data in retained:
                self._validate_data(project_id, root, data)

    def _validate_data(self, project_id, root, data):
        observed = _directory(self.registry.roots.path(project_id))
        binding = data.records.read('v2_shared_root', 'identity')
        if (data.root != root or (observed.st_dev, observed.st_ino) != data.root_identity
                or not isinstance(data.namespace, str) or not data.namespace
                or not data.records.database_path.resolve().is_relative_to(root / '.rebuild-data')
                or binding is None or binding.payload != {'project_id': project_id}):
            raise SharedProjectError('shared_root_binding_invalid', status=409)

    @contextmanager
    def transfer_guard(self, actor, project_id, target_user_id, authority, operation_id):
        project_id, target_user_id = segment(project_id), segment(target_user_id)
        with self.registry.records.begin_read() as tx:
            self.registry.authorize(tx, actor, project_id, action='manage', authority=authority)
            project = tx.read(PROJECTS, project_id)
            owner = project.payload['owner_user_id']
            received = tx.read(OPERATIONS, segment(operation_id)) is not None
            if not received:
                member = tx.read(MEMBERS, member_id(project_id, target_user_id))
                if member is None or member.payload['left_at'] is not None:
                    raise SharedProjectError('shared_member_unavailable', status=404)
                if target_user_id == owner or enabled_user(tx, target_user_id) is None:
                    raise SharedProjectError('shared_transfer_invalid', status=409)
        if received:
            # 原操作输入和操作者仍由 _operation 精确核对；重读已受理操作不重新转移容量。
            yield None
            return
        with self.jobs.storage_guard((owner, target_user_id)):
            yield (project.object_id, project.revision, owner, target_user_id)

    def check_transfer(self, tx, project, target_user_id, guard):
        if guard != (project.object_id, project.revision, project.payload['owner_user_id'], target_user_id):
            raise SharedProjectError('shared_authority_changed', status=409)
        user = tx.read('server_users', target_user_id)
        if (user is None or user.payload['disabled_at'] is not None
                or tx.read('server_user_deletions', target_user_id) is not None):
            raise QuotaError('intake_account_changed')
        # 同一中央事务与容量锁内，目标现有根和转入根一起取得租约、逐根只统计一次。
        with self.related_roots(target_user_id, reader=tx, include_project=project.object_id) as roots:
            total = self.jobs._storage_bytes(target_user_id, roots)
        limit = user.payload['storage_limit_mb']
        if limit is not None and total > limit * 1024 * 1024:
            raise QuotaError('storage_quota_exceeded')
