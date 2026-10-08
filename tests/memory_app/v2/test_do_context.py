import json
import pytest
from backend.memory_app.v2.do_context import packet_context, aggregate_usage
from backend.memory_app.context_adapter import compile_selected
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, _estimate_input_tokens


def test_actual_messages_are_partitioned_once_without_bodies():
    packet = compile_selected('p', [], [], '写 summary 中文', 1, expert_brief='结论 SECRET')
    context = packet_context(packet, {'window': 16000, 'reserve': 7000})
    parts = {row['key']: row for row in context['parts']}
    assert set(parts) == {'instruction', 'insight', 'source', 'expert_brief', 'question'}
    assert sum(row['tokens'] for row in parts.values()) == _estimate_input_tokens(packet['messages'])
    assert parts['expert_brief']['count'] == 1 and parts['expert_brief']['tokens'] > 0
    assert parts['question']['count'] == 1 and parts['question']['tokens'] > 0
    assert parts['insight']['tokens'] == parts['source']['tokens'] == 0
    assert context['window'] == 16000
    assert 'SECRET' not in json.dumps(context) and 'summary' not in json.dumps(context)


def test_gateway_exposes_its_real_configured_limits_without_sending():
    gateway = LiteLLMCompletionGateway(provider='openai-compatible', model='test', base_url='http://localhost:1', api_key="synthetic-test", context_window_tokens=12345, reserved_output_tokens=456)
    assert gateway.input_budget_limits() == {'window': 12345, 'reserve': 456}
    assert gateway.input_budget_limits(max_tokens=7000) == {'window': 12345, 'reserve': 7000}
    assert gateway.input_budget_snapshot() is None


def test_receipt_usage_aggregates_each_turn_call_once_and_keeps_missing_unknown():
    a = {'turn_id': 'a', 'model_request_id': 'r', 'usage': {'input_tokens': 11, 'output_tokens': 3}, 'model_id': 'm'}
    b = {'turn_id': 'b', 'model_request_id': 'r', 'usage': {'prompt_tokens': 7, 'completion_tokens': 4}, 'model_id': 'm'}
    assert aggregate_usage([a, b, a]) == {'input_tokens': 18, 'output_tokens': 7}
    assert aggregate_usage([]) is None
    assert aggregate_usage([a, {**b, 'usage': None}]) is None


def test_packet_scope_is_checked_before_prompt_or_model_reader(tmp_path):
    from core.storage_provider import SQLiteStructuredRecordStore
    from backend.recognition import WorkScope, RecognitionConflict
    from backend.memory_app.v2.do_context import task_context
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    packet = compile_selected('other', [], [], 'PRIVATE', 1)
    with records.begin() as tx:
        tx.put('recognition_context_packets', 'packet-a', packet, expected_revision=0)
        tx.commit()
    class Reader:
        def generation_budget_limits(self, **kwargs):
            raise AssertionError('cross-project model reader called')
    with pytest.raises(RecognitionConflict):
        task_context(records, WorkScope(project_id='p', user_id='user'), {'context_packet_id': 'packet-a'}, {}, Reader(), None)


def test_selected_frozen_recognition_is_counted_without_fetching_latest():
    packet = {'query': '问', 'messages': [{'role': 'system', 'content': '指令'}, {'role': 'user', 'content': '认识中文\n\nTask:\n问'}], 'graph': {'nodes': [{'node_type': 'conclusion', 'metadata': {'content': '认识中文'}}]}}
    context = packet_context(packet)
    parts = {part['key']: part for part in context['parts']}
    assert parts['insight']['count'] == 1 and parts['insight']['tokens'] > 0
    assert sum(part['tokens'] for part in context['parts']) == _estimate_input_tokens(packet['messages'])
    assert context['window'] is None


def test_question_suffix_does_not_remove_matching_text_from_a_recognition():
    content = '认识包含 Task:\n问'
    packet = {'query': '问', 'messages': [{'role': 'user', 'content': content + '\n\nTask:\n问'}], 'graph': {'nodes': [{'node_type': 'conclusion', 'metadata': {'content': content}}]}}
    parts = {part['key']: part for part in packet_context(packet)['parts']}
    assert parts['question']['count'] == parts['insight']['count'] == 1


def test_usage_projection_never_composes_a_runtime_or_reads_another_project():
    from types import SimpleNamespace
    from backend.memory_app.v2 import _LazyOrganization
    empty = _LazyOrganization(SimpleNamespace(state=SimpleNamespace()))
    assert empty.usage('turn-a', 'p') == []
    state = SimpleNamespace(agent_runtime_composition=SimpleNamespace(request_loader=lambda identity: {'scope': {'project_id': 'other'}}), ai_turn_store=object(), ai_runtime=object())
    with pytest.raises(RuntimeError, match='research_scope_changed'):
        _LazyOrganization(SimpleNamespace(state=state)).usage('turn-a', 'p')
