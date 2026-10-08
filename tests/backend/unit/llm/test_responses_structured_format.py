"""Native schema translation over real HTTP, gateway and SQLite model attempts."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import httpx
import pytest
from openai.lib._parsing._responses import type_to_text_format_param

from backend.memory_app.v2.policies.retry import decide
from backend.memory_app.v2.route import RouteOutput
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, ModelRetryControl
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.openai_responses import ResponsesCompletion, ProviderBackgroundOptions
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from tests.backend.unit.llm.test_provider_background import LocalProviderTransport
from tests.memory_app.v2.test_route import good_output


USAGE = {'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}


class StructuredProvider:
    def __init__(self, *, invalid=False):
        self.calls, self.bodies = [], []
        value = good_output()
        if invalid:
            value['parts'][0]['intent'] = 'unsupported'
        self.text = json.dumps(value, ensure_ascii=False)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.calls.append(('POST', self.path))
                owner.bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                events = [
                    {'type': 'response.created', 'sequence_number': 0,
                        'response': {'id': 'resp_schema', 'status': 'in_progress'}},
                    {'type': 'response.output_text.delta', 'sequence_number': 1, 'delta': owner.text},
                    {'type': 'response.completed', 'sequence_number': 2,
                        'response': {'id': 'resp_schema', 'status': 'completed',
                            'output': [{'type': 'message', 'content': [
                                {'type': 'output_text', 'text': owner.text}]}], 'usage': USAGE}},
                ]
                raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                owner.calls.append(('GET', self.path))
                self.send_error(405)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def run_format(tmp_path, provider, *, response_format=RouteOutput, subscription=False,
               background=False, streaming=False):
    store = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    clients, responses, result = [], [], {}

    def factory():
        client = httpx.Client(transport=LocalProviderTransport(provider.base), trust_env=False,
            follow_redirects=False, event_hooks={'response': [responses.append]})
        clients.append(client)
        return client

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            execution_control.model_call_routed(
                snapshot_ref='crp://session/test/turn-model-routing-snapshot-v1/frozen',
                snapshot_revision='a' * 64, prompt_cache_scope_identity='b' * 64,
                provider='openai', model='synthetic', execution_location='local_loopback')
            execution_control.model_call_started(provider='openai', model='synthetic')

            class Lease:
                def finish(self, *_args, **_kwargs):
                    return

            declaration = ModelCapabilities(background_resume=background)
            native = ResponsesCompletion(api_base=None if subscription else provider.base,
                capabilities=declaration)
            control = ModelRetryControl(policy=decide, checkpoint=execution_control.checkpoint,
                owned_client_factory=factory)
            gateway = LiteLLMCompletionGateway(provider='openai', model='synthetic',
                base_url=provider.base, api_key='synthetic-only', completion_fn=native,
                capabilities=declaration,
                background_options=ProviderBackgroundOptions(
                    checkpoint=execution_control.checkpoint, resume=lambda _value: False) if background else None,
                egress_guard=lambda *_args: (execution_control.checkpoint() or Lease()))
            options = {'wire_attempt_sink': execution_control, 'retry_control': control, 'timeout': 4}
            messages = [{'role': 'user', 'content': 'Synthetic route'}]
            if response_format is RouteOutput:
                if streaming:
                    output, usage = gateway.stream_structured_with_usage(messages,
                        response_model=RouteOutput, on_delta=lambda _text: None,
                        validate_current=execution_control.checkpoint, **options)
                else:
                    output, usage, _ = gateway.complete_structured_with_usage(messages,
                        response_model=RouteOutput, retries=0, max_wire_attempts=1, **options)
            else:
                raw, usage, _ = gateway.complete_text_with_usage(messages,
                    response_format=response_format, **options)
                output = RouteOutput.model_validate_json(raw)
            result.update(output=output, usage=usage)
            execution_control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': 'Native format validated'}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = json.loads((Path(__file__).resolve().parents[4] /
        'core-contracts/ai/fixtures/turn-request/valid-project-answer.json').read_text(encoding='utf-8'))
    receipt = runtime.submit_turn(request)
    events = tuple(runtime.events_after(receipt.turn_id))
    terminals = [store.get(event['data']['receipt_ref']) for event in events
        if event['type'] == 'model.attempt.terminal']
    return receipt, events, terminals, clients, responses, result


def assert_wire(provider, events, terminals, clients, responses, *, sent=True):
    assert provider.calls == ([('POST', '/v1/responses')] if sent else [])
    assert len([event for event in events if event['type'] == 'model.attempt.dispatched']) == 1
    assert len(terminals) == 1
    if sent:
        assert terminals[0]['status'] == 'succeeded' and terminals[0]['usage'] == USAGE
        assert len(clients) == len(responses) == 1
        assert all(client.is_closed for client in clients) and all(response.is_closed for response in responses)
    else:
        assert terminals[0]['status'] == 'failed_transport'
        assert terminals[0].get('usage') is None and clients == responses == []


@pytest.mark.parametrize('subscription,background,streaming', [
    (False, False, False), (False, True, False), (True, False, False),
])
def test_real_route_class_schema_is_strict_and_validated_after_one_native_attempt(
        tmp_path, subscription, background, streaming):
    provider = StructuredProvider()
    try:
        receipt, events, terminals, clients, responses, result = run_format(tmp_path, provider,
            subscription=subscription, background=background, streaming=streaming)
        assert receipt.status == 'completed'
        assert result['output'].model_dump() == good_output() and result['usage'] == USAGE
        assert_wire(provider, events, terminals, clients, responses)
        body = provider.bodies[0]
        assert body['text']['format'] == type_to_text_format_param(RouteOutput)
        fmt = body['text']['format']
        assert fmt['strict'] is True and fmt['name'] == 'RouteOutput'
        assert fmt['schema']['required'] == ['parts'] and fmt['schema']['additionalProperties'] is False
        part = fmt['schema']['$defs']['RoutePart']
        assert part['required'] == ['intent', 'span', 'instruction', 'depends_on']
        assert part['properties']['intent']['enum'] == ['remember', 'ask', 'do', 'inspiration']
        assert part['properties']['instruction']['anyOf'] == [
            {'maxLength': 200, 'type': 'string'}, {'type': 'null'}]
        assert body.get('store') is (True if background else False if subscription else None)
        assert body.get('background') is (True if background else None)
        assert len([event for event in events if event['type'] == 'model.completed']) == 1
    finally:
        provider.close()


@pytest.mark.parametrize('response_format', [None, {'type': 'json_object'},
    {'type': 'json_schema', 'json_schema': {'name': 'Provided', 'strict': True,
        'schema': {'type': 'object', 'required': ['parts'], 'properties': {'parts': {'type': 'array'}}}}},
])
def test_existing_dictionary_and_missing_format_keep_exact_wire_shape(tmp_path, response_format):
    provider = StructuredProvider()
    original = deepcopy(response_format)
    try:
        receipt, events, terminals, clients, responses, result = run_format(
            tmp_path, provider, response_format=response_format)
        assert receipt.status == 'completed' and result['output'].model_dump() == good_output()
        assert_wire(provider, events, terminals, clients, responses)
        body = provider.bodies[0]
        if response_format is None:
            assert 'text' not in body
        else:
            expected = ({'type': 'json_schema', **response_format['json_schema']}
                if response_format['type'] == 'json_schema' else response_format)
            assert body['text'] == {'format': expected}
        assert response_format == original and 'store' not in body and 'background' not in body
    finally:
        provider.close()


def test_native_schema_does_not_bypass_original_route_validator(tmp_path):
    provider = StructuredProvider(invalid=True)
    try:
        receipt, events, terminals, clients, responses, result = run_format(tmp_path, provider)
        assert receipt.status == 'failed' and result == {}
        assert_wire(provider, events, terminals, clients, responses)
        assert not any(event['type'] == 'model.completed' for event in events)
    finally:
        provider.close()


def test_unsupported_format_fails_before_native_http_with_safe_type_error(tmp_path):
    provider = StructuredProvider()
    try:
        native = ResponsesCompletion(api_base=provider.base)
        with pytest.raises(TypeError, match='unsupported_response_format'):
            native(model='synthetic', messages=[{'role': 'user', 'content': 'Synthetic route'}],
                api_key='synthetic-only', response_format=object(), timeout=4)
        assert provider.calls == []
    finally:
        provider.close()
