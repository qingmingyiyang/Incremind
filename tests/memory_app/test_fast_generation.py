"""Auxiliary model selection keeps the real configuration and gateway guards."""
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from tests.memory_app.test_governed_generation import Control, Metadata, WireSink, _route


def configured(tmp_path, completion=None):
    calls = []

    def complete(**request):
        calls.append(request)
        if completion is not None:
            completion()
        return {"choices": [{"finish_reason": "stop", "message": {"content": "accepted"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'),
        tmp_path, InMemorySecretStore(), completion_fn=complete)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    return models, calls


def choose(models, model, revision=0):
    return models.update_fast_model(model=model, expected_revision=revision,
        expected_generation_revision=models.public()['generation']['revision'],
        expected_mode_revision=models.generation_mode()['revision'])


def test_fast_model_cas_is_independent_and_real_governed_wire_uses_it(tmp_path):
    models, calls = configured(tmp_path)
    primary = models.records.read('recognition_model_config', 'generation')
    assert choose(models, 'quick')['model'] == 'quick'
    assert models.records.read('recognition_model_config', 'generation') == primary
    with pytest.raises(SQLiteUnitOfWorkConflict):
        choose(models, 'wrong')
    assert models.fast_model()['model'] == 'quick'
    selected = models.for_auxiliary(models.freeze_auxiliary_binding())
    metadata = Metadata()
    output, meta = selected.complete_governed([{'role': 'user', 'content': 'Synthetic'}],
        routing_snapshot=_route(selected), execution_control=Control(), metadata_sink=metadata,
        wire_attempt_sink=WireSink(), purpose='aux')
    assert output == 'accepted'
    assert meta['model'] == 'quick'
    assert calls[0]['model'] == 'openai/quick'
    assert metadata.events[0][0] == 'routed'
    assert metadata.events[0][1]['model'] == 'quick'
    assert metadata.events[0][1]['purpose'] == 'aux'
    assert models.snapshot('generation')['model'] == 'main'


def test_changed_aux_selection_rejects_returned_result_without_another_wire(tmp_path):
    models, calls = configured(tmp_path)
    choose(models, 'quick')
    selected = models.for_auxiliary(models.freeze_auxiliary_binding())
    # The selected view delegates the live provider object, whose callback is
    # a real CAS write after the synthetic wire has begun.
    def complete(**request):
        calls.append(request)
        choose(models, 'other', 1)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'late'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
    models._completion_fn = complete
    route = _route(selected)
    with pytest.raises(ModelConfigurationError):
        selected.complete_governed([{'role': 'user', 'content': 'Synthetic'}], routing_snapshot=route,
            execution_control=Control(), metadata_sink=Metadata(), wire_attempt_sink=WireSink(), purpose='aux')
    assert len(calls) == 1
    assert models.snapshot('generation')['model'] == 'main'


def test_unconfigured_aux_and_frozen_default_keep_main(tmp_path):
    models, calls = configured(tmp_path)
    binding = models.freeze_auxiliary_binding()
    assert binding['model'] is None
    choose(models, 'quick')
    assert models.for_auxiliary(binding) is models
    assert models.fast_model()['configured'] is True
    assert calls == []


def test_fast_metadata_does_not_change_original_public_configuration(tmp_path):
    models, _ = configured(tmp_path)
    primary = models.public()
    choose(models, 'quick')
    assert models.public() == primary
