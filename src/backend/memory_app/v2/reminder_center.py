from dataclasses import asdict
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from core.companion_core import CompanionRepository
from core.companion_core.reminders import CompanionReminderService
from core.companion_core.errors import CompanionConflict, CompanionRepositoryError
from ..workspace_contracts import _json, _project, _text
from .policies import get, version
from .policies.insight_time import instant
from .privacy import is_private_project
from .reminders import PreparedReminder


OPERATIONS = 'v2_reminder_operations'
BINDINGS = 'v2_reminder_bindings'


def _revision(value):
    if type(value) is not int or value < 1:
        raise HTTPException(400, 'invalid_expected_revision')
    return value


class ReminderCenter:
    def __init__(self, records, reminders, legacy, *, nudges=None):
        self.records, self.reminders, self.legacy = records, reminders, legacy
        self.nudges = nudges

    def _v2_view(self, value, *, delivered=None):
        return {'id': value['id'], 'source': 'v2', 'project_id': value['project_id'],
                'scene': value['scene'], 'title': value['text'], 'scheduled_at': value['at'],
                'state': value['state'], 'revision': value['revision'], 'binding_revision': 0,
                'turn_id': value['turn_id'], 'private': is_private_project(self.records, value['project_id']),
                'recurrence': 'once', 'repeat_count': 1, 'delivery': delivered,
                'actions': ['complete', 'cancel', 'reschedule'] if value['state'] == 'active' else []}

    def list(self, project_id):
        project = _project(project_id)
        delivered = {row['event_id']: row for row in self.nudges.list(project)
                     if row['kind'] == 'reminder'} if self.nudges is not None else {}
        values = [self._v2_view(row, delivered=delivered.get(row['id']))
                  for row in self.reminders.list(project)]
        values.extend(self._legacy_list())
        return {'items': sorted(values, key=lambda row: (row['scheduled_at'], row['source'], row['id']))}

    def _legacy_rows(self):
        self.legacy.list()
        return {row.reminder_id: row for row in self.legacy.repository.list_reminders(include_cancelled=True)}

    def _legacy_list(self):
        rows = self._legacy_rows()
        bindings = self.records.list(BINDINGS)
        hidden = {identity for row in bindings for identity in row.payload['source_ids']}
        result = [self._legacy_view(row.reminder_id, row) for row in rows.values() if row.reminder_id not in hidden]
        for binding in bindings:
            current = rows.get(binding.payload['current_id'])
            if current is None:
                raise HTTPException(409, 'reminder_binding_changed')
            value = self._legacy_view(binding.object_id, current, binding.revision)
            if binding.payload.get('active_operation'):
                operation = self.records.read(OPERATIONS, binding.payload['active_operation'])
                if operation is None:
                    raise HTTPException(409, 'reminder_binding_changed')
                value.update(actions=[], pending_operation={'id':operation.object_id,
                    'project_id':operation.payload['request']['project_id']})
            result.append(value)
        return result

    def _legacy_view(self, identity, row, binding_revision=0):
        occurrences = self.legacy.repository.list_reminder_occurrences(row.reminder_id)
        remaining = [item for item in occurrences if item.phase == 'due' and item.state in {'pending', 'presented', 'snoozed'}]
        presented = next((item for item in occurrences if item.phase == 'due' and item.state == 'presented'), None)
        return {'id': identity, 'source': 'legacy', 'project_id': None, 'scene': None,
                'title': row.schedule['title'], 'scheduled_at': row.schedule['scheduled_at'],
                'state': row.ack_state, 'revision': row.revision, 'binding_revision': binding_revision,
                'turn_id': None, 'private': False, 'recurrence': row.schedule['recurrence'],
                'repeat_count': row.schedule['repeat_count'], 'delivery': None,
                'remaining_count': len(remaining), 'next_at': (remaining[0].snooze_until or remaining[0].scheduled_for) if remaining else None,
                'complete_occurrence': {'id': presented.occurrence_id, 'revision': presented.revision} if presented else None,
                'actions': (['complete'] if presented else []) +
                           (['cancel', 'reschedule'] if row.ack_state == 'pending' else [])}

    def _validate(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('action'), str):
            raise HTTPException(400, 'invalid_reminder_command')
        _project(body.get('project_id'))
        if body['action'] == 'create':
            if set(body).difference({'project_id', 'action', 'text', 'at', 'scene'}) or 'text' not in body:
                raise HTTPException(400, 'invalid_reminder_command')
            _text(body['text'], 'reminder_text')
            scene = body.get('scene')
            if scene is not None and (not isinstance(scene,str) or not scene.strip()):
                raise HTTPException(400, 'invalid_reminder_scene')
        else:
            required = {'project_id', 'action', 'source', 'id', 'expected_revision', 'binding_revision'}
            if (not required <= set(body) or set(body).difference(required | {'at', 'occurrence'})
                    or body['action'] not in {'complete', 'cancel', 'reschedule'}
                    or body['source'] not in ('v2', 'legacy')):
                raise HTTPException(400, 'invalid_reminder_command')
            _project(body['id'])
            _revision(body['expected_revision'])
            if type(body['binding_revision']) is not int or body['binding_revision'] < 0:
                raise HTTPException(400, 'invalid_expected_revision')
            if body['action'] == 'reschedule' and 'at' not in body:
                raise HTTPException(400, 'invalid_reminder_at')
            if body['action'] != 'reschedule' and 'at' in body:
                raise HTTPException(400, 'invalid_reminder_command')
            if not (body['source'] == 'legacy' and body['action'] == 'complete') and 'occurrence' in body:
                raise HTTPException(400, 'invalid_reminder_command')
            if body['source'] == 'legacy' and body['action'] == 'complete':
                event = body.get('occurrence')
                if not isinstance(event, dict) or set(event) != {'id', 'revision'}:
                    raise HTTPException(400, 'invalid_reminder_command')
                _project(event['id'])
                _revision(event['revision'])

    def _time_plan(self, body):
        reference = self.reminders.now().isoformat()
        selected = self.reminders.policy_version or version('remind')
        if 'at' in body:
            at = get('remind', version=selected)(body['at'], reference,
                self.reminders.local_timezone, operation='at')
        else:
            text = body['text']
            eligible = text if text.startswith('提醒我') else '提醒我' + text
            parsed = self.reminders.parse(text, eligible_text=eligible)
            if parsed is None:
                raise HTTPException(400, 'reminder_time_required')
            return {'at': parsed.at, 'reference': parsed.reference, 'policy_version': parsed.policy_version}
        if at is None:
            raise HTTPException(400, 'invalid_reminder_at')
        return {'at': at, 'reference': reference, 'policy_version': selected}

    def _operation(self, row):
        return {'id': row.object_id, **row.payload, 'revision': row.revision}

    def read_operation(self, identity, project_id):
        row = self.records.read(OPERATIONS, _project(identity))
        if row is None or row.payload['request']['project_id'] != _project(project_id):
            raise HTTPException(404, 'reminder_operation_not_found')
        result = self._operation(row)
        if row.payload['request'].get('source') == 'legacy' and row.payload['state'] not in {'completed', 'abandoned'}:
            result['observed'] = self._observe_legacy(row.payload['plan'])
        return result

    def _prepare_legacy(self, tx, identity, body):
        if any(row.object_id != body['id'] and body['id'] in row.payload['source_ids'] for row in tx.list(BINDINGS)):
            raise HTTPException(409, 'reminder_binding_changed')
        binding = tx.read(BINDINGS, body['id'])
        revision = binding.revision if binding else 0
        if revision != body['binding_revision']:
            raise HTTPException(409, 'reminder_binding_changed')
        if binding and binding.payload.get('active_operation'):
            raise HTTPException(409, 'reminder_operation_in_progress')
        current_id = binding.payload['current_id'] if binding else body['id']
        current = self._legacy_rows().get(current_id)
        if current is None or current.revision != body['expected_revision']:
            raise HTTPException(409, 'reminder_revision_conflict')
        occurrences = [asdict(item) for item in self.legacy.repository.list_reminder_occurrences(current_id)]
        if body['action'] == 'complete':
            event = next((item for item in occurrences if item['occurrence_id'] == body['occurrence']['id']), None)
            if event is None or event['state'] != 'presented' or event['phase'] != 'due' or event['revision'] != body['occurrence']['revision']:
                raise HTTPException(409, 'reminder_revision_conflict')
        elif current.ack_state != 'pending':
            raise HTTPException(409, 'reminder_not_active')
        plan = {'source': asdict(current), 'occurrences': occurrences}
        if body['action'] == 'complete':
            plan['complete_occurrence'] = body['occurrence']
        if body['action'] == 'reschedule':
            remaining = sum(item['phase'] == 'due' and item['state'] in {'pending', 'presented', 'snoozed'} for item in occurrences)
            if remaining < 1:
                raise HTTPException(409, 'reminder_not_active')
            plan.update(self._time_plan(body))
            plan['target_id'] = 'rem_g08_' + uuid4().hex
            plan['target_schedule'] = {'schema_version': 1, 'title': current.schedule['title'],
                'timezone': current.schedule['timezone'], 'scheduled_at': plan['at'],
                'recurrence': 'daily' if remaining > 1 else 'once', 'repeat_count': remaining}
        payload = binding.payload if binding else {'current_id': current_id, 'source_ids': [current_id]}
        claimed = tx.put(BINDINGS, body['id'], {**payload, 'active_operation': identity}, expected_revision=revision)
        plan['binding_revision'] = claimed.revision
        return plan

    def _observe_legacy(self, plan):
        rows = self._legacy_rows()
        source = rows.get(plan['source']['reminder_id'])
        events = self.legacy.repository.list_reminder_occurrences(source.reminder_id) if source else ()
        target = rows.get(plan.get('target_id'))
        retired = source is not None and source.ack_state == 'cancelled' and not any(
            item.state in {'pending', 'presented', 'snoozed'} for item in events)
        completion = plan.get('complete_occurrence')
        completed = completion is not None and any(item.occurrence_id == completion['id']
            and item.state == 'completed' and item.revision > completion['revision'] for item in events)
        return {'source_revision': source.revision if source else None, 'source_retired': retired,
                'target_created': target is not None, 'target_valid': self._target_valid(target, plan) if target else False,
                'completion_completed': completed, 'source_state': source.ack_state if source else None,
                'occurrences': [asdict(item) for item in events],
                'planned_count': plan.get('target_schedule',{}).get('repeat_count'),
                'planned_at': plan.get('at')}

    def _target_valid(self, target, plan):
        if (target.schedule != plan.get('target_schedule')
                or target.advance_minutes != plan['source']['advance_minutes']):
            return False
        occurrences = self.legacy.repository.list_reminder_occurrences(target.reminder_id)
        return (sum(item.phase == 'due' for item in occurrences) == plan['target_schedule']['repeat_count']
                and sum(item.phase == 'advance' for item in occurrences) ==
                (plan['target_schedule']['repeat_count'] if target.advance_minutes else 0))

    def _binding(self, tx, identity, operation):
        binding = tx.read(BINDINGS, operation.payload['request']['id'])
        if (binding is None or binding.payload.get('active_operation') != identity
                or binding.revision != operation.payload['plan']['binding_revision']):
            raise HTTPException(409, 'reminder_binding_changed')
        return binding

    def _retire_legacy(self, plan):
        snapshot = plan.get('retirement_snapshot',plan)
        source = snapshot['source']
        current = self._legacy_rows().get(source['reminder_id'])
        if current is None or asdict(current) != source:
            raise CompanionConflict('source reminder changed')
        expected = source['revision']
        events = {item['occurrence_id']: dict(item) for item in snapshot['occurrences']}
        for event in events.values():
            if event['state'] == 'presented':
                saved = self.legacy.act(event_id=event['occurrence_id'], action='acknowledge', expected_revision=event['revision'])
                if saved.state != 'acknowledged' or saved.revision != event['revision'] + 1:
                    raise CompanionConflict('source occurrence changed')
                event.update(asdict(saved))
                expected += 1
        actual = self._legacy_rows()[source['reminder_id']]
        actual_events = {item.occurrence_id: asdict(item) for item in self.legacy.repository.list_reminder_occurrences(actual.reminder_id)}
        if actual.revision != expected or actual_events != events:
            raise CompanionConflict('source reminder changed')
        self.legacy.cancel(reminder_id=actual.reminder_id, expected_revision=expected)
        observed = self._observe_legacy(plan)
        if not observed['source_retired'] or observed['source_revision'] != expected + 1:
            raise CompanionConflict('source reminder retirement not verified')

    def _finish_legacy(self, tx, identity, operation, target, *, commit=True):
        binding = self._binding(tx, identity, operation)
        body, plan = operation.payload['request'], operation.payload['plan']
        source_ids = list(binding.payload['source_ids'])
        if target.reminder_id not in source_ids:
            source_ids.append(target.reminder_id)
        saved_binding = tx.put(BINDINGS, body['id'], {**binding.payload, 'current_id': target.reminder_id,
            'source_ids': source_ids, 'active_operation': None}, expected_revision=binding.revision)
        result = self._legacy_view(body['id'], target, saved_binding.revision)
        saved = tx.put(OPERATIONS, identity, {**{key:value for key,value in operation.payload.items() if key!='error'}, 'state': 'completed', 'result': result},
                       expected_revision=operation.revision + 1)
        if commit:
            tx.commit()
            return self._operation(saved)
        return saved

    def _apply_legacy(self, tx, identity, operation):
        body, plan = operation.payload['request'], operation.payload['plan']
        self._binding(tx, identity, operation)
        try:
            if body['action'] == 'complete':
                current = self._legacy_rows().get(plan['source']['reminder_id'])
                if current is None or asdict(current) != plan.get('confirmed_completion_source',plan['source']):
                    raise CompanionConflict('source reminder changed')
                self.legacy.act(event_id=body['occurrence']['id'], action='complete', expected_revision=body['occurrence']['revision'])
            else:
                if plan.get('confirmed_source_revision') is None:
                    self._retire_legacy(plan)
                else:
                    observed = self._observe_legacy(plan)
                    if not observed['source_retired'] or observed['source_revision'] != plan['confirmed_source_revision']:
                        raise CompanionConflict('source reminder changed')
                if body['action'] == 'reschedule':
                    target = self._legacy_rows().get(plan['target_id'])
                    if target is None:
                        if instant(plan['at']) <= self.reminders.now():
                            raise HTTPException(409, 'reminder_replacement_time_expired')
                        schedule = plan['target_schedule']
                        creator = CompanionReminderService(self.legacy.repository, now=self.legacy.now,
                                                           id_factory=lambda: plan['target_id'])
                        target = creator.create(title=schedule['title'], scheduled_at=schedule['scheduled_at'],
                            timezone_name=schedule['timezone'], advance_minutes=plan['source']['advance_minutes'],
                            recurrence=schedule['recurrence'], repeat_count=schedule['repeat_count'])
                    if not self._target_valid(target, plan):
                        raise CompanionConflict('replacement reminder changed')
                    return self._finish_legacy(tx, identity, operation, target)
            target = self._legacy_rows()[plan['source']['reminder_id']]
            return self._finish_legacy(tx, identity, operation, target)
        except (CompanionRepositoryError, HTTPException) as error:
            code = ('reminder_revision_conflict' if isinstance(error, CompanionConflict)
                    else error.detail if isinstance(error,HTTPException) and error.detail == 'reminder_replacement_time_expired'
                    else 'reminder_operation_incomplete')
            saved = tx.put(OPERATIONS, identity, {**operation.payload, 'state': 'blocked', 'error': code},
                           expected_revision=operation.revision + 1)
            tx.commit()
            return self._operation(saved)

    def resume(self, identity, *, project_id, expected_revision, source_revision, occurrences):
        _revision(expected_revision)
        _revision(source_revision)
        with self.records.begin() as tx:
            operation = tx.read(OPERATIONS, _project(identity))
            if (operation is None or operation.payload['request']['project_id'] != _project(project_id)
                    or operation.revision != expected_revision
                    or operation.payload['request'].get('source') != 'legacy'
                    or operation.payload['state'] not in {'prepared', 'blocked'}):
                raise HTTPException(409, 'reminder_operation_conflict')
            self._binding(tx, identity, operation)
            observed = self._observe_legacy(operation.payload['plan'])
            if observed['source_revision'] != source_revision or observed['occurrences'] != occurrences:
                raise HTTPException(409, 'reminder_revision_conflict')
            if observed['completion_completed']:
                tx.put(OPERATIONS, identity, {**operation.payload, 'state': 'executing'}, expected_revision=operation.revision)
                target = self._legacy_rows()[operation.payload['plan']['source']['reminder_id']]
                return self._finish_legacy(tx, identity, operation, target)
            plan, body = dict(operation.payload['plan']), operation.payload['request']
            source = self._legacy_rows().get(plan['source']['reminder_id'])
            if (source is None or source.revision != source_revision or source.schedule != plan['source']['schedule']
                    or source.advance_minutes != plan['source']['advance_minutes']
                    or observed['target_created'] and (not observed['target_valid'] or not observed['source_retired'])):
                raise HTTPException(409, 'reminder_source_not_retired')
            if body['action'] == 'complete':
                event = next((item for item in occurrences if item['occurrence_id']==body['occurrence']['id']),None)
                if event is None or event['state']!='presented' or event['revision']!=body['occurrence']['revision']:
                    raise HTTPException(409, 'reminder_revision_conflict')
                plan['confirmed_completion_source'] = asdict(source)
            elif observed['source_retired']:
                plan['confirmed_source_revision'] = source_revision
            else:
                # 原改期目标保持不变，用户只确认下一段退役所依据的实际状态。
                plan.pop('confirmed_source_revision',None)
                plan['retirement_snapshot'] = {'source':asdict(source),'occurrences':occurrences}
            value = {**{key:value for key,value in operation.payload.items() if key!='error'}, 'state': 'prepared', 'plan': plan}
            tx.put(OPERATIONS, identity, value, expected_revision=operation.revision)
            tx.commit()
        return self.apply(identity, value['request'])

    def prepare(self, identity, body):
        identity = _project(identity)
        self._validate(body)
        with self.records.begin() as tx:
            existing = tx.read(OPERATIONS, identity)
            if existing is not None:
                if existing.payload['request'] != body:
                    raise HTTPException(409, 'reminder_operation_conflict')
                return self._operation(existing)
            if body['action'] == 'create':
                plan = {**self._time_plan(body), 'target_id': 'manual-' + uuid4().hex}
            elif body['source'] == 'v2':
                current = self.reminders.read(body['id'], project_id=body['project_id'], reader=tx)
                if current['revision'] != body['expected_revision'] or body['binding_revision'] != 0:
                    raise HTTPException(409, 'reminder_revision_conflict')
                plan = self._time_plan(body) if body['action'] == 'reschedule' else {}
            else:
                plan = self._prepare_legacy(tx, identity, body)
            saved = tx.put(OPERATIONS, identity, {'request': body, 'plan': plan,
                'state': 'prepared', 'result': None}, expected_revision=0)
            tx.commit()
            return self._operation(saved)

    def apply(self, identity, body):
        self.prepare(identity, body)
        with self.records.begin() as tx:
            operation = tx.read(OPERATIONS, identity)
            value = operation.payload
            if value['state'] != 'prepared':
                return self._operation(operation)
            # 首次写入取得同一数据库写锁，放弃和执行不能越过彼此的终态。
            tx.put(OPERATIONS, identity, {**value, 'state': 'executing'}, expected_revision=operation.revision)
            if body['action'] == 'create':
                parsed = PreparedReminder(value['plan']['at'], body['text'], value['plan']['reference'], value['plan']['policy_version'])
                result = self.reminders.stage_manual(tx, identity=value['plan']['target_id'],
                    project_id=body['project_id'], scene=body.get('scene'), parsed=parsed)
                result = self._v2_view(result)
            elif body['source'] == 'v2':
                changes = {'at': value['plan']['at']} if body['action'] == 'reschedule' else {
                    'state': 'done' if body['action'] == 'complete' else 'deleted'}
                result = self._v2_view(self.reminders.stage_update(tx, body['id'],
                    project_id=body['project_id'], expected_revision=body['expected_revision'], **changes))
            else:
                return self._apply_legacy(tx, identity, operation)
            saved = tx.put(OPERATIONS, identity, {**value, 'state': 'completed', 'result': result},
                           expected_revision=operation.revision + 1)
            tx.commit()
            return self._operation(saved)

    def abandon(self, identity, *, project_id, request, expected_revision):
        self._validate(request)
        if request['project_id'] != _project(project_id) or type(expected_revision) is not int or expected_revision < 0:
            raise HTTPException(400, 'invalid_reminder_command')
        with self.records.begin() as tx:
            row = tx.read(OPERATIONS, _project(identity))
            if row is None:
                if expected_revision != 0:
                    raise HTTPException(409, 'reminder_operation_conflict')
                saved = tx.put(OPERATIONS, identity, {'request': request, 'plan': {},
                    'state': 'abandoned', 'result': None}, expected_revision=0)
            else:
                if row.payload['request'] != request or row.revision != expected_revision:
                    raise HTTPException(409, 'reminder_operation_conflict')
                if row.payload['state'] in {'completed', 'abandoned'}:
                    return self._operation(row)
                saved = self._abandon_prepared(tx, identity, row)
            tx.commit()
            return self._operation(saved)

    def _abandon_prepared(self, tx, identity, row):
        effects = None
        if row.payload['request'].get('source') == 'legacy':
            binding = self._binding(tx, identity, row)
            effects = self._observe_legacy(row.payload['plan'])
            if effects['target_created'] or effects['completion_completed']:
                if effects['target_created'] and (not effects['target_valid'] or not effects['source_retired']):
                    raise HTTPException(409, 'reminder_operation_incomplete')
                tx.put(OPERATIONS, identity, {**row.payload, 'state': 'executing'}, expected_revision=row.revision)
                target_id = row.payload['plan'].get('target_id') if effects['target_created'] else row.payload['plan']['source']['reminder_id']
                return self._finish_legacy(tx, identity, row, self._legacy_rows()[target_id], commit=False)
            tx.put(BINDINGS, binding.object_id, {**binding.payload, 'active_operation': None}, expected_revision=binding.revision)
        return tx.put(OPERATIONS, identity, {**row.payload, 'state': 'abandoned', 'effects': effects}, expected_revision=row.revision)


def install_reminder_center_routes(application, *, records, reminders, runtime_root, nudges=None):
    legacy = CompanionReminderService(CompanionRepository.at_data_root(runtime_root), now=reminders.now)
    service = ReminderCenter(records, reminders, legacy, nudges=nudges)
    application.state.memory_reminder_center = service
    router = APIRouter(prefix='/api/v2/reminder-center')

    @router.get('')
    async def list_reminders(project_id: str):
        return await run_in_threadpool(service.list, project_id)

    @router.get('/operations/{id}')
    async def read_operation(id: str, project_id: str):
        return await run_in_threadpool(service.read_operation, id, project_id)

    @router.post('/operations/{id}')
    async def apply_operation(id: str, request: Request):
        return await run_in_threadpool(service.apply, id, await _json(request))

    @router.post('/operations/{id}/abandon')
    async def abandon_operation(id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id', 'request', 'expected_revision'}:
            raise HTTPException(400, 'invalid_reminder_command')
        return await run_in_threadpool(service.abandon, id, **body)

    @router.post('/operations/{id}/resume')
    async def resume_operation(id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id', 'expected_revision', 'source_revision', 'occurrences'}:
            raise HTTPException(400, 'invalid_reminder_command')
        return await run_in_threadpool(service.resume, id, **body)

    application.include_router(router)
