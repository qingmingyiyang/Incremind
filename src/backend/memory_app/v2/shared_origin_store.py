from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
from threading import get_ident

from backend.recognition import RecognitionConflict
from backend.security.user_context import USER_ACCESS, user_action_allowed
from core.ai_kernel.agent_contracts import (
    agent_budget_reservation_to_payload, agent_child_link_to_payload, agent_run_from_payload, agent_run_to_payload,
)
from core.ai_kernel.product_admission import validate_bundle
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from ..kernel.agent_coordinator import AgentCoordinatorError, _CHILD_TURN_ACCEPTANCE
from .material_origin_admission import read_origin_item
from .material_origins import ORIGIN_KIND, SharedProjectLease
from .server_deletion import _directory
from .shared_runtime import current_shared_read_capability
from .shared_extraction import PersonalDestination
from .server_users import current_caller, enabled_user


_CLAIM_ADMISSION = ContextVar('shared_origin_claim_admission', default=None)
_CHILD_COMMIT = ContextVar('shared_origin_child_commit', default=None)


class SharedOriginTurnStore(SQLiteAITurnStore):
    def __init__(self, path, *, application, registry, devices, **options):
        self.application, self.registry, self.devices = application, registry, devices
        state = application.state
        self._owner = state.server_user_id
        self._root = Path(state.recognition_runtime_root).absolute()
        self._records, self._documents = state.recognition_records, state.recognition_documents
        self._workspace = state.workspace_domains
        self._models = state.recognition_models
        self._namespace = self._documents.namespace_id
        observed = _directory(self._root)
        self._root_identity = observed.st_dev, observed.st_ino
        self._destination = PersonalDestination(self._root, self._records, self._documents, self._workspace,
            self._root_identity, self._namespace, self._owner)
        if (not isinstance(self._owner, str) or not self._owner
                or self._root != registry.users.server_root / 'users' / self._owner
                or Path(path) != self._root / '.rebuild-data' / 'ai-turns.sqlite3'
                or not Path(self._records.database_path).is_relative_to(self._root)):
            raise RecognitionConflict('material_origin_turn_owner_changed')
        super().__init__(path, **options)

    def _personal(self):
        self._destination.validate_application(self.registry, self.application)
        state = self.application.state
        observed = _directory(self._root)
        if (state.server_user_id != self._owner or state.recognition_runtime_root != self._root
                or state.recognition_records is not self._records or state.recognition_documents is not self._documents
                or state.workspace_domains is not self._workspace or self._documents.namespace_id != self._namespace
                or state.recognition_models is not self._models or self._workspace.query.models is not self._models
                or (observed.st_dev, observed.st_ino) != self._root_identity
                or state.ai_turn_store is not self):
            raise RecognitionConflict('material_origin_turn_owner_changed')
        return {'owner_user_id': self._owner, 'namespace_id': self._namespace,
            'namespace': '/'.join(('personal', self._owner, self._namespace)),
            'root_identity': list(self._root_identity)}

    @contextmanager
    def admission_bundle(self, request, binding):
        validate_bundle(request, binding)
        with super().admission_bundle(request, binding):
            registration = {'store': self, 'request': deepcopy(request), 'bundle': deepcopy(binding),
                'thread': get_ident(), 'active': True}
            token = _CLAIM_ADMISSION.set(registration)
            try:
                yield
            finally:
                registration['active'] = False
                _CLAIM_ADMISSION.reset(token)

    def claim_turn(self, request):
        registration = _CLAIM_ADMISSION.get()
        if registration is not None and (registration['store'] is not self or not registration['active']
                or registration['thread'] != get_ident() or registration['request'] != request):
            raise RecognitionConflict('material_origin_admission_expired')
        saved = self.get_request(request['turn_id'])
        bundle = (self.restore_admission(request) if saved is not None
                  else registration['bundle'] if registration is not None else None)
        items = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
        markers = [ref for ref in request['input']['refs'] if isinstance(ref.get('uri'), str)
            and ref['uri'].startswith('crp://material-origin/')]
        origin_request = request
        pending = _CHILD_TURN_ACCEPTANCE.get()
        host_child = pending is not None and pending['request'] == request
        binding = request.get('agent_binding', {})
        if saved is None and (binding.get('role') == 'subagent' or 'parent_run_id' in binding) and not host_child:
            raise RecognitionConflict('material_origin_binding_missing')
        stored_child = saved == request and bool(saved.get('agent_binding', {}).get('parent_run_id'))
        if bundle is None and registration is None and (markers or host_child or stored_child):
            origin_request, items = self._shared_child_origin(request, saved, markers=markers, pending=pending)
        if not markers and not items:
            return super().claim_turn(request)
        if not markers or len(items) != 1 or origin_request is not request and markers != [items[0].ref]:
            raise RecognitionConflict('material_origin_binding_missing')
        payload = items[0].payload
        if not isinstance(payload, dict) or not isinstance(payload.get('shared'), dict):
            raise RecognitionConflict('material_origin_binding_invalid')
        lease = SharedProjectLease.from_payload(payload['shared'].get('lease'))
        personal = self._personal()
        read_lease, auxiliary = read_origin_item(origin_request, items[0], actor_user_id=lease.actor_user_id, personal=personal)
        if lease != read_lease:
            raise RecognitionConflict('material_origin_binding_changed')
        if origin_request is not request and auxiliary is not None:
            raise RecognitionConflict('material_origin_binding_changed')
        child_commit = pending if origin_request is not request and saved is None else None
        access = USER_ACCESS.get()
        if access is not None and (access.target_user_id != self._owner
                or access.caller.user_id != lease.actor_user_id or access.caller.device_id != lease.device_id
                or access.caller.revision != lease.device_revision):
            raise RecognitionConflict('material_origin_execution_caller_changed')
        capability = current_shared_read_capability(registry=self.registry)
        if capability is not None:
            if capability.actor.user_id != lease.actor_user_id or capability.lease_payload() != lease.payload():
                raise RecognitionConflict('material_origin_binding_changed')
            self._authorize_owner(capability._writer._central, capability.actor)
            self._personal()
            return self._claim_under_origin(request, child_commit)
        actor = self.devices.identity_for_device(lease.device_id, lease.actor_user_id)
        if actor is None or actor.revision != lease.device_revision:
            raise RecognitionConflict('material_origin_identity_changed')
        authority = {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
            'account_revision': lease.account_revision}
        # 接纳仅短持中央资格及共享根，网络执行继续由原运行时管理。
        with self.registry.records.begin() as central:
            self.registry.authorize(central, actor, lease.project_id, authority=authority)
            self._authorize_owner(central, actor)
            with self.registry.roots.lease(lease.project_id):
                data = self.registry.roots._data(lease.project_id)
                if data.namespace != lease.data_namespace or tuple(data.root_identity) != lease.root_identity:
                    raise RecognitionConflict('material_origin_root_changed')
                self._personal()
                return self._claim_under_origin(request, child_commit)

    def _claim_under_origin(self, request, child_commit):
        if child_commit is None:
            return super().claim_turn(request)
        token = _CHILD_COMMIT.set((self, child_commit))
        try:
            return super().claim_turn(request)
        finally:
            _CHILD_COMMIT.reset(token)

    def _commit_product_admission(self, connection, registration, turn_id):
        current = _CHILD_COMMIT.get()
        if current is not None:
            store, pending = current
            if (store is not self or pending is not _CHILD_TURN_ACCEPTANCE.get() or not pending['active']
                    or pending['thread'] != get_ident() or pending['request']['turn_id'] != turn_id):
                raise RecognitionConflict('material_origin_child_acceptance_changed')
            parent, child, link, reservation = (pending[key] for key in ('parent', 'child', 'link', 'reservation'))
            checks = (
                ('SELECT turn_id,project_id,parent_run_id,payload_json FROM ai_agent_runs WHERE run_id=?', child.run_id,
                    (turn_id, child.project_id, parent.run_id, agent_run_to_payload(child))),
                ('SELECT parent_run_id,child_run_id,project_id,status,payload_json FROM ai_agent_child_links WHERE link_id=?',
                    link.link_id, (parent.run_id, child.run_id, child.project_id, 'reserved', agent_child_link_to_payload(link))),
                ('SELECT parent_run_id,child_run_id,project_id,status,payload_json FROM ai_agent_budget_reservations WHERE reservation_id=?',
                    reservation.reservation_id, (parent.run_id, child.run_id, child.project_id, 'reserved',
                        agent_budget_reservation_to_payload(reservation))),
            )
            # 冷启动恢复也能中止未接纳子轮，必须在实际原生写入事务提交前复验保留状态。
            for statement, identity, expected in checks:
                row = connection.execute(statement, (identity,)).fetchone()
                if row is None or (*row[:-1], json.loads(row[-1])) != expected:
                    raise RecognitionConflict('material_origin_child_acceptance_changed')
            saved = connection.execute('SELECT turn_id,project_id,parent_run_id,payload_json FROM ai_agent_runs WHERE run_id=?',
                (parent.run_id,)).fetchone()
            if saved is None or saved[:-1] != (parent.turn_id, parent.project_id, parent.parent_run_id):
                raise RecognitionConflict('material_origin_child_acceptance_changed')
            actual_parent = agent_run_from_payload(json.loads(saved[-1]))
            if (actual_parent.run_id != parent.run_id or actual_parent.turn_id != parent.turn_id
                    or actual_parent.cancel_epoch != parent.cancel_epoch or actual_parent.is_terminal):
                raise RecognitionConflict('material_origin_child_acceptance_changed')
        return super()._commit_product_admission(connection, registration, turn_id)

    def _shared_child_origin(self, request, saved, *, markers, pending):
        binding = request.get('agent_binding', {})
        project = request['scope']['project_id']
        composition = getattr(self.application.state, 'agent_runtime_composition', None)
        if (composition is None or not binding.get('parent_run_id') or binding.get('role') != 'subagent'
                or saved is not None and saved != request):
            raise RecognitionConflict('material_origin_binding_missing')
        coordinator, agents = composition.coordinator, composition.store
        if coordinator._store is not agents or Path(agents._path) != self._path:
            raise RecognitionConflict('material_origin_turn_owner_changed')
        if pending is not None and (pending['coordinator'] is not coordinator or pending['thread'] != get_ident()
                or not pending['active'] or pending['request'] != request):
            raise RecognitionConflict('material_origin_child_acceptance_changed')
        if pending is None and saved is None:
            raise RecognitionConflict('material_origin_binding_missing')
        found = agents.get_run_by_turn_id(request['turn_id'], project_id=project)
        if found is None or found[0].parent_run_id != binding['parent_run_id']:
            raise RecognitionConflict('material_origin_child_ancestry_changed')
        root, run, ancestry, seen = request, found[0], [], set()
        while run.parent_run_id is not None:
            if run.parent_run_id in seen or len(ancestry) >= 8:
                raise RecognitionConflict('material_origin_child_ancestry_changed')
            seen.add(run.parent_run_id)
            run = agents.get_run(run.parent_run_id)
            previous = self.get_request(run.turn_id) if run is not None else None
            if (previous is None or run.project_id != project or previous['scope'] != root['scope']
                    or previous['privacy'] != root['privacy'] or previous.get('agent_binding', {}).get('run_id') != run.run_id
                    or any(ref not in previous['input']['refs'] for ref in root['input']['refs'])):
                raise RecognitionConflict('material_origin_child_ancestry_changed')
            ancestry.append(previous)
            root = previous
        original = self.restore_admission(root)
        items = [] if original is None else [item for item in original.items if item.kind == ORIGIN_KIND]
        root_marked = any(ref['uri'].startswith('crp://material-origin/') for ref in root['input']['refs'])
        if not markers and not items and not root_marked:
            return request, []
        if (len(items) != 1 or markers != [items[0].ref]
                or request['desired_outcome'] not in {'project.task', 'agent.steward.plan'}):
            raise RecognitionConflict('material_origin_binding_missing')
        try:
            if pending is not None:
                parent, child, link, reservation = (pending[key] for key in ('parent', 'child', 'link', 'reservation'))
                actual_parent = agents.get_run(parent.run_id)
                child_binding = {'run_id': child.run_id, 'parent_run_id': parent.run_id, 'link_id': link.link_id,
                    'reservation_id': reservation.reservation_id, 'spawn_operation_id': link.spawn_operation_id}
                if (actual_parent is None or actual_parent.turn_id != parent.turn_id or actual_parent.is_terminal
                        or actual_parent.cancel_epoch != parent.cancel_epoch or actual_parent.project_id != project
                        or agents.get_run(child.run_id) != child or child.turn_id != request['turn_id']
                        or child.parent_run_id != parent.run_id
                        or any(binding.get(key) != value for key, value in child_binding.items())
                        or link.status != 'reserved' or reservation.status != 'reserved'
                        or agents.get_child_link(link.link_id, project_id=project) != link
                        or agents.get_reservation(reservation.reservation_id, project_id=project) != reservation):
                    # 已最终登记的原子轮可以沿其已持久绑定重放。
                    if saved is None:
                        raise RecognitionConflict('material_origin_child_acceptance_changed')
                    coordinator.verify_agent_binding(request, binding)
            else:
                coordinator.verify_agent_binding(request, binding)
            for previous in ancestry:
                coordinator.verify_agent_binding(previous, previous['agent_binding'])
            first = tuple(self.events_after(root['turn_id']))[:1]
            if (root['desired_outcome'] != 'project.task' or root.get('agent_binding', {}).get('role') != 'main'
                    or not first or first[0]['type'] != 'turn.accepted' or first[0]['sequence'] != 1):
                raise RecognitionConflict('material_origin_child_ancestry_changed')
            return root, items
        except AgentCoordinatorError as error:
            raise RecognitionConflict('material_origin_child_ancestry_changed') from error

    def _authorize_owner(self, central, actor):
        caller = current_caller(central, actor)
        if (not user_action_allowed(caller.payload['role'], actor.user_id, self._owner, 'space')
                or enabled_user(central, self._owner) is None):
            raise RecognitionConflict('material_origin_execution_owner_changed')


def shared_origin_store_factory(application, registry, devices):
    def create(path, **options):
        return SharedOriginTurnStore(path, application=application, registry=registry, devices=devices, **options)
    return create
