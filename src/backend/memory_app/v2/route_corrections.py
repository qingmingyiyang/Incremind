from copy import deepcopy
import asyncio
from uuid import uuid4

from backend.recognition import RecognitionConflict
from ..workspace_contracts import _now
from .multipart import part_state, part_finished, grouped_remember
from .policies import get
from .route_reuse import dependency_parent
from .turn_execution import TurnExecutionService, safe_failure, _REQUEST_KEY

CORRECTIONS = 'v2_route_corrections'
OPERATIONS = 'v2_route_correction_operations'
REUSES = 'v2_route_correction_reuses'


def validate_operation_id(value):
    if not isinstance(value, str) or not _REQUEST_KEY.fullmatch(value):
        raise ValueError('invalid_route_correction_operation')
    return value


def source_parts(records, row, *, view=None, reader=None, scope_namespace=None):
    owner = records
    records = reader if reader is not None else records
    value = row.payload
    if value['intent'] != 'multi':
        parts = [{'intent': value['intent'], 'span': value['user_text'], 'depends_on': [],
            'turn_id': row.object_id, 'instruction': None}]
    else:
        parts = value['receipt']['parts']
    result = []
    for index, part in enumerate(parts):
        child = records.read('v2_turns', part.get('turn_id') or value.get('part_turn_ids', [])[index]) if value['intent'] == 'multi' else row
        if child is None and value['intent'] == 'multi':
            from ..route_correction_failures import missing_part
            missing = missing_part(owner, row, index, reader=reader)
            if scope_namespace is not None:
                from .projects import display_name
                from ..route_correction_scopes import source_scope
                for member in missing['source_members']:
                    member['scope'] = source_scope(owner, records.read('v2_turns', member['turn_id']),
                        namespace=scope_namespace, reader=reader)
                missing['scope'] = deepcopy(missing['frozen_scope'])
                project = records.read('v2_projects', missing['scope']['project_id'])
                missing['scope_label'] = display_name(missing['scope']['project_id'], project.payload['name'] if project else missing['scope']['project_id'])
            result.append(missing)
            continue
        observed = view(child) if child is not None and view else child.payload if child is not None else None
        if (child is None
                or not part_finished(observed)
                or observed.get('receipt', {}).get('do', {}).get('retry_check_required')):
            raise RecognitionConflict('route_correction_source_unfinished')
        if child.payload['intent'] == 'ask':
            from ..kernel.route_source_snapshot import require_closed_answer
            require_closed_answer(owner, child, reader=reader)
        if value['intent'] == 'multi':
            dependency_parent(records, row.object_id, child.object_id, source_owner=owner)
        members, attachments, original_items = [], [], []
        member_ids = list(dict.fromkeys([*part.get('source_turn_ids', [child.object_id]), *part.get('attachment_source_turn_ids', [])]))
        for member_id in member_ids:
            member = records.read('v2_turns', member_id)
            if member is None:
                raise RecognitionConflict('route_correction_source_changed')
            member_observed = view(member) if view else member.payload
            if not part_finished(member_observed):
                raise RecognitionConflict('route_correction_source_unfinished')
            if member.payload['intent'] == 'ask' and member_id != child.object_id:
                from ..kernel.route_source_snapshot import require_closed_answer
                require_closed_answer(owner, member, reader=reader)
            if value['intent'] == 'multi':
                dependency_parent(records, row.object_id, member_id, source_owner=owner)
            members.append({'turn_id': member_id, 'revision': member.revision, 'parent_turn_id': member.payload.get('parent_turn_id')})
            if scope_namespace is not None:
                from ..route_correction_scopes import source_scope
                members[-1]['scope'] = source_scope(owner, member, namespace=scope_namespace, reader=reader)
            item = member.payload.get('item_id')
            if not item and member.payload['intent'] == 'remember':
                item = member.payload['receipt']['remember'].get('item_id')
            original = records.read('workspace_items', item) if item else None
            if item:
                original_items.append(item)
            if original and original.payload.get('input_kind') in {'file', 'image', 'audio', 'video'}:
                attachments.append(item)
        result.append({'index': index, 'intent': part['intent'], 'span': part['span'],
            'depends_on': list(part.get('depends_on', [])), 'instruction': part.get('instruction'),
            'turn_id': child.object_id, 'revision': child.revision, 'parent_turn_id': child.payload.get('parent_turn_id'),
            'item_ids': attachments, 'original_item_ids': original_items, 'source_members': members})
        if scope_namespace is not None:
            from ..route_correction_scopes import source_scope
            from .projects import display_name
            result[-1]['scope'] = source_scope(owner, child, namespace=scope_namespace, reader=reader)
            scoped_project = records.read('v2_projects', child.payload['project_id'])
            result[-1]['scope_label'] = display_name(child.payload['project_id'], scoped_project.payload['name'] if scoped_project else child.payload['project_id'])
    return result


class RouteCorrections:
    def __init__(self, records, *, instance, origins, view=None, examples=None, shared_execute=None, remember_binding_factory=None, shared_capture=None):
        self.records, self.instance = records, instance
        self.view, self.examples = view, examples
        self.origins, self.shared_execute = origins, shared_execute
        self.remember_binding_factory = remember_binding_factory
        self.shared_capture = shared_capture

    def read(self, turn_id, project_id):
        row = self.records.read('v2_turns', turn_id)
        if row is None or row.payload['project_id'] != project_id:
            raise ValueError('route_correction_not_found')
        marker = self.records.read(CORRECTIONS, turn_id)
        example = self.records.read('v2_route_examples', turn_id)
        try:
            origin = self.origins(turn_id)
            parts = source_parts(self.records, row, view=self.view, scope_namespace=origin['namespace'])
        except RecognitionConflict as error:
            if str(error) not in {'route_correction_missing_child_requires_review',
                    'route_correction_missing_file_requires_review', 'route_correction_missing_do_requires_review'}:
                raise
            reuse = self.records.read(REUSES, turn_id)
            inspection = []
            for index, child_id in enumerate(row.payload.get('part_turn_ids', [])):
                child = self.records.read('v2_turns', child_id)
                inspection.append({'index': index, 'intent': row.payload['receipt']['parts'][index]['intent'],
                    'turn_id': child.object_id if child else None, 'planned_turn_id': child_id})
            return {'turn_id': turn_id, 'project_id': project_id, 'source_revision': row.revision,
                'revision': marker.revision if marker else 0, 'parts': [], 'can_correct': False,
                'blocking_reason': str(error), 'inspection': inspection,
                'operation_id': reuse.payload['operation_id'] if reuse else None,
                'replacement': deepcopy(marker.payload) if marker else None}
        return {'turn_id': turn_id, 'project_id': project_id, 'source_revision': row.revision,
            'revision': marker.revision if marker else 0, 'parts': parts, 'can_correct': True,
            'example': {'revision': example.revision, 'deleted': example.payload.get('deleted', False)} if example else None,
            'replacement': deepcopy(marker.payload) if marker else None}

    def operation(self, operation_id, *, cancel=False):
        validate_operation_id(operation_id)
        with self.records.begin() as tx:
            row = tx.read(OPERATIONS, operation_id)
            if row is None and cancel:
                row = tx.put(OPERATIONS, operation_id, {'status': 'cancelled', 'at': _now()}, expected_revision=0)
            tx.commit()
        if row is None:
            return {'operation_id': operation_id, 'status': 'not_received'}
        result = {**deepcopy(row.payload), 'operation_id': operation_id, 'revision': row.revision}
        if result.get('status') == 'running' and result.get('instance') != self.instance:
            result['status'] = 'unknown'
        result.pop('instance', None)
        for name in ('plan', 'children', 'originals'):
            result.pop(name, None)
        return result

    def claim(self, turn_id, *, project_id, operation_id, expected_revision, source_revision, parts):
        validate_operation_id(operation_id)
        origin = self.origins(turn_id)
        if origin['project_id'] != project_id:
            raise RecognitionConflict('route_correction_origin_changed')
        if origin['kind'] == 'shared' and (self.shared_execute is None or self.shared_capture is None):
            raise RecognitionConflict('route_correction_shared_owner_unavailable')
        if type(expected_revision) is not int or type(source_revision) is not int:
            raise ValueError('invalid_route_correction_revision')
        observed_source = self.records.read('v2_turns', turn_id)
        observed_parts = source_parts(self.records, observed_source, view=self.view, scope_namespace=origin['namespace']) if observed_source is not None else None
        with self.records.begin() as tx:
            if tx.read(OPERATIONS, operation_id) is not None:
                raise RecognitionConflict('route_correction_operation_exists')
            source = tx.read('v2_turns', turn_id)
            old = tx.read(CORRECTIONS, turn_id)
            if (source is None or source.payload['project_id'] != project_id):
                raise ValueError('route_correction_not_found')
            if source.revision != source_revision or (old.revision if old else 0) != expected_revision:
                raise RecognitionConflict('route_correction_revision_changed')
            # 每个原轮只接受一个替换；后续修改从新轮开始，避免同时重做同一结果。
            if old is not None:
                raise RecognitionConflict('route_correction_already_replaced')
            from ..transaction_records import TransactionRecords
            originals = source_parts(self.records, source, reader=TransactionRecords(tx), scope_namespace=origin['namespace'])
            if observed_parts != originals:
                raise RecognitionConflict('route_correction_source_changed')
            from ..route_correction_failures import close_missing_parts
            close_missing_parts(self.records, tx, originals)
            from ..route_correction_originals import capture_member_lineage
            if origin['kind'] == 'shared':
                self.shared_capture(tx, originals, origin)
            else:
                for original in originals:
                    for member in original['source_members']:
                        capture_member_lineage(self.records, tx, member)
            plan = get('route_correction', version='@2')(originals, parts)
            if self.remember_binding_factory is None and any(plan['parts'][index]['intent'] == 'remember'
                    and not plan['parts'][index]['attachment_aggregate'] for index in plan['affected']):
                raise RecognitionConflict('route_correction_remember_owner_unavailable')
            unchanged = len(plan['parts']) == len(originals) and not plan['affected'] and not plan['deleted']
            if unchanged:
                raise ValueError('route_correction_unchanged')
            from ..route_correction_scopes import PARENT_PREFIX
            parent_id = PARENT_PREFIX + uuid4().hex
            children = [originals[part['sources'][0]]['turn_id'] if index not in plan['affected'] or part['attachment_aggregate'] else 'turn-' + uuid4().hex
                for index, part in enumerate(plan['parts'])]
            planned_scopes = {children[index]: {'part_index': index, 'target': deepcopy(part['target_scope']),
                'selected_source_ids': [member['turn_id'] for member in part['source_members']]}
                for index, part in enumerate(plan['parts']) if index in plan['affected'] and not part['attachment_aggregate']}
            payload = {'schema_version': 3, 'status': 'running', 'source_turn_id': turn_id, 'source_revision': source.revision,
                'project_id': project_id, 'replacement_turn_id': parent_id, 'plan': plan,
                'children': children, 'originals': originals, 'planned_scopes': planned_scopes, 'instance': self.instance, 'at': _now()}
            payload['source_origin'] = origin
            tx.put(OPERATIONS, operation_id, payload, expected_revision=0)
            tx.put(CORRECTIONS, turn_id, {'operation_id': operation_id, 'replacement_turn_id': parent_id,
                'affected': plan['affected'], 'deleted': plan['deleted'], 'status': 'running', 'at': _now()}, expected_revision=0)
            parent = {'id': parent_id, 'project_id': project_id, 'thread_id': source.payload['thread_id'],
                'intent': 'multi', 'user_text': source.payload['user_text'], 'scene': source.payload.get('scene'),
                'created_at': _now(), 'updated_at': _now(), 'instance': self.instance, 'part_turn_ids': list(children),
                'receipt': {'parts': [{**part, 'index': index, 'turn_id': children[index],
                    'project_id': part['target_scope']['project_id'], 'state': 'waiting' if index in plan['affected'] else part_state(tx.read('v2_turns', children[index]).payload),
                    'receipt': {} if index in plan['affected'] else deepcopy(tx.read('v2_turns', children[index]).payload['receipt'])}
                    for index, part in enumerate(plan['parts'])], 'route': {'mode': 'manual', 'usage': None, 'egress_receipt_id': None}},
                'attachment_item_ids': [item for part in plan['parts'] for item in part['item_ids']]}
            tx.put('v2_turns', parent_id, parent, expected_revision=0)
            reused = {}
            for index, part in enumerate(plan['parts']):
                if index not in plan['affected'] or part['item_ids'] or any(
                        tx.read('v2_turns', member['turn_id']).payload['intent'] in {'remember', 'inspiration'} for member in part['source_members']):
                    for member in part['source_members']:
                        reused[member['turn_id']] = member
                if part['attachment_aggregate']:
                    parent['receipt']['parts'][index].update(source_turn_ids=[member['turn_id'] for member in part['source_members']], state='done')
                elif part['item_ids'] and part['intent'] != 'remember':
                    parent['receipt']['parts'][index]['attachment_source_turn_ids'] = [member['turn_id'] for member in part['source_members']
                        if tx.read('v2_turns', member['turn_id']).payload['intent'] == 'remember']
            saved_parent = tx.read('v2_turns', parent_id)
            tx.put('v2_turns', parent_id, parent, expected_revision=saved_parent.revision)
            tx.put(REUSES, parent_id, {'operation_id': operation_id, 'source_turn_id': turn_id,
                'source_revision': source.revision, 'project_id': project_id, 'parts': reused,
                'planned_scopes': planned_scopes}, expected_revision=0)
            tx.commit()
        return operation_id

    async def execute(self, operation_id, execute, *, classify_do=None):
        row = self.records.read(OPERATIONS, operation_id)
        if row is None or row.payload['instance'] != self.instance or row.payload['status'] != 'running':
            raise RecognitionConflict('route_correction_execution_changed')
        value = row.payload
        if self.origins(value['source_turn_id']) != value['source_origin']:
            raise RecognitionConflict('route_correction_origin_changed')
        parent_id, project = value['replacement_turn_id'], value['project_id']
        parts = value['plan']['parts']
        affected = set(value['plan']['affected'])
        events = [asyncio.Event() for _ in parts]
        failed = set()
        for index in range(len(parts)):
            if index not in affected:
                events[index].set()

        def update(index, **changes):
            with self.records.begin() as tx:
                parent = tx.read('v2_turns', parent_id)
                current = deepcopy(parent.payload)
                current['receipt']['parts'][index].update(changes)
                current['updated_at'] = _now()
                tx.put('v2_turns', parent_id, current, expected_revision=parent.revision)
                tx.commit()

        async def run(index):
            part = parts[index]
            try:
                for dependency in part['depends_on']:
                    await events[dependency].wait()
                if any(dependency in failed for dependency in part['depends_on']):
                    failed.add(index)
                    update(index, state='not_started', error='dependency_failed')
                    return
                if part['attachment_aggregate']:
                    grouped = grouped_remember(self.records, [(parent_id, member['turn_id']) for member in part['source_members']], self.view or (lambda row: row.payload))
                    if grouped['remember']['state'] != 'done':
                        failed.add(index)
                    update(index, state=grouped['remember']['state'], receipt=grouped)
                    return
                # 认领后只运行本次新建子轮，任何GET与进程重启均不会进入此入口。
                update(index, state='running')
                parent = self.records.read('v2_turns', parent_id)
                target = part['target_scope']
                body = {'project_id': target['project_id'], 'thread_id': target['thread_id'],
                    'text': part['span'], 'intent': part['intent']}
                existing_file_only = part['item_ids'] and all(value['originals'][source]['intent'] == 'remember' for source in part['sources'])
                if existing_file_only and part['intent'] == 'remember' and len(part['item_ids']) == 1:
                    body['item_id'] = part['item_ids'][0]
                classified = {}
                if part['intent'] == 'do':
                    if classify_do is None:
                        raise RecognitionConflict('route_correction_classifier_unavailable')
                    classified = await classify_do(part['span'], project_id=target['project_id'],
                        request_key='correction-' + value_ids[index])
                    if not isinstance(classified, dict) or set(classified) - {'situation', 'data_operation', 'route_meta'}:
                        raise ValueError('route_correction_classifier_invalid')
                async def child(value, **options):
                    dependencies = []
                    for dependency in part['depends_on']:
                        source = parts[dependency]
                        dependencies.extend([member['turn_id'] for member in source['source_members']]
                            if source['attachment_aggregate'] else [value_ids[dependency]])
                    if part['intent'] in {'ask', 'do'}:
                        dependencies.extend(member['turn_id'] for member in part['source_members']
                            if self.records.read('v2_turns', member['turn_id']).payload['intent'] in {'remember', 'inspiration'})
                    if self.origins(row.payload['source_turn_id']) != row.payload['source_origin']:
                        raise RecognitionConflict('route_correction_origin_changed')
                    selected_execute = self.shared_execute if row.payload['source_origin']['kind'] == 'shared' else execute
                    remembered = ({'remember_stage_created': self.remember_binding_factory(operation_id, index)}
                        if part['intent'] == 'remember' and not existing_file_only else {})
                    return await selected_execute(value, **options, forced_turn_id=value_ids[index],
                        parent_turn_id=parent_id, forced_scene=target['scene'], forced_project=target['project_id'],
                        forced_thread_id=target['thread_id'],
                        instruction=part['instruction'] if part['intent'] in {'ask', 'do'} else None,
                        dependency_turns=list(dict.fromkeys(dependencies)), **classified, **remembered)
                executor = TurnExecutionService(self.records, child, instance=self.instance,
                    internal_keys=True, internal_identity='route-correction-' + value_ids[index])
                result = await executor.run(body, operation_id + ':' + str(index))
                state = part_state(result['turn'])
                if part['intent'] == 'ask' and result['turn']['receipt']['ask'].get('error'):
                    state = 'failed'
                if state in {'failed', 'interrupted', 'unknown', 'not_started', 'partial', 'stopped', 'cancelled'}:
                    failed.add(index)
                update(index, state=state, receipt=result['turn']['receipt'], turn_id=result['turn']['id'])
                if part['item_ids'] and part['intent'] == 'remember' and not existing_file_only:
                    retained = [member['turn_id'] for member in part['source_members']
                        if self.records.read('v2_turns', member['turn_id']).payload['intent'] == 'remember']
                    update(index, source_turn_ids=[value_ids[index], *retained])
            except Exception as error:
                failed.add(index)
                update(index, state='failed', error=safe_failure(error))
            finally:
                events[index].set()

        value_ids = value['children']
        await asyncio.gather(*(run(index) for index in sorted(affected)))
        # 原记住与干活可在后台继续；等待实际公开终态后再保存完成样例。
        while True:
            parent = self.records.read('v2_turns', parent_id)
            pending = False
            for index in affected:
                if parts[index]['attachment_aggregate']:
                    continue
                child = self.records.read('v2_turns', value_ids[index])
                if child is None:
                    continue
                observed = self.view(child) if self.view else child.payload
                state = part_state(observed)
                if state in {'unknown', 'interrupted'}:
                    update(index, state='unknown')
                    with self.records.begin() as tx:
                        operation = tx.read(OPERATIONS, operation_id)
                        tx.put(OPERATIONS, operation_id, {**operation.payload, 'status': 'unknown'}, expected_revision=operation.revision)
                        tx.commit()
                    return
                elif not part_finished(observed):
                    pending = True
                else:
                    if state in {'failed', 'not_started', 'partial', 'stopped', 'cancelled'}:
                        failed.add(index)
                    update(index, state=state, receipt=observed['receipt'])
            if not pending:
                break
            await asyncio.sleep(.25)
        with self.records.begin() as tx:
            current = tx.read(OPERATIONS, operation_id)
            parent = tx.read('v2_turns', parent_id)
            status = 'partial' if failed else 'completed'
            tx.put(OPERATIONS, operation_id, {**current.payload, 'status': status, 'finished_at': _now()}, expected_revision=current.revision)
            marker = tx.read(CORRECTIONS, value['source_turn_id'])
            tx.put(CORRECTIONS, marker.object_id, {**marker.payload, 'status': status}, expected_revision=marker.revision)
            if self.examples is not None:
                previous = tx.read('v2_route_examples', value['source_turn_id'])
                if previous is not None and not previous.payload.get('deleted'):
                    tx.put('v2_route_examples', previous.object_id, {**previous.payload, 'deleted': True}, expected_revision=previous.revision)
                if not failed:
                    self.examples.stage(tx, parent_id, parts=[{key: part[key] for key in ('intent', 'span', 'depends_on', 'instruction')} for part in parts],
                        corrected=True, source_turn_id=value['source_turn_id'])
            tx.commit()
        return parent.payload
