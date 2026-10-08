"""Real product entry points, with only the provider's first call failing."""
import asyncio

from backend.memory_app.v2.organize_turns import OrganizeTurns
from tests.memory_app.v2.test_fast_aux_calls import fast_env
from tests.memory_app.v2.test_route import env as route_env, TEXT
from tests.memory_app.v2.test_workbench_ask import publish, ask
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_divided_do import _real_kernel_drafts


class TransientFailure(RuntimeError):
    status_code = 503


def fail_first(models):
    original, calls = models._completion_fn, []

    def provider(**values):
        calls.append(values)
        if len(calls) == 1:
            raise TransientFailure('synthetic')
        return original(**values)
    models._completion_fn = provider
    return calls


def receipts(store, identity):
    return [store.get(event['data']['receipt_ref']) for event in store.events_after(identity)
            if event['type'] == 'model.attempt.terminal']


def test_http_answer_retries_within_original_turn(fast_env):
    env = fast_env
    publish(env)
    calls = fail_first(env.model)
    result = ask(env, intent='ask')
    assert result.status_code == 200, result.text
    identity = result.json()['turn']['id']
    store = env.app.state.ai_turn_store
    assert store.get_request(identity)['policy_versions']['retry'] == '@1'
    assert len(calls) == 2
    assert [r['status'] for r in receipts(store, identity)] == ['failed_transport', 'succeeded']
    assert calls[0]['messages'] == calls[1]['messages']
    assert calls[0]['model'] == calls[1]['model']


def test_real_route_retries_with_original_four_second_budget_and_cached_result(route_env):
    service, records, models, _, _ = route_env
    calls = fail_first(models)
    result = service.route(TEXT, project_id='alpha', request_key='retry-route')
    assert result.mode == 'model'
    assert len(calls) == 2
    assert all(0 < r['timeout'] <= 4 for r in calls)
    assert service.store.get_request(result.turn_id)['policy_versions']['retry'] == '@1'
    assert [r['status'] for r in receipts(service.store, result.turn_id)] == ['failed_transport', 'succeeded']
    assert service.route(TEXT, project_id='alpha', request_key='retry-route') == result
    assert len(calls) == 2


def test_real_organize_retries_and_completed_checkpoint_does_not_charge_again(fast_env):
    env = fast_env
    item = asyncio.run(env.domains.intake.add_text({'project_id': 'alpha', 'text': 'Synthetic organization'}))
    calls = fail_first(env.model)
    parameters = dict(root=env.root, records=env.records, models=env.model, item_id=item['id'],
        project_id='alpha', source='Synthetic organization', validate_current=lambda: None)
    options = dict(max_tokens=100, validate_current=lambda: None)
    messages = [{'role': 'user', 'content': 'Synthetic organization'}]
    first = OrganizeTurns(**parameters).complete(messages, **options)
    assert len(calls) == 2
    identity = env.records.list('workspace_organize_steps')[0].payload['turn_id']
    store = OrganizeTurns(**parameters).store
    assert store.get_request(identity)['policy_versions']['retry'] == '@1'
    assert [r['status'] for r in receipts(store, identity)] == ['failed_transport', 'succeeded']
    assert OrganizeTurns(**parameters).complete(messages, **options) == first
    assert len(calls) == 2


def test_real_task_planner_retries_first_wire_and_preserves_all_completed_drafts(do_env):
    http, models = do_env
    calls = fail_first(models)
    _real_kernel_drafts(do_env)
    store = http.app.state.ai_turn_store
    roots = [run for run in http.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
             if run.role == 'main']
    assert len(roots) == 1
    identity = roots[0].turn_id
    assert store.get_request(identity)['policy_versions']['retry'] == '@1'
    failed_identity = next(run.turn_id for run in
        http.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
        if any(row['status'] == 'failed_transport' for row in receipts(store, run.turn_id)))
    attempts = receipts(store, failed_identity)
    assert [r['status'] for r in attempts[:2]] == ['failed_transport', 'succeeded']
    assert calls[0]['messages'] == calls[1]['messages']
    assert calls[0]['model'] == calls[1]['model']
