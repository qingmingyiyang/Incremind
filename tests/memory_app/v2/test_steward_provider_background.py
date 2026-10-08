"""Steward background use keeps every real organization owner distinct."""
from copy import deepcopy
import sqlite3
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.kernel.provider_store_binding import BINDINGS, steward_provider_store_activation
from backend.memory_app.turn_routing import SNAPSHOT_KIND
from backend.shared.llm.openai_responses import ResponsesCompletion
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_main_provider_background import (
    application, env, store_facts, submit, toggle,
)


@pytest.mark.parametrize('phase', ['before_output', 'body'])
def test_real_do_steward_main_and_worker_keep_separate_physical_owners(env, phase):
    env.provider.phase = phase
    toggle(env)
    choose(env.models, 'quick')
    with TestClient(application(env)) as http:
        identity, _result = submit(env, http, 'do')
        composition, store = env.application.state.agent_runtime_composition, env.application.state.ai_turn_store
        runs = composition.store.list_runs(project_id='project-a')
        assert sorted((run.role, run.profile_id) for run in runs) == [
            ('main', 'main.orchestrator'), ('subagent', 'steward.scheduler'), ('subagent', 'subagent.worker')]
        owners = {('main' if run.role == 'main' else
            'steward' if run.profile_id == 'steward.scheduler' else 'worker'): run for run in runs}
        assert owners['main'].turn_id == identity
        assert owners['steward'].parent_run_id == owners['worker'].parent_run_id == owners['main'].run_id
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert [(call['kind'], call['body']['model']) for call in posts] == [
            ('steward', 'main'), ('worker', 'main'), ('main', 'main')]
        assert all(post['body'].get('store') is True and post['body'].get('background') is True
            for post in posts if post['kind'] in {'steward', 'main'})
        assert 'store' not in posts[1]['body'] and 'background' not in posts[1]['body']
        gets = [call for call in env.provider.calls if call['method'] == 'GET']
        assert len(gets) == 2 and len({call['identity'] for call in gets}) == 2
        assert [call['after'] for call in gets] == [0 if phase == 'before_output' else 1] * 2
        physical = []
        for name in ('steward', 'worker', 'main'):
            run = owners[name]
            dispatches, terminals, checkpoints, effects = store_facts(store, run.turn_id)
            assert len(dispatches) == len(terminals) == 1
            physical.append(dispatches[0]['attempt_id'])
            assert dispatches[0]['turn_id'] == run.turn_id and dispatches[0]['model_id'] == 'main'
            assert terminals[0]['status'] == 'succeeded'
            assert terminals[0]['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
            assert effects[dispatches[0]['attempt_id']] == 'SETTLED_OK'
            events = store.events_after(run.turn_id)
            assert events[-1]['type'] == 'turn.completed'
            receipts = [store.get(event['data']['receipt_ref']) for event in events
                if event['type'] == 'model.completed' and event['data'].get('receipt_ref')]
            assert len(receipts) == 1 and receipts[0]['model_call_purpose'] == 'primary'
            assert receipts[0]['model_id'] == 'main'
            assert receipts[0]['usage'] == {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}
            if name == 'worker':
                assert env.records.read(BINDINGS, run.turn_id) is None and checkpoints == []
            else:
                binding = env.records.read(BINDINGS, run.turn_id)
                assert binding.revision == 1 and binding.payload['enabled'] is True
                assert binding.payload['auxiliary'] is None and binding.payload['configuration']['model'] == 'main'
                assert binding.payload['kind'] == ('agent.steward.plan' if name == 'steward' else 'project.task')
                get = gets[0 if name == 'steward' else 1]
                sequences = [0, 1, 2] if phase == 'before_output' else [0, 1, 2, 3]
                assert [item['cursor'] for item in checkpoints] == [
                    {'response_id': get['identity'], 'sequence_number': number} for number in sequences]
                assert all(item['dispatch'] == dispatches[0] and item['run_lease']['owner_id']
                    and item['effect_lease'] for item in checkpoints)
            capability = 'agent.plan' if name == 'steward' else 'document.draft.propose' if name == 'worker' else None
            if capability:
                assert sum(event['type'] == 'tool.intent.recorded' for event in events) == 1
                assert sum(event['type'] == 'tool.completed' for event in events) == 1
        assert len(set(physical)) == 3
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def steward_run(env):
    runs = env.application.state.agent_runtime_composition.store.list_runs(project_id='project-a')
    found = [run for run in runs if run.profile_id == 'steward.scheduler']
    assert len(found) == 1 and found[0].role == 'subagent'
    return found[0]


def do_until_terminal(env, http):
    response = http.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '写一段总结'})
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = monotonic() + 25
    while monotonic() < deadline:
        view = http.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]
        if view['receipt']['do']['state'] in {'done', 'failed', 'partial'}:
            return view
        sleep(.05)
    pytest.fail('real Do did not reach a terminal product projection')


@pytest.mark.parametrize('control', ['off', 'undeclared'])
def test_steward_first_unsupported_choice_is_off_and_worker_is_never_adopted(env, control):
    if control == 'undeclared':
        toggle(env)
        env.models._completion_fn = ResponsesCompletion(api_base=env.provider.base)
    choose(env.models, 'quick')
    with TestClient(application(env)) as http:
        _, _result = submit(env, http, 'do')
        posts = [call for call in env.provider.calls if call['method'] == 'POST']
        assert [(call['kind'], call['body']['model']) for call in posts] == [
            ('steward', 'main'), ('worker', 'main'), ('main', 'main')]
        assert all('store' not in call['body'] and 'background' not in call['body'] for call in posts)
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        store = env.application.state.ai_turn_store
        child = steward_run(env)
        row = env.records.read(BINDINGS, child.turn_id)
        assert row.revision == 1 and row.payload['enabled'] is False
        assert all(row.payload[field] is None for field in ('parent', 'configuration', 'adapter', 'auxiliary'))
        assert len(store_facts(store, child.turn_id)[0]) == len(store_facts(store, child.turn_id)[1]) == 1
        frozen = store.get_request(child.turn_id)
        saved = store.get_immutable_payload(child.turn_id, SNAPSHOT_KIND)
        assert steward_provider_store_activation(env.models, env.records, frozen, saved[1]) is None
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('revocation', ['generation', 'remote', 'selection', 'private'])
def test_steward_cursor_get_rechecks_current_configuration_and_parent_privacy(env, revocation):
    toggle(env)
    application(env)
    changed = []
    def revoke():
        if changed or env.provider.calls[-1].get('kind') != 'steward':
            return
        child = steward_run(env)
        store = env.application.state.ai_turn_store
        deadline = monotonic() + 8
        while monotonic() < deadline and not store_facts(store, child.turn_id)[2]:
            sleep(.01)
        assert store_facts(store, child.turn_id)[2]
        frozen = store.get_request(child.turn_id)
        parent = env.application.state.agent_runtime_composition.store.get_run(child.parent_run_id)
        parent_request = store.get_request(parent.turn_id)
        assert frozen['scope'] == parent_request['scope'] and parent_request['desired_outcome'] == 'project.task'
        assert frozen['scope']['project_id'] == 'project-a'
        changed.append(child.turn_id)
        if revocation in {'generation', 'remote'}:
            cfg = env.models.public()['generation']
            env.models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
                'model': 'different' if revocation == 'generation' else cfg['model'],
                'allow_remote': revocation != 'remote', 'expected_revision': cfg['revision']})
        elif revocation == 'selection':
            toggle(env, False)
        else:
            from backend.memory_app.privacy_policy import set_private_project
            set_private_project(env.records, 'project-a', True, 0)
    env.provider.after_emitted = revoke
    with TestClient(env.application) as http:
        do_until_terminal(env, http)
        assert env.provider.callback_errors == []
        child = steward_run(env)
        assert changed == [child.turn_id]
        posts = [call for call in env.provider.calls if call['method'] == 'POST' and call['kind'] == 'steward']
        assert len(posts) == 1 and posts[0]['body']['store'] is True and posts[0]['body']['background'] is True
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        dispatches, terminals, checkpoints, effects = store_facts(env.application.state.ai_turn_store, child.turn_id)
        assert len(dispatches) == len(terminals) == 1 and checkpoints
        assert terminals[0]['status'] == 'failed_transport'
        assert terminals[0]['usage'] is None and terminals[0]['usage_status'] == 'unavailable'
        assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


@pytest.mark.parametrize('failure', ['observer', 'close'])
def test_steward_storage_or_owned_close_failure_never_creates_another_post(env, failure):
    from types import SimpleNamespace
    from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
    toggle(env)
    application(env)
    if failure == 'close':
        env.provider.fail_close = True
    with TestClient(env.application) as http:
        get_or_build_ai_runtime(SimpleNamespace(app=env.application), SimpleNamespace(root_dir=env.root))
        if failure == 'observer':
            with sqlite3.connect(env.application.state.ai_turn_store._path) as connection:
                connection.execute("CREATE TRIGGER synthetic_steward_observer BEFORE INSERT ON ai_turn_immutable_payloads "
                    "WHEN NEW.kind LIKE 'model-provider-checkpoint-%' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        do_until_terminal(env, http)
        child = steward_run(env)
        posts = [call for call in env.provider.calls if call['method'] == 'POST' and call['kind'] == 'steward']
        assert len(posts) == 1 and posts[0]['body']['background'] is True
        assert not any(call['method'] == 'GET' for call in env.provider.calls)
        dispatches, terminals, checkpoints, effects = store_facts(env.application.state.ai_turn_store, child.turn_id)
        assert len(dispatches) == len(terminals) == 1
        assert terminals[0]['status'] == 'failed_transport' and terminals[0]['usage'] is None
        assert effects[dispatches[0]['attempt_id']] == 'UNKNOWN'
        if failure == 'observer':
            assert checkpoints == []
    assert env.clients and all(client.is_closed for client in env.clients)
    assert env.responses and all(response.is_closed for response in env.responses)


def test_steward_capture_sql_abort_keeps_original_route_without_historical_adoption(env):
    toggle(env)
    application(env)
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER synthetic_steward_binding BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_provider_store_bindings' AND "
            "json_extract(NEW.payload_json,'$.kind')='agent.steward.plan' "
            "BEGIN SELECT RAISE(ABORT,'synthetic binding failure'); END")
    with TestClient(env.application) as http:
        do_until_terminal(env, http)
        child = steward_run(env)
        store = env.application.state.ai_turn_store
        saved = store.get_immutable_payload(child.turn_id, SNAPSHOT_KIND)
        assert saved is not None and env.records.read(BINDINGS, child.turn_id) is None
        assert not any(call.get('kind') == 'steward' for call in env.provider.calls)
        assert store_facts(store, child.turn_id)[:3] == ([], [], [])
        with sqlite3.connect(env.records.database_path) as connection:
            connection.execute('DROP TRIGGER synthetic_steward_binding')
        frozen = store.get_request(child.turn_id)
        assert steward_provider_store_activation(env.models, env.records, frozen, saved[1]) is None
        assert env.records.read(BINDINGS, child.turn_id) is None
        assert store.get_immutable_payload(child.turn_id, SNAPSHOT_KIND) == saved


def test_steward_predicate_alone_cannot_replace_real_identity_or_parent_validation(env):
    from backend.memory_app.kernel.product_routing import ProductGenerationRouting
    toggle(env)
    with TestClient(application(env)) as http:
        submit(env, http, 'do')
        composition, store = env.application.state.agent_runtime_composition, env.application.state.ai_turn_store
        frozen = store.get_request(steward_run(env).turn_id)
        unverified = ProductGenerationRouting(env.models, store, records=env.records, steward_parent_check=lambda _: True)
        assert unverified._provider_store_owner(frozen, ()) is False
        canonical = ProductGenerationRouting(env.models, store, records=env.records,
            agent_binding_verifier=composition.coordinator.verify_agent_binding, steward_parent_check=lambda _: False)
        assert canonical._provider_store_owner(frozen, ()) is False
        trusted = ProductGenerationRouting(env.models, store, records=env.records,
            agent_binding_verifier=composition.coordinator.verify_agent_binding, steward_parent_check=lambda _: True)
        assert trusted._provider_store_owner(frozen, ()) is True
        wrong = deepcopy(frozen)
        wrong['agent_binding']['profile_id'] = 'subagent.worker'
        from backend.memory_app.kernel.agent_coordinator import AgentCoordinatorError
        with pytest.raises(AgentCoordinatorError):
            trusted._provider_store_owner(wrong, ())
        worker = next(run for run in composition.store.list_runs(project_id='project-a') if run.profile_id == 'subagent.worker')
        assert trusted._provider_store_owner(store.get_request(worker.turn_id), ()) is False


def test_steward_first_off_selection_never_adopts_a_later_toggle(env):
    with TestClient(application(env)) as http:
        submit(env, http, 'do')
        child = steward_run(env)
        store = env.application.state.ai_turn_store
        frozen = store.get_request(child.turn_id)
        saved = store.get_immutable_payload(child.turn_id, SNAPSHOT_KIND)
        row = env.records.read(BINDINGS, child.turn_id)
        assert row.revision == 1 and row.payload['enabled'] is False
        calls = deepcopy(env.provider.calls)
        toggle(env)
        assert env.settings.get()['enabled'] is True
        assert steward_provider_store_activation(env.models, env.records, frozen, saved[1]) is None
        assert env.records.read(BINDINGS, child.turn_id) == row
        assert store.get_immutable_payload(child.turn_id, SNAPSHOT_KIND) == saved
        assert env.provider.calls == calls and not any(call['method'] == 'GET' for call in calls)
