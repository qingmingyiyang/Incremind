"""用真实完成态 Kernel 和不可变成果验证任务读取接点。"""
import json
from types import SimpleNamespace

import pytest

from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.ai_kernel.turn_kinds import freeze_turn_request
from backend.memory_app.v2 import TaskRuntimeProjection
from backend.memory_app.v2.outcomes import COMPOSITION


def test_completed_task_projection_uses_its_original_turn_store(tmp_path):
    store = SQLiteAITurnStore(tmp_path / 'ai-turns.sqlite3')
    summary = '已完成参观成果'
    composed = {}

    class CompletedPlanner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            # 仅隔离任务决策；完成事件、租约、Store 和成果读取均使用原实现。
            composed.update(turn_id=request['turn_id'], model_request_id=execution_control.model_request_id,
                input=request['input'], markdown=summary, changes=[], fallback_new=False)
            store.get_or_create_immutable_payload(request['turn_id'], COMPOSITION, composed)
            return {'type':'complete', 'summary':summary}

    runtime = SynchronousAIRuntime(planner=CompletedPlanner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = freeze_turn_request('project.task', turn_id='turn-projection-t13-2',
        session_id='session-projection-t13-2', operation_id='op-projection-t13-2',
        idempotency_key='projection-t13-2', project_id='alpha', created_at='2026-10-08T00:00:00Z',
        text=json.dumps({'task':'完成参观成果', 'outcome_input':{
            'document_id':'document-original', 'continuation_policy':'@2'}}, ensure_ascii=False),
        capabilities=[], privacy={'mode':'remote_allowed', 'allow_remote':True, 'pii':'possible',
            'consent_refs':['crp://default/model-settings/generation'], 'retention':'session'})
    assert runtime.submit_turn(request).status == 'completed'
    before_events = tuple(store.events_after(request['turn_id']))
    before_result = store.get_immutable_payload(request['turn_id'], COMPOSITION)
    projection = TaskRuntimeProjection(runtime=runtime, turn_store=store,
        composition=None, organization=SimpleNamespace())
    assert projection.read(request['turn_id'], 'alpha') == {
        'status':'completed', 'summary':summary, 'outcome':composed}
    with pytest.raises(RuntimeError, match='research_scope_changed'):
        projection.read(request['turn_id'], 'another-project')
    assert tuple(store.events_after(request['turn_id'])) == before_events
    assert store.get_immutable_payload(request['turn_id'], COMPOSITION) == before_result
