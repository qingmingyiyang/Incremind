"""Persist product progress around a single governed organization Turn."""
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4
from fastapi import HTTPException
from core.storage_provider.connection_scope import with_connection_scope

from starlette.concurrency import run_in_threadpool

from .inspirations import freeze_task_turn, validate_task_inputs
from .task_divisions import TaskDivisions
import json
from ..workspace_contracts import _now
from .policies import override
from .policies.pipelines import versions_for_turn
from .outcome_corrections import complete_redos
from ..transaction_records import TransactionRecords
from core.document_engine import SQLiteDocumentRepository
from backend.recognition import WorkScope, RecognitionConflict
from ..source_egress import recognition_service
from .outcomes import validate_selection, record_outcome, prepare_continuation, validate_continuation

TASK_EXECUTIONS = 'v2_task_executions'
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'timed_out',
            'quarantined', 'recovery_required'}


def _policy_bound_initial(initial):
    @wraps(initial)
    def bound(*args, **kwargs):
        # Only new preparation runs here; advance uses the saved request unchanged.
        with override(**versions_for_turn('project.task')):
            return initial(*args, **kwargs)
    return bound


class TaskDo:
    def __init__(self, records, models, drafts, organization, reader, topology, query=None, *, execution_getter=None, service=None):
        self.records, self.models, self.drafts = records, models, drafts
        self.organization, self.reader, self.topology = organization, reader, topology
        self.divisions = TaskDivisions(records, models=models)
        self.query = query
        self.execution_getter = execution_getter
        self.style_service = service or recognition_service(records)

    def execution(self):
        runtime, runner = self.execution_getter() if self.execution_getter else (None, None)
        if runtime is None or runner is None or getattr(runtime, 'task_continuations', None) is None:
            raise HTTPException(409, 'turn_interrupted')
        return runtime, runner

    async def continue_on_request(self, turn_id, project, key):
        from core.ai_kernel import validate_turn_action
        state, turn = self.records.read(TASK_EXECUTIONS, turn_id), self.records.read('v2_turns', turn_id)
        if (state is None or turn is None or state.payload['project_id'] != project
                or turn.payload['project_id'] != project or turn.payload['intent'] != 'do'):
            raise HTTPException(404, 'workbench_not_found')
        runtime, runner = self.execution()
        service = runtime.task_continuations
        request = state.payload['request']
        identity = turn.payload['receipt']['do'].get('kernel_turn_id')
        if (not state.payload['started'] or identity != request['turn_id']
                or request['scope']['project_id'] != project
                or request['session_id'] != 'session-' + turn_id):
            raise HTTPException(409, 'turn_interrupted')
        service.validate_product_request(request, service.store.get_request(identity))
        prior = service.store.get_action(key)
        if prior is not None:
            action, _ = prior
            if action['turn_id'] != identity or action['type'] != 'resume':
                raise HTTPException(409, 'idempotency_key_conflict')
            await self.advance(turn_id)
            return
        binding = service.paused(identity, project)
        if binding is None or runtime.receipt_for(identity).status != 'waiting_approval':
            raise HTTPException(409, 'turn_interrupted')
        service.validate_current(binding[1])
        action = validate_turn_action({'schema_version': '1.0.0', 'action_id': 'action-' + uuid4().hex,
            'turn_id': identity, 'type': 'resume', 'target_event_id': None, 'reason': 'continue partial task',
            'actor': 'user', 'expected_sequence': len(tuple(service.store.events_after(identity))),
            'idempotency_key': key, 'created_at': _now()})
        with service.executing(identity, action, binding):
            await run_in_threadpool(runner.apply_action_and_wait, action)
        with self.records.begin() as tx:
            latest = tx.read('v2_turns', turn_id)
            receipt = dict(latest.payload['receipt']['do'])
            if runtime.receipt_for(identity).status != 'failed':
                receipt = {key: value for key, value in receipt.items()
                           if key not in {'partial', 'interruption'}}
            tx.put('v2_turns', turn_id, {**latest.payload, 'receipt': {'do': {**receipt, 'state': 'running'}}},
                expected_revision=latest.revision)
            tx.commit()
        await self.advance(turn_id)

    @_policy_bound_initial
    @with_connection_scope
    def initial(self, turn_id, project, text, scene, *, division_override=None, situation=None, part_context=None, continuation=None, continuation_version=None):
        selected = (validate_selection(self.records, project=project, scene=scene, selection=continuation)
                    if continuation is not None else None)
        outcome_input = (prepare_continuation(self.records, self.drafts.documents,
            project=project, scene=scene, selection=selected, policy_version=continuation_version or '@1')
            if selected is not None and (selected['mode'] == 'continue' or continuation_version is not None) else
            {'continuation_policy': continuation_version, 'document_id': None} if continuation_version is not None else None)
        from .profile import confirmed_profile, freeze_task_profile
        service = self.style_service
        profile = confirmed_profile(self.records, service)
        from .policies import version
        from .style_context import confirmed_style, style_input, freeze_task_style
        try:
            style_version = version('style')
        except ValueError as error:
            if str(error) != 'unknown_policy_interface':
                raise
            style_version = None
        style = (confirmed_style(self.records, service, project, scene=scene,
            version=style_version, scope_version=version('scope')) if style_version is not None else None)
        query = getattr(self.organization, 'method_query', None)
        from .method_context import prepare_methods, freeze_task_methods
        method_plan = prepare_methods(query, project, text, scene) if query is not None else None
        identity = 'turn-' + uuid4().hex
        rows = self.records.read_batch({'v2_task_divisions': None})
        examples = self.divisions.similar(project,text, rows=rows['v2_task_divisions'])
        previous_turns = {row.object_id: row for row in self.records.read_batch(
            {'v2_turns': tuple(example['source_turn_id'] for example in examples)})['v2_turns']}
        references = []
        for example in examples:
            previous = previous_turns.get(example['source_turn_id'])
            if previous is not None:
                references.append({'turn_id':example['source_turn_id'], 'project_id':example['project_id'],
                                   'thread_id':previous.payload['thread_id']})
        data = {'task':text, 'division_examples':examples, 'division_override':division_override,
                **({'outcome_selection': selected} if selected is not None else {}),
                **({'outcome_input': outcome_input} if outcome_input is not None else {}),
                **({'style_input': style_input(style)} if style is not None else {})}
        if part_context:
            from .part_context import validate_context
            validate_context(self.records, self.query, part_context)
            data['dependency_context'] = part_context['text']
        prompt = json.dumps(data, ensure_ascii=False)
        materials = ([{'type':'recognition', 'id':item['id'], 'revision':item['revision'],
                       'project_id':'me'} for item in profile['items']] +
                     [{'type':'experience' if row.get('inspiration') else 'recognition',
                       'id':row['id'], 'revision':row['entry']['revision'],
                       'project_id':row['scope'].project_id} for row in (method_plan or {}).get('chosen', [])] +
                     (outcome_input.get('materials', []) if outcome_input is not None else []))
        for item in (style or {}).get('items', []):
            material = {'type':'recognition', 'id':item['id'], 'revision':item['revision'],
                        'project_id':item['project_id']}
            # 同一认识可同时作为画像或方法，保留原引用顺序且不重复冻结。
            if material not in materials:
                materials.append(material)
        request = freeze_task_turn(records=self.records, models=self.models, query=self.query,
            inspirations=[row for row in (method_plan or {}).get('chosen', []) if row.get('inspiration')],
            project_id=project, load_text=lambda item: '', text=prompt, materials=materials,
            turn_id=identity, session_id='session-'+turn_id, operation_id='op-'+identity,
            idempotency_key='task-'+turn_id, created_at=_now(), situation=situation)
        from .part_context import bind_context
        bind_context(self.records, request, part_context)
        if outcome_input is not None:
            validate_continuation(self.records, request)
        freeze_task_profile(self.records, request, profile)
        if style is not None:
            freeze_task_style(self.records, request, style)
        if method_plan is not None:
            method_plan['requested_scene'] = scene
            freeze_task_methods(self.records, request, method_plan, turn_id)
        return ({'task_id':None, 'document_id':None, 'title':text[:80], 'state':'running',
                 'kernel_turn_id':identity, 'experts':[], 'division':[],
                 'continues':None, 'changes':[], 'fallback_new':False,
                 'division_examples':references,
                 'progress':{'done':0, 'total':3}},
                {'project_id':project, 'request':request, 'task_text':text, 'scene':scene, 'started':False,
                 'owner':None, 'claimed_at':None, 'failures':0,
                 **({'outcome_selection': selected} if selected is not None else {})})

    @with_connection_scope
    async def advance(self, turn_id):
        owner = uuid4().hex
        with self.records.begin() as tx:
            state, turn = tx.read(TASK_EXECUTIONS, turn_id), tx.read('v2_turns', turn_id)
            if state is None or turn is None or turn.payload['receipt']['do']['state'] in {'done','partial','failed'}:
                tx.rollback()
                return
            values = state.payload
            if values.get('owner') and values.get('claimed_at'):
                if (datetime.now(timezone.utc)-datetime.fromisoformat(values['claimed_at'])).total_seconds() < 60:
                    tx.rollback()
                    return
            tx.put(TASK_EXECUTIONS, turn_id, {**values,'owner':owner,'claimed_at':_now()}, expected_revision=state.revision)
            tx.commit()
        receipt = dict(turn.payload['receipt']['do'])
        updates = {}
        delivery = None
        try:
            request, project = values['request'], values['project_id']
            validate_task_inputs(self.records, self.models, request, query=self.query)
            if values.get('outcome_selection') is not None:
                validate_selection(self.records, project=project, scene=values['scene'],
                                   selection=values['outcome_selection'])
            if not values['started']:
                await run_in_threadpool(self.organization.start, request, agent_turn_mode=True)
                updates['started'] = True
            result = await run_in_threadpool(self.reader, request['turn_id'], project)
            rows = await run_in_threadpool(self.topology, request['turn_id'], project)
            receipt.update(experts=rows, division=rows,
                progress={'done':2 if rows and all(row['state'] in {'done','failed'} for row in rows) else 1, 'total':3})
            if result['status'] == 'waiting_approval' and self.execution_getter:
                runtime, _ = self.execution_getter()
                continuations = getattr(runtime, 'task_continuations', None)
                binding = continuations.paused(request['turn_id'], project) if continuations else None
                if binding is not None:
                    receipt.update(state='interrupted', partial=binding[1]['partial'],
                                   interruption=binding[1]['interruption'])
            if result['status'] in TERMINAL:
                summary = result.get('summary','')
                failed = [row for row in rows if row['state'] == 'failed']
                if failed:
                    summary += '\n\n未完成：' + '；'.join(row.get('goal',row.get('role','')) for row in failed)
                if result['status'] == 'completed' and summary.strip():
                    validate_task_inputs(self.records, self.models, request, query=self.query)
                    composed = result.get('outcome')
                    product_input = json.loads(request['input']['text'])
                    outcome_input = product_input.get('outcome_input')
                    frozen_style = product_input.get('style_input')
                    if frozen_style is not None:
                        from .style_context import frozen_task_style
                        frozen_task_style(self.records, self.style_service, request)
                    if outcome_input is not None and outcome_input['document_id'] is not None and composed is None:
                        raise RecognitionConflict('outcome_composition_unavailable')
                    if composed is None and not (frozen_style is not None and frozen_style['text']):
                        # 未固定写法的原首稿与旧重做保留既有交付时序。
                        output = self.drafts.create(turn_id=request['turn_id'], project=project,
                            operation='deliver-'+request['turn_id'], title=receipt['title'], markdown=summary)
                        receipt['document_id'] = output['document_id']
                    else:
                        delivery = composed['markdown'] if composed is not None else summary
                    receipt.update(state='partial' if failed else 'done', verified=False)
                    if composed is not None:
                        receipt.update(changes=composed['changes'], fallback_new=composed['fallback_new'])
                        updates.update(outcome_composition=composed, outcome_composition_turn=turn_id)
                        if composed['fallback_new']:
                            receipt['continues'] = None
                    receipt['progress'] = {'done':3,'total':3}
                else:
                    receipt.update(state='failed', error='task_execution_failed')
                items = [{key:row[key] for key in ('goal','deliverable','capabilities','depends_on')}
                         for row in rows if 'goal' in row]
                if not items:
                    items = [{'goal':values['task_text'], 'deliverable':'整理稿', 'capabilities':[], 'depends_on':[]}]
                self.divisions.complete(turn_id, project=project, text=values['task_text'], items=items, outcome=receipt['state'])
            updates['failures'] = 0
        except Exception:
            updates['failures'] = values.get('failures',0)+1
            if updates['failures'] >= 3:
                receipt.update(state='failed',error='task_progress_failed')
                for key in ('experts', 'division'):
                    receipt[key] = [{**row, 'state':'failed'} if row.get('state') not in {'done','failed'} else row
                                    for row in receipt.get(key, [])]
        lineage_error = None
        with self.records.begin() as tx:
            state, turn = tx.read(TASK_EXECUTIONS,turn_id), tx.read('v2_turns',turn_id)
            if state is None or turn is None or state.payload.get('owner') != owner:
                tx.rollback()
                return
            if delivery is not None:
                try:
                    validate_task_inputs(TransactionRecords(tx), self.models, values['request'], query=self.query)
                    if json.loads(values['request']['input']['text']).get('outcome_input') is not None:
                        validate_continuation(TransactionRecords(tx), values['request'])
                    if json.loads(values['request']['input']['text']).get('style_input') is not None:
                        from .style_context import frozen_task_style
                        frozen_task_style(TransactionRecords(tx), self.style_service, values['request'])
                    output = self.drafts.create_in_uow(tx, turn_id=values['request']['turn_id'],
                        project=values['project_id'], operation='deliver-'+values['request']['turn_id'],
                        title=receipt['title'], markdown=delivery)
                    receipt['document_id'] = output['document_id']
                except RecognitionConflict as error:
                    lineage_error = error
                    tx.rollback()
            if lineage_error is None:
                tx.put(TASK_EXECUTIONS,turn_id,{**state.payload,**updates,'owner':None},expected_revision=state.revision)
                now = _now()
                tx.put('v2_turns',turn_id,{**turn.payload,'receipt':{'do':receipt},'updated_at':now},expected_revision=turn.revision)
            if lineage_error is None and receipt.get('state') == 'done':
                documents = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=self.drafts.documents.namespace_id)
                complete_redos(tx, documents, scope=WorkScope('local-user', values['project_id']), turn_id=turn_id, now=now)
                try:
                    lineage = record_outcome(tx, project=values['project_id'], document_id=receipt['document_id'],
                        turn_id=turn_id, scene=values['scene'], task_text=values['task_text'],
                        selection=None if receipt.get('fallback_new') is True else values.get('outcome_selection'))
                    if result.get('outcome') is not None and lineage is not None:
                        receipt['continues'] = (None if receipt['fallback_new'] else {
                            'document_id': values['outcome_selection']['document_id'],
                            'version': lineage.payload['version']})
                        current = tx.read('v2_turns', turn_id)
                        tx.put('v2_turns', turn_id, {**current.payload, 'receipt': {'do': receipt}}, expected_revision=current.revision)
                except RecognitionConflict as error:
                    lineage_error = error
                    tx.rollback()
            if lineage_error is None:
                if receipt['state'] in {'done', 'partial', 'interrupted'}:
                    from .turn_frames import complete_stream
                    saved = tx.read('v2_turns', turn_id)
                    complete_stream(tx, {'thread_id': saved.payload['thread_id'],
                        'turn': {'id': saved.object_id, **saved.payload}})
                tx.commit()
        if lineage_error is not None:
            # 新链资格冲突沿原重试计数收口，公开完成和重做事实已经一并回滚。
            with self.records.begin() as tx:
                state, turn = tx.read(TASK_EXECUTIONS, turn_id), tx.read('v2_turns', turn_id)
                if state is None or turn is None or state.payload.get('owner') != owner:
                    tx.rollback()
                    return
                failures = state.payload.get('failures', 0) + 1
                failed_receipt = dict(turn.payload['receipt']['do'])
                if failures >= 3:
                    failed_receipt.update(state='failed', error='outcome_selection_changed')
                tx.put(TASK_EXECUTIONS, turn_id, {**state.payload, 'owner': None,
                    'started': updates.get('started', state.payload['started']), 'failures': failures},
                    expected_revision=state.revision)
                tx.put('v2_turns', turn_id, {**turn.payload, 'receipt': {'do': failed_receipt}, 'updated_at': _now()},
                    expected_revision=turn.revision)
                tx.commit()
