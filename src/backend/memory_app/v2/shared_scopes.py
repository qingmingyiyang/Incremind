import asyncio
import json

from backend.recognition import RecognitionConflict
from core.ai_kernel import validate_turn_request
from core.ai_kernel.product_admission import bundle_from_payload, validate_bundle
from ..kernel.receipt_projection import frozen_public_turn_requests
from ..kernel.route_source_snapshot import native_source_snapshot
from ..server_jobs import retain_until_finished
from ..transaction_records import TransactionRecords
from .material_origin_admission import read_origin_item
from .material_origins import ORIGIN_KIND
from .shared_extraction import PersonalDestination


def _prepared_task_without_native(destination, reader, row):
    if row.payload.get('intent') != 'do':
        return False
    execution = reader.read('v2_task_executions', row.object_id)
    if execution is None:
        return False
    values = execution.payload
    try:
        request = validate_turn_request(values['request'])
        snapshot = native_source_snapshot(destination.records, request['turn_id'], reader=reader)
        if snapshot['request'] is not None:
            return False
        project, thread = row.payload['project_id'], row.payload['thread_id']
        if (values.get('started') is not False or values.get('project_id') != project
                or values.get('task_text') != row.payload.get('user_text')
                or request['scope']['project_id'] != project or request['desired_outcome'] != 'project.task'
                or request['session_id'] != 'session-' + row.object_id
                or row.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != request['turn_id']
                or json.loads(request['input']['text']).get('task') != values['task_text']):
            raise ValueError('shared_scope_prepared_task_changed')
        bundle = bundle_from_payload(values['admission_bundle'])
        validate_bundle(request, bundle)
        expected = {'owner_user_id': destination.owner_user_id, 'namespace_id': destination.namespace,
            'origin_kind': 'personal', 'project_id': project, 'turn_id': request['turn_id']}
        if any(item.scope != expected for item in bundle.items):
            raise ValueError('shared_scope_prepared_owner_changed')
        identity = [item.payload for item in bundle.items if item.kind == 'workbench-steer-binding-v1']
        bound = {**expected, 'target_turn_id': row.object_id, 'thread_id': thread,
            'public_turn_id': row.payload.get('parent_turn_id') or row.object_id}
        if len(identity) != 1 or any(identity[0].get(key) != value for key, value in bound.items()):
            raise ValueError('shared_scope_prepared_identity_changed')
    except (ValueError, KeyError, TypeError) as error:
        raise RecognitionConflict('shared_scope_history_identity_changed') from error
    # 已保存的准备材料不提供已接纳历史证明，也不触发后台开始或旧租约恢复。
    return True


def personal_history_scopes(destination):
    records = destination.records
    destination.validate()
    result = {}
    with records.begin_read() as transaction:
        reader = TransactionRecords(transaction)
        threads = {row.object_id: row for row in reader.list('v2_threads')
            if isinstance(row.payload.get('project_id'), str)
            and row.payload['project_id'].startswith('shared-')}
        discovered = set()
        for row in reader.list('v2_turns'):
            thread = threads.get(row.payload.get('thread_id'))
            if (thread is None or thread.object_id in discovered
                    or row.payload.get('project_id') != thread.payload['project_id']
                    or row.payload.get('intent') not in {'ask', 'do', 'remember'}):
                continue
            if row.payload['intent'] == 'remember':
                from .shared_workbench_remember import read_shared_remember
                binding = read_shared_remember(reader, row, owner_user_id=destination.owner_user_id,
                    namespace_id=destination.namespace)
                if binding is not None:
                    found = result.setdefault(binding['project_id'], {})
                    found[thread.object_id] = {'id': thread.object_id, 'title': thread.payload['title'],
                        'updated_at': thread.payload['updated_at']}
                    discovered.add(thread.object_id)
                continue
            if _prepared_task_without_native(destination, reader, row):
                continue
            try:
                entries = frozen_public_turn_requests(records, destination.root, row.object_id, reader=reader)
            except (ValueError, KeyError, TypeError) as error:
                raise RecognitionConflict('shared_scope_history_identity_changed') from error
            for entry in entries:
                request = entry['native_request']
                snapshot = native_source_snapshot(records, request['turn_id'], reader=reader)
                bundle = snapshot['admission']
                selected = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
                if not selected:
                    continue
                if len(selected) != 1 or not entry['qualified'] or snapshot['request'] != request:
                    raise RecognitionConflict('shared_scope_history_identity_changed')
                personal = selected[0].payload.get('execution')
                logical = {'owner_user_id': destination.owner_user_id, 'namespace_id': destination.namespace,
                    'namespace': 'personal/' + destination.owner_user_id + '/' + destination.namespace}
                if (not isinstance(personal, dict) or set(personal) != {*logical, 'root_identity'}
                        or any(personal.get(key) != value for key, value in logical.items())):
                    raise RecognitionConflict('shared_scope_history_owner_changed')
                lease, auxiliary = read_origin_item(request, selected[0],
                    actor_user_id=selected[0].payload.get('actor_user_id'), personal=personal)
                if (auxiliary is not None or entry['identity'].get('owner_user_id') != destination.owner_user_id
                        or entry['identity'].get('namespace_id') != destination.namespace):
                    raise RecognitionConflict('shared_scope_history_identity_changed')
                # 个人旧正文按其原生归属发现；当前成员、设备和物理来源由新操作分别取得。
                project = request['scope']['project_id']
                bound_thread = threads.get(entry['identity']['thread_id'])
                if bound_thread is None or bound_thread.payload['project_id'] != project:
                    raise RecognitionConflict('shared_scope_history_identity_changed')
                found = result.setdefault(project, {})
                found[bound_thread.object_id] = {'id': bound_thread.object_id, 'title': bound_thread.payload['title'],
                    'updated_at': bound_thread.payload['updated_at']}
                discovered.add(bound_thread.object_id)
    destination.validate()
    return {project: sorted(threads.values(), key=lambda row: (row['updated_at'], row['id']), reverse=True)
        for project, threads in result.items()}


class SharedScopes:
    def __init__(self, registry, *, server_context):
        self.registry, self.context = registry, server_context

    async def list(self, actor, owner_user_id):
        async with self.context.pool.lease(owner_user_id) as application:
            destination = PersonalDestination.from_application(self.registry, actor, application,
                target_user_id=owner_user_id)
            return await retain_until_finished(asyncio.to_thread(self._list, actor, destination))

    def _list(self, actor, destination):
        history = personal_history_scopes(destination)
        current = {row['id']: row for row in self.registry.list(actor) if row['state'] == 'active'}
        result = []
        for project in sorted(set(current) | set(history)):
            row = current.get(project)
            active = row is not None
            result.append({'id': project, 'name': row['name'] if active else project, 'scope_kind': 'shared',
                'membership': 'active' if active else 'unavailable', 'role': row['role'] if active else None,
                'can_ask': active,
                'can_do': active,
                'can_write': active and row['can_write'], 'can_read_sources': active,
                'has_personal_history': project in history, 'threads': history.get(project, [])})
        destination.validate()
        return {'items': result}
