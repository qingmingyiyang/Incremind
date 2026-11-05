from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime, timezone
import re
from uuid import uuid4

from .server_users import current_caller, enabled_user

PROJECTS = 'shared_projects'
MEMBERS = 'shared_members'
INVITES = 'shared_invites'
OPERATIONS = 'shared_project_operations'
CANCELLED = 'shared_project_cancelled_operations'
ACTIVITY = 'v2_shared_activity'
ATTRIBUTION = 'v2_shared_writes'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_ROLES = {'owner', 'member', 'viewer'}


class SharedProjectError(ValueError):
    def __init__(self, code, *, status=400, operation_id=None):
        super().__init__(code)
        self.status, self.operation_id = status, operation_id


def segment(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SharedProjectError('shared_identity_invalid')
    return value


def name(value):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 80
            or any(ord(char) < 32 for char in value)):
        raise SharedProjectError('shared_name_invalid')
    return value.strip()


def revision(value):
    if type(value) is not int or value < 1:
        raise SharedProjectError('shared_revision_invalid')
    return value


def member_id(project_id, user_id):
    return segment(segment(project_id) + '--' + segment(user_id))


def view(row):
    return {**row.payload, 'revision': row.revision}


@dataclass(frozen=True)
class SharedAccess:
    project_id: str
    actor_user_id: str
    role: str
    project_revision: int
    member_revision: int
    account_revision: int
    by: str | None

    @property
    def authority(self):
        return {'project_revision': self.project_revision, 'member_revision': self.member_revision,
                'account_revision': self.account_revision}


class SharedProjects:
    def __init__(self, users, roots, *, clock=None):
        self.users, self.records, self.roots = users, users.records, roots
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.storage = None

    def now(self):
        return self.clock().astimezone(timezone.utc).isoformat()

    def authorize(self, tx, actor, project_id, *, action='read', authority=None, states=('active',)):
        caller = current_caller(tx, actor)
        project = tx.read(PROJECTS, segment(project_id))
        if project is None or project.payload['state'] not in states:
            raise SharedProjectError('shared_project_unavailable', status=404)
        member = tx.read(MEMBERS, member_id(project_id, actor.user_id))
        role = (member.payload['role'] if member and member.payload['left_at'] is None else None)
        admin = caller.payload['role'] == 'admin'
        if (not admin and (role not in _ROLES or action == 'write' and role == 'viewer'
                           or action == 'manage' and role != 'owner')):
            raise SharedProjectError('shared_forbidden', status=403)
        if action not in {'read', 'write', 'manage'}:
            raise SharedProjectError('shared_action_invalid')
        access = SharedAccess(project_id, actor.user_id, 'admin' if admin else role,
                              project.revision, member.revision if member else 0, caller.revision,
                              'admin' if admin and role != 'owner' else None)
        if authority is not None and authority != access.authority:
            raise SharedProjectError('shared_authority_changed', status=409)
        return access

    def project_view(self, tx, row, actor):
        access = self.authorize(tx, actor, row.object_id, states=('active', 'provisioning', 'deleting'))
        owner = tx.read('server_users', row.payload['owner_user_id'])
        return {**view(row), 'shared': True, 'role': access.role, 'authority': access.authority,
                'actor_user_id': actor.user_id,
                'owner_name': owner.payload['name'] if owner else None,
                'can_write': access.role in {'admin', 'owner', 'member'},
                'can_manage': access.role in {'admin', 'owner'}}

    def list(self, actor):
        with self.records.begin_read() as tx:
            caller = current_caller(tx, actor)
            memberships = {row.payload['project_id'] for row in tx.list(MEMBERS)
                           if row.payload['user_id'] == actor.user_id and row.payload['left_at'] is None}
            return [self.project_view(tx, row, actor) for row in tx.list(PROJECTS)
                    if row.payload['state'] != 'deleted'
                    and (caller.payload['role'] == 'admin' or row.object_id in memberships)]

    def directory(self, actor):
        with self.records.begin_read() as tx:
            current_caller(tx, actor)
            return [{'user_id': row.object_id, 'name': row.payload['name']}
                    for row in tx.list('server_users') if enabled_user(tx, row.object_id)]

    def detail(self, actor, project_id):
        with self.records.begin_read() as tx:
            self.authorize(tx, actor, project_id, states=('active', 'provisioning', 'deleting'))
            project = self.project_view(tx, tx.read(PROJECTS, project_id), actor)
            members = []
            for row in tx.list(MEMBERS):
                if row.payload['project_id'] != project_id or row.payload['left_at'] is not None:
                    continue
                user = tx.read('server_users', row.payload['user_id'])
                members.append({**view(row), 'name': user.payload['name'] if user else None})
            invites = [view(row) for row in tx.list(INVITES)
                       if row.payload['project_id'] == project_id and row.payload['state'] == 'pending'
                       and project['can_manage']]
            return {'project': project, 'members': members, 'invitations': invites}

    def invitations(self, actor):
        with self.records.begin_read() as tx:
            current_caller(tx, actor)
            return [view(row) for row in tx.list(INVITES)
                    if row.payload['to_user_id'] == actor.user_id and row.payload['state'] == 'pending'
                    and (project := tx.read(PROJECTS, row.payload['project_id'])) is not None
                    and project.payload['state'] == 'active']

    def _operation(self, tx, actor, operation_id, action, project_id, inputs):
        operation_id = segment(operation_id)
        if tx.read(CANCELLED, member_id(operation_id, actor.user_id)) is not None:
            raise SharedProjectError('shared_operation_cancelled', status=409)
        existing = tx.read(OPERATIONS, operation_id)
        payload = {'operation_id': operation_id, 'actor_user_id': actor.user_id,
                   'project_id': project_id, 'action': action, 'inputs': inputs}
        if existing:
            if any(existing.payload[key] != value for key, value in payload.items()):
                raise SharedProjectError('shared_operation_conflict', status=409)
            return existing, True
        row = tx.put(OPERATIONS, operation_id, {**payload, 'state': 'pending', 'created_at': self.now()},
                     expected_revision=0)
        return row, False

    def _activity(self, tx, operation, access, *, object_kind, object_id, object_title):
        payload = {'id': operation.object_id, 'at': self.now(), 'project_id': access.project_id,
                   'actor_user_id': access.actor_user_id, 'by': access.by,
                   'action': operation.payload['action'], 'object_kind': object_kind,
                   'object_id': object_id, 'object_title': object_title}
        tx.put(ACTIVITY, operation.object_id, payload, expected_revision=0)

    def finish(self, actor, operation_id):
        # 控制记录先提交，活动投影另行提交；pending 状态保留可恢复证据。
        with self.records.begin_read() as tx:
            current_caller(tx, actor)
            operation = tx.read(OPERATIONS, segment(operation_id))
            if operation is None:
                raise SharedProjectError('shared_operation_unavailable', status=404)
            project_id = operation.payload['project_id']
            self.authorize(tx, actor, project_id,
                action='read' if operation.payload['actor_user_id'] == actor.user_id else 'manage',
                states=('active', 'provisioning', 'deleting', 'deleted'))
            if operation.payload['state'] == 'completed':
                return view(operation)
        if operation.payload['action'] == 'create':
            self.roots.provision(project_id, operation.payload['inputs']['name'])
        if operation.payload['action'] == 'delete':
            self.roots.delete(project_id, operation.payload['inputs']['root_identity'])
        with self.records.begin() as tx:
            self.authorize(tx, actor, project_id,
                           action='read' if operation.payload['actor_user_id'] == actor.user_id else 'manage',
                           states=('active', 'provisioning', 'deleting'))
            current = tx.read(OPERATIONS, operation_id)
            if current.payload['state'] == 'completed':
                return view(current)
            activity = tx.read(ACTIVITY, operation_id)
            if activity is not None and operation.payload['action'] != 'delete':
                self.roots.project_activity(project_id, activity.payload)
            if operation.payload['action'] == 'create':
                project = tx.read(PROJECTS, project_id)
                tx.put(PROJECTS, project_id, {**project.payload, 'state': 'active'}, expected_revision=project.revision)
            if operation.payload['action'] == 'delete':
                project = tx.read(PROJECTS, project_id)
                tx.put(PROJECTS, project_id, {**project.payload, 'state': 'deleted'}, expected_revision=project.revision)
            result = tx.put(OPERATIONS, operation_id, {**current.payload, 'state': 'completed',
                            'completed_at': self.now()}, expected_revision=current.revision)
            tx.commit()
            return view(result)

    def create(self, actor, *, project_name, operation_id):
        project_name = name(project_name)
        with self.records.begin() as tx:
            caller = current_caller(tx, actor)
            existing = tx.read(OPERATIONS, segment(operation_id))
            project_id = existing.payload['project_id'] if existing else 'shared-' + uuid4().hex
            operation, replay = self._operation(tx, actor, operation_id, 'create', project_id, {'name': project_name})
            if not replay:
                tx.put(PROJECTS, project_id, {'id': project_id, 'name': project_name, 'owner_user_id': actor.user_id,
                       'state': 'provisioning', 'created_at': self.now(), 'scenes': []}, expected_revision=0)
                tx.put(MEMBERS, member_id(project_id, actor.user_id), {'project_id': project_id,
                       'user_id': actor.user_id, 'role': 'owner', 'joined_at': self.now(), 'left_at': None}, expected_revision=0)
                access = SharedAccess(project_id, actor.user_id, 'owner', 1, 1, caller.revision, None)
                self._activity(tx, operation, access, object_kind='project', object_id=project_id, object_title=project_name)
            tx.commit()
        self.finish(actor, operation_id)
        return self.detail(actor, project_id)

    def invite(self, actor, project_id, *, user_id, role, authority, operation_id):
        if not isinstance(authority, dict):
            raise SharedProjectError('shared_authority_required')
        if role not in {'member', 'viewer'}:
            raise SharedProjectError('shared_role_invalid')
        with self.records.begin() as tx:
            access = self.authorize(tx, actor, project_id, action='manage', authority=authority)
            if enabled_user(tx, segment(user_id)) is None:
                raise SharedProjectError('shared_user_unavailable', status=404)
            operation, replay = self._operation(tx, actor, operation_id, 'invite', project_id,
                                               {'user_id': user_id, 'role': role})
            if not replay:
                member = tx.read(MEMBERS, member_id(project_id, user_id))
                if member and member.payload['left_at'] is None:
                    raise SharedProjectError('shared_already_member', status=409)
                if any(row.payload['project_id'] == project_id and row.payload['to_user_id'] == user_id
                       and row.payload['state'] == 'pending' for row in tx.list(INVITES)):
                    raise SharedProjectError('shared_invitation_pending', status=409)
                tx.put(INVITES, operation_id, {'id': operation_id, 'project_id': project_id,
                       'project_name': tx.read(PROJECTS, project_id).payload['name'], 'from_user_id': actor.user_id,
                       'to_user_id': user_id, 'role': role, 'state': 'pending', 'created_at': self.now()}, expected_revision=0)
                self._activity(tx, operation, access, object_kind='member', object_id=user_id,
                               object_title=tx.read('server_users', user_id).payload['name'])
            tx.commit()
        return self.finish(actor, operation_id)

    def accept(self, actor, invitation_id, *, expected_revision, operation_id):
        with self.records.begin() as tx:
            caller = current_caller(tx, actor)
            invitation = tx.read(INVITES, segment(invitation_id))
            if invitation is None or invitation.payload['to_user_id'] != actor.user_id:
                raise SharedProjectError('shared_invitation_unavailable', status=404)
            project_id = invitation.payload['project_id']
            operation, replay = self._operation(tx, actor, operation_id, 'accept', project_id,
                                               {'invitation_id': invitation_id, 'revision': revision(expected_revision)})
            if not replay:
                if invitation.revision != expected_revision or invitation.payload['state'] != 'pending':
                    raise SharedProjectError('shared_invitation_changed', status=409)
                project = tx.read(PROJECTS, project_id)
                if project is None or project.payload['state'] != 'active':
                    raise SharedProjectError('shared_project_unavailable', status=404)
                old = tx.read(MEMBERS, member_id(project_id, actor.user_id))
                if old and old.payload['left_at'] is None:
                    raise SharedProjectError('shared_already_member', status=409)
                saved = tx.put(MEMBERS, member_id(project_id, actor.user_id), {'project_id': project_id,
                       'user_id': actor.user_id, 'role': invitation.payload['role'], 'joined_at': self.now(),
                       'left_at': None}, expected_revision=old.revision if old else 0)
                tx.put(INVITES, invitation_id, {**invitation.payload, 'state': 'accepted'}, expected_revision=expected_revision)
                access = SharedAccess(project_id, actor.user_id, saved.payload['role'], project.revision,
                                      saved.revision, caller.revision, None)
                self._activity(tx, operation, access, object_kind='member', object_id=actor.user_id,
                               object_title=caller.payload['name'])
            tx.commit()
        return self.finish(actor, operation_id)

    def change_member(self, actor, project_id, *, user_id, action, role=None, expected_revision,
                      authority, operation_id):
        if not isinstance(authority, dict):
            raise SharedProjectError('shared_authority_required')
        if action not in {'role', 'remove', 'leave', 'transfer'} or action == 'role' and role not in {'member', 'viewer'}:
            raise SharedProjectError('shared_member_action_invalid')
        guard = (self.storage.transfer_guard(actor, project_id, user_id, authority, operation_id)
                 if action == 'transfer' and self.storage is not None else nullcontext(None))
        with guard as storage_guard, self.records.begin() as tx:
            access = self.authorize(tx, actor, project_id, action='read' if action == 'leave' else 'manage', authority=authority)
            operation, replay = self._operation(tx, actor, operation_id, action, project_id,
                {'user_id': user_id, 'role': role, 'revision': revision(expected_revision)})
            if not replay:
                member = tx.read(MEMBERS, member_id(project_id, user_id))
                if member is None or member.payload['left_at'] is not None:
                    raise SharedProjectError('shared_member_unavailable', status=404)
                if member.revision != expected_revision:
                    raise SharedProjectError('shared_member_changed', status=409)
                project = tx.read(PROJECTS, project_id)
                if action == 'leave' and user_id != actor.user_id:
                    raise SharedProjectError('shared_forbidden', status=403)
                if action != 'transfer' and member.payload['role'] == 'owner':
                    raise SharedProjectError('shared_owner_transfer_required', status=409)
                if action == 'transfer':
                    if user_id == project.payload['owner_user_id'] or enabled_user(tx, user_id) is None:
                        raise SharedProjectError('shared_transfer_invalid', status=409)
                    if self.storage is not None:
                        self.storage.check_transfer(tx, project, user_id, storage_guard)
                    old = tx.read(MEMBERS, member_id(project_id, project.payload['owner_user_id']))
                    tx.put(MEMBERS, old.object_id, {**old.payload, 'role': 'member'}, expected_revision=old.revision)
                    tx.put(PROJECTS, project_id, {**project.payload, 'owner_user_id': user_id}, expected_revision=project.revision)
                changes = {'role': 'owner' if action == 'transfer' else role} if action in {'role', 'transfer'} else {'left_at': self.now()}
                tx.put(MEMBERS, member.object_id, {**member.payload, **changes}, expected_revision=expected_revision)
                user = tx.read('server_users', user_id)
                self._activity(tx, operation, access, object_kind='member', object_id=user_id,
                               object_title=user.payload['name'] if user else user_id)
            tx.commit()
        # 离开后的活动投影由持久操作恢复，不重新授予已离开的成员读取资格。
        if action == 'leave' or action == 'remove' and user_id == actor.user_id:
            return {'operation_id': operation_id, 'state': 'pending', 'project_id': project_id}
        return self.finish(actor, operation_id)

    def pending(self, actor, project_id):
        with self.records.begin_read() as tx:
            self.authorize(tx, actor, project_id, action='manage', states=('active', 'provisioning', 'deleting'))
            return [view(row) for row in tx.list(OPERATIONS)
                    if row.payload['project_id'] == project_id and row.payload['state'] == 'pending']

    def operation(self, actor, operation_id):
        with self.records.begin_read() as tx:
            current_caller(tx, actor)
            row = tx.read(OPERATIONS, segment(operation_id))
            if row is None:
                cancelled = tx.read(CANCELLED, member_id(operation_id, actor.user_id))
                if cancelled is not None:
                    return view(cancelled)
                raise SharedProjectError('shared_operation_not_received', status=404)
            if row.payload['actor_user_id'] != actor.user_id:
                self.authorize(tx, actor, row.payload['project_id'], action='manage',
                               states=('active', 'provisioning', 'deleting', 'deleted'))
            return view(row)

    def cancel_unreceived(self, actor, operation_id, *, action, project_id, inputs):
        if (action not in {'create', 'invite', 'accept', 'role', 'remove', 'leave', 'transfer', 'delete'}
                or not isinstance(inputs, dict) or len(str(inputs)) > 8000):
            raise SharedProjectError('shared_command_invalid')
        if project_id is not None:
            segment(project_id)
        with self.records.begin() as tx:
            current_caller(tx, actor)
            existing = tx.read(OPERATIONS, segment(operation_id))
            if existing is not None:
                if existing.payload['actor_user_id'] != actor.user_id:
                    raise SharedProjectError('shared_forbidden', status=403)
                return view(existing)
            key = member_id(operation_id, actor.user_id)
            existing = tx.read(CANCELLED, key)
            if existing is not None:
                return view(existing)
            row = tx.put(CANCELLED, key, {'operation_id': operation_id, 'actor_user_id': actor.user_id,
                'project_id': project_id, 'action': action, 'inputs': inputs, 'state': 'cancelled', 'at': self.now()},
                expected_revision=0)
            tx.commit()
            return view(row)

    def delete(self, actor, project_id, *, confirmation, authority, operation_id, skip_backup):
        if not isinstance(authority, dict) or type(skip_backup) is not bool:
            raise SharedProjectError('shared_delete_fields_invalid')
        if not skip_backup:
            raise SharedProjectError('shared_backup_required', status=409)
        with self.records.begin_read() as tx:
            self.authorize(tx, actor, project_id, action='manage', authority=authority)
            project = tx.read(PROJECTS, project_id)
            if confirmation != project.payload['name']:
                raise SharedProjectError('shared_confirmation_invalid')
        root_identity = self.roots.inspect(project_id)
        with self.records.begin() as tx:
            access = self.authorize(tx, actor, project_id, action='manage', authority=authority)
            project = tx.read(PROJECTS, project_id)
            operation, _ = self._operation(tx, actor, operation_id, 'delete', project_id,
                {'root_identity': root_identity, 'confirmation': confirmation, 'skip_backup': True})
            tx.put(PROJECTS, project_id, {**project.payload, 'state': 'deleting'}, expected_revision=project.revision)
            self._activity(tx, operation, access, object_kind='project', object_id=project_id,
                           object_title=project.payload['name'])
            tx.commit()
        return self.finish(actor, operation_id)

    def activity(self, actor, project_id, *, actor_user_id=None):
        with self.records.begin_read() as tx:
            self.authorize(tx, actor, project_id)
            central = [view(row) for row in tx.list(ACTIVITY) if row.payload['project_id'] == project_id]
        local = self.roots.activity(project_id)
        with self.records.begin_read() as tx:
            self.authorize(tx, actor, project_id)
            result = {row['id']: row for row in (*local, *central)}
            return sorted((row for row in result.values() if actor_user_id is None or row['actor_user_id'] == actor_user_id),
                          key=lambda row: (row['at'], row['id']), reverse=True)
