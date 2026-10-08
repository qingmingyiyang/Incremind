"""Actual rewrite, bookshelf and answer wires preserve the original date meaning."""
import asyncio
import json
from unittest.mock import patch

from backend.memory_app.v2.bookshelf import consult_bookshelf
from backend.memory_app.v2.multi_query import expand_plan
from backend.memory_app.v2.policies import override
from backend.memory_app.recall_preferences import set_preference
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_ask import env as _env, ask
from tests.memory_app.v2.test_insight_validity import publish, supersede, MARCH, JUNE, TODAY

env = _env


def test_rewrite_carries_original_time_scope_and_excludes_future_variants(env):
    old = publish(env, '春港展会北厅小型展')
    future = publish(env, '春港展会南厅大型展', JUNE)
    supersede(env, old, future)
    original = env.model.complete
    wires = []

    def complete(messages, *, wire_attempt_sink=None, **kwargs):
        if '"queries"' not in messages[0]['content']:
            return original(messages, wire_attempt_sink=wire_attempt_sink, **kwargs)
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            wires.append(messages)
            attempt.succeeded(usage={'input_tokens': 1, 'output_tokens': 1}, cache_observation=None)
            return json.dumps({'queries': ['春港展会南厅大型展']}, ensure_ascii=False), {'usage': {'total_tokens': 2}}
        return attempt.invoke_wire(wire)

    env.model.complete = complete
    question = '春港展会2026年三月时风格预算规模供应商怎么想的？'
    with patch('backend.memory_app.v2.insight_validity.now', return_value=TODAY), override(retrieve='@3', compose='@3'):
        query = env.domains.query
        collected = query.collect_candidates('alpha', question)
        plan = query.prepare_ask('alpha', question, collected=collected)
        async def execute():
            result, _, rewrite = await expand_plan(query, 'alpha', question, plan, collected, turn_id='time-rewrite')
            return {'ids': [c['id'] for c in result['chosen']], 'time_scope': result.get('time_scope'), 'rewrite': rewrite}
        result = asyncio.run(query.answer_turns.run(turn_id='turn-time-rewrite', project='alpha',
            question=question, policy_versions=plan['policy_versions'], operation=execute))
    assert wires and result['rewrite']['used'] is True
    assert result['time_scope'] == collected['time_scope']
    assert set(result['ids']) == {old.id}


def test_bookshelf_append_rejects_future_automatic_forgotten_insight(env):
    old = publish(env, '春港展会北厅小型展')
    future = publish(env, '春港展会预算规模供应商南厅大型展', JUNE)
    supersede(env, old, future)
    set_preference(env.records, WorkScope('local-user', 'alpha'), future.id,
                   recognition_revision=1, preference_revision=0, state='forgotten')
    with env.records.begin() as tx:
        row = tx.read('recognition_recall_preferences', future.id)
        tx.put('recognition_recall_preferences', future.id, {**row.payload, 'by': 'auto'}, expected_revision=row.revision)
        tx.commit()
    question = '春港展会2026年三月时风格预算规模供应商怎么想的？'
    with override(retrieve='@3', compose='@3'):
        query = env.domains.query
        plan = query.prepare_ask('alpha', question)
        assert plan['trace'][-1]['stopped'] is False
        consult_bookshelf(query, 'alpha', question, plan)
    assert {c['id'] for c in plan['chosen']} == {old.id}


def test_real_answer_wire_contains_then_valid_label_once_and_preserves_historical_receipt(env):
    old = publish(env, '春港展会在北厅办小型展')
    future = publish(env, '春港展会改在南厅办大型展', JUNE)
    supersede(env, old, future)
    with override(retrieve='@3', compose='@3'):
        result = ask(env, text='春港展会在2026年三月时怎么想的？')
    assert result.status_code == 200, result.text
    receipt = result.json()['turn']['receipt']['ask']
    assert {c['id'] for c in receipt['citations']} == {old.id}
    assert env.model.messages[-1]['content'].count('当时有效') == 1
    assert '2026-03-12' in env.model.messages[-1]['content']
    saved = env.records.read('workspace_ask_receipts', receipt['egress_receipt_id'])
    third = publish(env, '春港展会改在东厅', TODAY)
    supersede(env, future, third, TODAY)
    assert env.records.read('workspace_ask_receipts', receipt['egress_receipt_id']) == saved
