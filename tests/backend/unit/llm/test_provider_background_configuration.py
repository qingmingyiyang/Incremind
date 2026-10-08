"""A proven native transport crosses the actual model configuration wrapper."""
import httpx
import pytest
import json
from pathlib import Path

from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import ModelRetryControl
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion, ProviderBackgroundOptions
from backend.memory_app.v2.policies.retry import decide
from core.storage_provider import SQLiteStructuredRecordStore
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from tests.backend.unit.llm.test_model_transport_watchdog import Answer
from tests.backend.unit.llm.test_provider_background import BackgroundProvider, LocalProviderTransport


def configured(tmp_path, provider, *, proven):
    native = ResponsesCompletion(api_base=provider.base,
        capabilities=ModelCapabilities(background_resume=proven))
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'),
        tmp_path, InMemorySecretStore(), completion_fn=native)
    models.update('generation', {'base_url': provider.base, 'model': 'synthetic',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    return models


@pytest.mark.parametrize('enabled', [False, True])
def test_configuration_keeps_native_validation_and_one_owned_response(tmp_path, enabled):
    provider = BackgroundProvider('resume' if enabled else 'full')
    clients, responses, decisions, deltas = [], [], [], []

    def factory():
        client = httpx.Client(transport=LocalProviderTransport(provider.base), follow_redirects=False)
        client.event_hooks['response'].append(responses.append)
        clients.append(client)
        return client

    def resume(value):
        decisions.append(value)
        return len(decisions) == 1

    try:
        models = configured(tmp_path, provider, proven=True)
        options = ProviderBackgroundOptions(checkpoint=lambda: None, resume=resume)
        gateway = models._generation_gateway(models.snapshot('generation'),
            **({'background_options': options} if enabled else {}))
        observed = {}
        store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')

        class Planner:
            def plan(self, request, events, capabilities, payloads, execution_control):
                execution_control.model_call_routed(
                    snapshot_ref='crp://session/test/turn-model-routing-snapshot-v1/frozen',
                    snapshot_revision='a' * 64, prompt_cache_scope_identity='b' * 64,
                    provider='openai', model='synthetic', execution_location='local_loopback')
                execution_control.model_call_started(provider='openai', model='synthetic')
                result, usage = gateway.stream_structured_with_usage(
                    [{'role': 'user', 'content': 'synthetic'}], response_model=Answer,
                    on_delta=deltas.append, validate_current=execution_control.checkpoint, timeout=20,
                    wire_attempt_sink=execution_control,
                    retry_control=ModelRetryControl(policy=decide, checkpoint=execution_control.checkpoint,
                        owned_client_factory=factory, wait=lambda _: None, jitter=lambda: 0))
                observed.update(result=result, usage=usage)
                execution_control.model_call_completed(usage=usage)
                return {'type': 'complete', 'summary': result.answer}

        runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
            events=store, payloads=store, state=store)
        request = json.loads((Path(__file__).resolve().parents[4] /
            'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
        receipt = runtime.submit_turn(request)
        assert receipt.status == 'completed'
        result, usage = observed['result'], observed['usage']
        assert result.answer == 'hello'
        assert usage == {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}
        assert sum(method == 'POST' for method, _ in provider.calls) == 1
        assert sum(method == 'GET' for method, _ in provider.calls) == int(enabled)
        events = tuple(runtime.events_after(receipt.turn_id))
        assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == 1
        terminals = [store.get(event['data']['receipt_ref']) for event in events
            if event['type'] == 'model.attempt.terminal']
        assert len(terminals) == 1 and terminals[0]['status'] == 'succeeded'
        assert terminals[0]['usage'] == usage
        assert ''.join(deltas) == 'hello'
        if enabled:
            assert provider.bodies[0]['store'] is True and provider.bodies[0]['background'] is True
            assert len(decisions) == 1
        else:
            assert 'store' not in provider.bodies[0] and 'background' not in provider.bodies[0]
            assert decisions == []
        assert all(client.is_closed for client in clients)
        assert all(response.is_closed for response in responses)
    finally:
        provider.close()


def test_configuration_cannot_turn_an_unproven_transport_into_background(tmp_path):
    provider = BackgroundProvider('full')
    try:
        models = configured(tmp_path, provider, proven=False)
        with pytest.raises(ValueError, match='provider_background_adapter_invalid'):
            models._generation_gateway(models.snapshot('generation'), background_options=
                ProviderBackgroundOptions(checkpoint=lambda: None, resume=lambda _: True))
        assert provider.calls == [] and provider.bodies == []
    finally:
        provider.close()
