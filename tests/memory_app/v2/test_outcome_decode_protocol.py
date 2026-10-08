"""原结构化 decoder、真实 gateway 和模型配置包装的安全分类合同。"""
from pathlib import Path
import json
import traceback

import pytest
from pydantic import BaseModel

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.turn_routing import RecognitionModelRoutingSnapshotAuthority
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.json_mode import validate_json_response
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from tests.memory_app.test_governed_generation import Control, Metadata, WireSink


RAW_MARKER = 'R3_SYNTHETIC_RAW_BODY_MUST_NOT_APPEAR'
NORMALIZED_USAGE = {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}


class Summary(BaseModel):
    summary: str


def _cases(*, schema=False):
    cases = [(json.dumps([RAW_MARKER]), 'json_object_required'),
             (RAW_MARKER, 'json_invalid')]
    cases.append((json.dumps({'summary': [RAW_MARKER]}), 'schema_invalid')
                 if schema else ('', 'json_empty'))
    return cases


def _models(root: Path, raw: str, *, reported=True, interrupted=False):
    root.mkdir(parents=True)
    calls, closed = [], []

    def provider(**request):
        calls.append(request.get('stream'))
        assert request.get('stream') is True

        def stream():
            try:
                yield {'choices': [{'delta': {'content': raw}, 'finish_reason': None}]}
                if interrupted:
                    raise ConnectionError(RAW_MARKER)
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}
                       if reported else {}}
            finally:
                closed.append(True)
        return stream()

    records = SQLiteStructuredRecordStore(root / 'models.sqlite3')
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider)
    models.update('generation', {'base_url': 'https://example.test', 'model': 'test-model',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    turns = SQLiteAITurnStore(root / 'turns.sqlite3')
    turns.claim_turn(dict(turn_id='turn-one', session_id='session-one',
        operation_id='operation-one', idempotency_key='key-one'))
    route = RecognitionModelRoutingSnapshotAuthority(models, turns).acquire(
        turn_id='turn-one', project_id='project-a', context_packet_id='packet-one',
        project_profile_id='profile-one', project_profile_revision=1,
        boundary_profile_id='boundary-one', boundary_profile_revision=1,
        capability_ids=('recognition.task.execute',), agent_binding=None, allow_remote=True)
    return models, route, calls, closed


def test_original_decoder_has_fixed_safe_codes_without_completed_wire():
    for raw, expected in _cases():
        with pytest.raises(ValueError) as rejected:
            validate_json_response(raw_text=raw, response_model=Summary)
        error = rejected.value
        assert type(error).__name__ == 'StructuredResponseDecodeError', expected
        assert error.code == expected
        assert error.completed_wire is False and error.usage is None
        assert RAW_MARKER not in str(error)
        assert RAW_MARKER not in ''.join(traceback.format_exception(error))


def test_real_stream_decoder_marks_only_completed_wire_and_keeps_unknown_usage(tmp_path):
    for index, (raw, expected) in enumerate(_cases(schema=True)):
        for reported in (True, False):
            models, _route, calls, closed = _models(tmp_path / f'case-{index}-{reported}', raw,
                reported=reported)
            gateway = models._generation_gateway(models.snapshot('generation'))
            wire, control = WireSink(), Control()
            with pytest.raises(ValueError) as rejected:
                gateway.stream_structured_with_usage([{'role': 'user', 'content': '合成结构检查'}],
                    response_model=Summary, field='summary', on_delta=lambda _text: None,
                    validate_current=control.checkpoint, wire_attempt_sink=wire, max_tokens=100)
            error = rejected.value
            assert type(error).__name__ == 'StructuredResponseDecodeError', expected
            assert error.code == expected and error.completed_wire is True
            assert error.usage == (NORMALIZED_USAGE if reported else {})
            assert calls == [True] and closed == [True]
            assert wire.begin_calls == 1
            assert [name for name, _ in wire.handle.events] == ['invoke', 'succeeded']
            assert wire.handle.events[-1][1]['usage'] == error.usage
            assert RAW_MARKER not in str(error)
            assert RAW_MARKER not in ''.join(traceback.format_exception(error))

    # 看起来像非法 JSON 的前缀不能给未完成的真实 transport 授予恢复资格。
    models, _route, calls, closed = _models(tmp_path / 'interrupted', json.dumps([RAW_MARKER]),
        interrupted=True)
    gateway = models._generation_gateway(models.snapshot('generation'))
    wire = WireSink()
    with pytest.raises(ConnectionError) as stopped:
        gateway.stream_structured_with_usage([{'role': 'user', 'content': '合成运输检查'}],
            response_model=Summary, field='summary', on_delta=lambda _text: None,
            validate_current=Control().checkpoint, wire_attempt_sink=wire, max_tokens=100)
    assert getattr(stopped.value, 'completed_wire', False) is False
    assert getattr(stopped.value, 'usage', None) is None
    assert calls == [True] and closed == [True]
    assert [name for name, _ in wire.handle.events] == ['invoke', 'failed_transport']


def test_real_governed_decode_wrapper_keeps_safe_classification_and_transport_failure(tmp_path):
    for index, (raw, expected) in enumerate(_cases(schema=True)):
        for reported in (True, False):
            models, route, calls, closed = _models(tmp_path / f'case-{index}-{reported}', raw,
                reported=reported)
            metadata, wire = Metadata(), WireSink()
            with pytest.raises(ModelConfigurationError) as rejected:
                models.complete_governed([{'role': 'user', 'content': '合成包装检查'}],
                    routing_snapshot=route.generation_binding(), execution_control=Control(),
                    metadata_sink=metadata, wire_attempt_sink=wire, response_model=Summary,
                    stream_field='summary', on_delta=lambda _text: None, max_tokens=100)
            error = rejected.value
            assert type(error).__name__ == 'ModelResponseDecodeError', expected
            assert error.decode_code == expected and error.completed_wire is True
            assert error.usage == (NORMALIZED_USAGE if reported else {})
            assert calls == [True] and closed == [True]
            assert [name for name, _ in wire.handle.events] == ['invoke', 'succeeded']
            assert wire.handle.events[-1][1]['usage'] == error.usage
            assert [name for name, _ in metadata.events] == ['routed', 'started', 'failed']
            assert RAW_MARKER not in str(error) and 'synthetic-only' not in str(error)
            assert RAW_MARKER not in ''.join(traceback.format_exception(error))

    models, route, calls, closed = _models(tmp_path / 'interrupted', json.dumps([RAW_MARKER]),
        interrupted=True)
    metadata, wire = Metadata(), WireSink()
    with pytest.raises(ModelConfigurationError) as stopped:
        models.complete_governed([{'role': 'user', 'content': '合成运输检查'}],
            routing_snapshot=route.generation_binding(), execution_control=Control(),
            metadata_sink=metadata, wire_attempt_sink=wire, response_model=Summary,
            stream_field='summary', on_delta=lambda _text: None, max_tokens=100)
    assert type(stopped.value).__name__ == 'ModelConfigurationError'
    assert getattr(stopped.value, 'completed_wire', False) is False
    assert getattr(stopped.value, 'usage', None) is None
    assert RAW_MARKER not in str(stopped.value)
    assert RAW_MARKER not in ''.join(traceback.format_exception(stopped.value))
    assert calls == [True] and closed == [True]
    assert [name for name, _ in wire.handle.events] == ['invoke', 'failed_transport']
    assert [name for name, _ in metadata.events] == ['routed', 'started', 'failed']


@pytest.mark.parametrize('completed_wire,usage,expected_error', [
    pytest.param(1, {}, 'invalid_response_decode_completion', id='integer-one-flag'),
    pytest.param(0, None, 'invalid_response_decode_completion', id='integer-zero-flag'),
    pytest.param(None, None, 'invalid_response_decode_completion', id='none-flag'),
    pytest.param(True, None, 'invalid_response_decode_usage', id='completed-none-usage'),
    pytest.param(True, [], 'invalid_response_decode_usage', id='completed-list-usage'),
    pytest.param(True, {'unexpected': 1}, 'invalid_response_decode_usage', id='extra-usage-key'),
    pytest.param(True, {'input_tokens': True}, 'invalid_response_decode_usage', id='bool-token-value'),
    pytest.param(True, {'output_tokens': -1}, 'invalid_response_decode_usage', id='negative-token-value'),
    pytest.param(True, {'total_tokens': 1.5}, 'invalid_response_decode_usage', id='float-token-value'),
    pytest.param(False, {}, 'incomplete_response_decode_has_usage', id='incomplete-empty-usage')
])
def test_decode_carrier_rejects_invalid_completion_and_usage(completed_wire, usage, expected_error):
    from backend.shared.llm.json_mode import StructuredResponseDecodeError

    with pytest.raises(ValueError) as rejected:
        StructuredResponseDecodeError('json_invalid', RAW_MARKER,
            completed_wire=completed_wire, usage=usage)
    # 合法 carrier 也继承 ValueError，因此精确类型才能证明构造前置拒绝。
    assert type(rejected.value) is ValueError
    assert str(rejected.value) == expected_error


def test_decode_mapping_requires_exact_completed_carrier_type():
    from backend.shared.llm.json_mode import StructuredResponseDecodeError
    from backend.memory_app.model_config import ModelResponseDecodeError, _model_request_failure

    class SameFactsDecodeError(StructuredResponseDecodeError):
        """沿原构造器提供相同事实，仅类型不同，不覆盖被测实现。"""

    for usage in ({}, dict(NORMALIZED_USAGE)):
        carrier = StructuredResponseDecodeError('json_invalid', RAW_MARKER,
            completed_wire=True, usage=usage)
        mapped = _model_request_failure(carrier)
        assert type(mapped) is ModelResponseDecodeError
        assert mapped.decode_code == 'json_invalid' and mapped.completed_wire is True
        assert mapped.usage == usage and mapped.usage is not carrier.usage
        assert RAW_MARKER not in str(mapped)

        subclass = SameFactsDecodeError('json_invalid', RAW_MARKER,
            completed_wire=True, usage=usage)
        assert (subclass.code, subclass.completed_wire, subclass.usage) == (
            carrier.code, carrier.completed_wire, carrier.usage)
        rejected = _model_request_failure(subclass)
        assert type(rejected) is ModelConfigurationError
        assert not hasattr(rejected, 'decode_code')
        assert getattr(rejected, 'completed_wire', False) is False
        assert getattr(rejected, 'usage', None) is None
        assert RAW_MARKER not in str(rejected)

    incomplete = StructuredResponseDecodeError('json_invalid', RAW_MARKER,
        completed_wire=False, usage=None)
    rejected = _model_request_failure(incomplete)
    assert type(rejected) is ModelConfigurationError
    assert not hasattr(rejected, 'decode_code')
    assert getattr(rejected, 'completed_wire', False) is False
    assert getattr(rejected, 'usage', None) is None
    assert RAW_MARKER not in str(rejected)
