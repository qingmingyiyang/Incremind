from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import socket

import pytest

from backend.recognition import RecognitionConflict
from tests.memory_app.v2.test_task_attachment_input import originals, planner_messages, Decision
from tests.memory_app.v2.test_task_attachment_projection import organization


def test_total_input_reservation_rejects_an_observed_overrun():
    from backend.memory_app.v2.task_attachment_input import remaining_agent_input
    assert remaining_agent_input(1500, 1250) == 250
    assert remaining_agent_input(1500, 1500) == 0
    with pytest.raises(RecognitionConflict, match='budget exceeded'):
        remaining_agent_input(1500, 1501)


def test_real_child_allocation_restricts_model_context_windows(originals, tmp_path):
    from backend.memory_app.v2.task_attachment_input import agent_input_budget, fit_task_attachment_messages, input_tokens
    records, context = originals
    turns, composition, _, children = organization(tmp_path, records, context)
    for child in children:
        budget = agent_input_budget(turns, composition.store, child.request)
        assert budget['input_token_limit'] == budget['remaining_input_tokens'] == 2000
        selected, messages = fit_task_attachment_messages(records, None, context, '预算分别多少',
            planner_messages(context), {'window': 8000, 'reserve': 200}, response_model=Decision,
            agent_budget=budget)
        assert all(row['content']['windows'] for row in selected['originals'])
        assert input_tokens(messages, Decision) <= 2000
        with pytest.raises(RecognitionConflict, match='budget'):
            fit_task_attachment_messages(records, None, context, '预算分别多少',
                [{'role': 'system', 'content': '固定指令。' * 400}, *planner_messages(context)],
                {'window': 8000, 'reserve': 200}, response_model=Decision, agent_budget=budget)


def test_native_run_reserves_unknown_input_across_requests_and_readonly_owners(originals, tmp_path):
    from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
    from backend.memory_app.kernel.agent_runtime_composition import build_agent_runtime_composition
    from backend.memory_app.kernel.product_routing import ProductGenerationRouting
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.v2.task_attachment_input import (
        PreparedAttachmentInput, agent_input_budget, fit_task_attachment_messages, validate_task_attachment_inputs,
    )
    from backend.recognition.kernel_read_facts import authority_facts
    from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry, SynchronousAIRuntime
    from core.ai_kernel.turn_kinds import freeze_turn_request
    from backend.memory_app.part_context_binding import bind_context

    records, context = originals
    turns = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    registry = ScopedCapabilityRegistry()
    composition = build_agent_runtime_composition(runtime_root=tmp_path, session_store=turns,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(registry)))
    profile = composition.profiles.get('main.orchestrator')
    composition.profiles.update(replace(profile, revision=profile.revision + 1,
        budget_limit=replace(profile.budget_limit, input_tokens=1500)), expected_revision=profile.revision)
    models = ModelConfiguration(records, tmp_path)
    models.update('generation', {'provider': 'openai', 'base_url': 'http://127.0.0.1:9/v1',
        'model': 'qwen2.5-1.5b-instruct', 'enabled': True, 'allow_remote': False, 'expected_revision': 0})
    receiver, peer = socket.socketpair()
    receiver.settimeout(0.05)
    dispatched, snapshots, invocations = [], [], []

    class DomainBudgetBoundary:
        def plan(self, request, events, capabilities, payloads, execution_control):
            budget = agent_input_budget(turns, composition.store, request)
            snapshots.append(budget)
            selected, projected = fit_task_attachment_messages(records, None, context, '预算分别多少',
                planner_messages(context), {'window': 1100, 'reserve': 100}, response_model=Decision,
                agent_budget=budget)
            route = ProductGenerationRouting(models, turns).acquire(request=request,
                project_id='alpha', project_profile_id='project-native', project_profile_revision=1,
                boundary_profile_id='boundary-native', boundary_profile_revision=1, capability_ids=[])
            configuration = route.payload['configuration']
            execution_control.model_call_routed(snapshot_ref=route.payload_ref, snapshot_revision=route.revision,
                prompt_cache_scope_identity=route.payload['prompt_cache_scope_identity'],
                provider=configuration['provider'], model=configuration['model'], execution_location='local_loopback')
            execution_control.model_call_started(provider=configuration['provider'], model=configuration['model'])
            prepared = PreparedAttachmentInput(records=records, turns=turns, agents=composition.store,
                root_request=request, request=request,
                context_row=records.read('v2_part_contexts', request['turn_id']), selected=selected,
                messages=projected, routing_snapshot=route.generation_binding(),
                limits={'window': 1100, 'reserve': 100}, response_model=Decision,
                execution_control=execution_control,
                validate=lambda: validate_task_attachment_inputs(records, turns, composition.store, request))
            handle = prepared.wire_attempt_sink.begin_model_wire_attempt()
            dispatched.append(handle.dispatch)
            active = agent_input_budget(turns, composition.store, request)
            assert active['attempt_reservations'][-1]['usage_status'] == 'unknown'
            assert active['attempt_reservations'][-1]['terminal_ref'] is None
            assert active['reserved_input_tokens'] > budget['reserved_input_tokens']

            def receive_timeout():
                invocations.append(handle.dispatch['attempt_id'])
                try:
                    receiver.recv(1)
                except TimeoutError:
                    handle.failed_transport(error_code='ai.domain_receive_timeout')
                    raise
                pytest.fail('保持连接的本机对端应触发接收超时')

            with pytest.raises(TimeoutError):
                handle.invoke_wire(receive_timeout)
            return {'type': 'tool', 'capability_id': 'agent.list', 'arguments': {'include_messages': False}}

    runtime = SynchronousAIRuntime(planner=DomainBudgetBoundary(), registry=registry,
        events=turns, payloads=turns, state=turns)
    composition.bind_runtime(runtime)
    request = freeze_turn_request('project.task', turn_id='turn-' + 'a' * 32, session_id='session-native',
        operation_id='op-native-budget-probe', idempotency_key='key-native-budget', project_id='alpha',
        created_at='2026-10-25T00:00:00Z',
        text=json.dumps({'task': '预算分别多少', 'dependency_context': context['text']}, ensure_ascii=False),
        privacy={'mode': 'local_only', 'allow_remote': False, 'pii': 'possible',
            'consent_refs': [], 'retention': 'session'}, capabilities=['agent.list'], template_version=3)
    bind_context(records, request, context)
    main = composition.coordinator.accept_and_register_main(request)
    now = datetime.now(timezone.utc)
    lease = runtime.try_acquire_run_lease(request['turn_id'], 'attachment-budget',
        now=now, stale_after=now + timedelta(seconds=30))
    assert lease is not None
    assert runtime.try_acquire_run_lease(request['turn_id'], 'other-budget-owner',
        now=now, stale_after=now + timedelta(seconds=30)) is None
    try:
        receipt = runtime.run_accepted_turn(main.request['turn_id'], lease)
    finally:
        runtime.release_strict_run_lease(lease)
        receiver.close()
        peer.close()
    assert receipt.status == 'failed'
    assert len(dispatched) == len(invocations) == 2, turns.events_after(request['turn_id'])[-1]['data']
    assert len({row['model_request_id'] for row in dispatched}) == 2
    assert len(snapshots) == 3
    assert snapshots[-1]['remaining_input_tokens'] < snapshots[1]['remaining_input_tokens']
    assert all(row['usage_status'] == 'unknown' for row in snapshots[-1]['attempt_reservations'])
    assert all(row['terminal_ref'] for row in snapshots[-1]['attempt_reservations'])
    proofs = validate_task_attachment_inputs(records, turns, composition.store, main.request)
    assert len(proofs) == 2
    assert all(proof['agent_budget']['budget_snapshot_ref'] == main.run.budget_snapshot_ref for proof in proofs)
    facts, agents = authority_facts(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    assert validate_task_attachment_inputs(records, facts, agents, main.request) == proofs
    with records.begin() as tx:
        assert validate_task_attachment_inputs(tx, facts, agents, main.request) == proofs
    print('agent-input-reservations', json.dumps([row['remaining_input_tokens'] for row in snapshots]))
