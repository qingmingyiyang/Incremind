"""Image input uses local providers or an auxiliary, tool-free product Turn."""
from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore
import json
from pathlib import Path
import pytest
from pydantic import BaseModel


@pytest.mark.parametrize('texts, expected, spans', [
    (['一行 😀\r\n第二行'], '一行 😀\r\n第二行', [{'ordinal': 1, 'start': 0, 'end': 9}]),
    (['甲\n## 第2张\n仍是甲', '', '乙'],
     '## 第1张\n\n甲\n## 第2张\n仍是甲\n\n## 第2张\n\n\n\n## 第3张\n\n乙',
     [{'ordinal': 1, 'start': 8, 'end': 20}, {'ordinal': 3, 'start': 40, 'end': 41}]),
    (['', ' '], '', []),
])
def test_image_text_projection_produces_actual_spans_without_parsing_fake_headings(texts, expected, spans):
    from backend.memory_app.v2.policies import image_read
    images = [{'text': text, 'description': ''} for text in texts]
    source, actual = image_read.project_texts(images)
    assert source == expected
    assert actual == spans
    assert image_read.decide(images)['source_text'] == source
    assert [source[span['start']:span['end']] for span in actual] == [text.strip() for text in texts if text.strip()]


def test_vision_defaults_local_without_installation_or_remote_consent(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore())
    row = models.public()['vision']
    assert row['mode'] == 'local'
    assert row['allow_remote'] is False
    assert row['revision'] == 0
    assert records.read('recognition_model_config', 'vision') is None


def test_rapidocr_missing_package_is_unavailable_without_constructing_engine(tmp_path, monkeypatch):
    from core.product_core.rapidocr_provider import RapidOcrImageAdapter
    monkeypatch.setattr('core.product_core.rapidocr_provider.find_spec', lambda name: None)
    adapter = RapidOcrImageAdapter(model_root=tmp_path / 'models')
    assert adapter.status() == 'unavailable'
    assert not (tmp_path / 'models').exists()


def test_image_read_turn_is_auxiliary_and_has_no_tools():
    from core.ai_kernel.turn_kinds import freeze_turn_request
    request = freeze_turn_request('media.image_read', turn_id='vision-1', session_id='session-1',
        operation_id='op-1', idempotency_key='key-1', project_id='alpha',
        created_at='2026-10-04T12:00:00Z', text='识图', privacy={
            'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/model-settings/vision'], 'retention': 'session'})
    assert request['execution_policy']['purpose'] == 'aux'
    assert request['capability_policy']['allowed'] == []
    assert request['context_policy']['include_memory'] is False


def test_vision_uses_existing_structured_gateway_with_image_parts_and_usage(tmp_path):
    class Output(BaseModel):
        text: str
        description: str
    calls = []
    def complete(**request):
        calls.append(request)
        return {'choices': [{'message': {'content': json.dumps({'text': '图片原文',
            'description': '画面说明'})}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 24, 'completion_tokens': 12}}
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'),
        tmp_path, secrets=InMemorySecretStore(), completion_fn=complete)
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': '识图'},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aW1hZ2U='}}]}]
    output, metadata = models.complete_vision(messages, response_model=Output)
    assert output.model_dump() == {'text': '图片原文', 'description': '画面说明'}
    assert metadata['usage'] == {'input_tokens': 24, 'output_tokens': 12, 'total_tokens': 36}
    assert calls[0]['messages'][-1]['content'] == messages[0]['content']
    assert 'synthetic-private-value' not in str(models.public())
    models.update('vision', {'allow_remote': False, 'expected_revision': 1})
    with pytest.raises(ValueError, match='vision_remote_disabled'):
        models.complete_vision(messages, response_model=Output)
    assert len(calls) == 1


def test_image_policy_keeps_order_and_bounds_inferred_description():
    from backend.memory_app.v2.policies.image_read import v1
    parts = [{'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{value}'}}
        for value in ('YWJj', 'ZGVm')]
    assert v1.prepare(parts)[0]['content'][1:] == parts
    result = v1.decide([{'text': '原文一\n第二行', 'description': '说明一'},
        {'text': '原文二', 'description': '说明二'}])
    assert result == {'source_text': '## 第1张\n\n原文一\n第二行\n\n## 第2张\n\n原文二',
        'descriptions': ['说明一', '说明二']}
    assert v1.decide([{'text': '单图原文', 'description': ''}])['source_text'] == '单图原文'
    with pytest.raises(ValueError, match='image_read_output_invalid'):
        v1.decide([{'text': '原文', 'description': '长' * 201}])


def test_image_freeze_is_byte_identical_and_keeps_source_privacy_ceiling(tmp_path):
    from backend.memory_app.v2.image_read import freeze_image_request, validate_image_request
    from backend.memory_app.v2.privacy import set_private_project
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore())
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    parts = upload_fixture(records, tmp_path, 'image-1', count=2)
    args = {'records': records, 'models': models, 'project_id': 'alpha',
        'materials': [{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
        'messages': [{'role': 'user', 'content': parts}],
        'turn_id': 'turn-' + '1' * 32, 'session_id': 'image-read-session',
        'operation_id': 'op-image-read-session', 'idempotency_key': 'image-read-key-000001',
        'created_at': '2026-10-04T12:00:00Z'}
    first = freeze_image_request('media.image_read', **args)
    second = freeze_image_request('media.image_read', **args)
    encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    assert encoded(first) == encoded(second)
    from backend.memory_app.v2.image_read import frozen_image_messages
    assert frozen_image_messages(records, models, first) == args['messages']
    assert len(first['input']['text']) < 2000
    assert first['policy_versions'] == {'image_read': '@1', 'retry': '@1'}
    assert first['privacy']['consent_refs'] == ['crp://default/model-settings/vision']
    assert first['privacy']['source_snapshots'][0]['nodes'][0]['effective_purposes'] == ['embedding', 'generation', 'rerank']
    assert models.public()['generation']['allow_remote'] is False
    validate_image_request(records, models, first)
    set_private_project(records, 'alpha', True, 0)
    with pytest.raises(ValueError, match='image_remote_disabled'):
        validate_image_request(records, models, first)


def test_rapidocr_lazy_engine_has_every_asset_and_preserves_lines(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from backend.memory_app.uploaded_media import UploadedImageReference
    from core.product_core.rapidocr_provider import RapidOcrImageAdapter
    assets = tmp_path / 'models'; assets.mkdir()
    for name in ('det.onnx', 'cls.onnx', 'rec.onnx', 'keys.txt', 'font.ttf'):
        (assets / name).write_bytes(b'provider-fixture')
    path = tmp_path / 'image.png'; path.write_bytes(b'provider-image-fixture')
    reference = UploadedImageReference({'id': 'image-1'}, path)
    calls = []
    class Engine:
        def __init__(self, *, params):
            calls.append(params)
        def __call__(self, value):
            assert value == str(path)
            return SimpleNamespace(txts=['第一行', '第二行'])
    monkeypatch.setattr('core.product_core.rapidocr_provider.find_spec', lambda name: object())
    monkeypatch.setitem(sys.modules, 'rapidocr', SimpleNamespace(RapidOCR=Engine))
    adapter = RapidOcrImageAdapter(reference, model_root=assets)
    assert adapter.status() == 'ready' and calls == []
    source = {'id': 'image-1', 'type': 'image', 'metadata': {'image_reference': reference.reference,
        'image_authorization': {'authorization_id': reference.record['id']}}}
    output = adapter.extract_text(source=source, job={'required_capability': 'ocr'})
    assert output.text == '第一行\n第二行'
    assert output.metadata['remote_processing'] is False
    assert set(calls[0]) >= {'Det.model_path', 'Cls.model_path', 'Rec.model_path', 'Rec.rec_keys_path', 'Global.font_path'}
    assert all(Path(calls[0][key]).is_file() for key in
        ('Det.model_path', 'Cls.model_path', 'Rec.model_path', 'Rec.rec_keys_path', 'Global.font_path'))


def screenshot_bytes():
    import random
    from io import BytesIO
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (1920, 1080), 'white')
    draw = ImageDraw.Draw(image)
    for index in range(54):
        draw.text((40, 20 + index * 19), f'Original screenshot row {index}: reading evidence in its original order. ' * 3,
            fill=(index * 3 % 255, index * 5 % 255, index * 7 % 255))
    # A screenshot includes both body text and an embedded visual asset.
    image.paste(Image.frombytes('RGB', (256, 128), random.Random(15).randbytes(256 * 128 * 3)), (1600, 900))
    output = BytesIO()
    image.save(output, format='PNG')
    assert len(output.getvalue()) > 75000
    return output.getvalue()


def upload_fixture(records, root, identity, *, count=1):
    from backend.shared.llm.image_input import image_content
    folder = root / 'workspace'
    folder.mkdir(exist_ok=True)
    paths = [folder / f'{identity}-{index}.png' for index in range(count)]
    for path in paths:
        path.write_bytes(screenshot_bytes())
    with records.begin() as tx:
        tx.put('workspace_items', identity, {'id': identity, 'project_id': 'alpha',
            'input_kind': 'image', 'original_path': str(paths[0]), 'source_text': '', 'status': 'processing'}, expected_revision=0)
        if count > 1:
            tx.put('v2_image_groups', identity, {'item_id': identity, 'project_id': 'alpha',
                'images': [{'path': str(path), 'name': path.name} for path in paths]}, expected_revision=0)
        tx.commit()
    return [image_content(path.read_bytes()) for path in paths]


def test_real_screenshot_aux_turn_keeps_budget_images_usage_and_freeze_identity(tmp_path):
    from backend.memory_app.v2.image_read import freeze_image_request, validate_image_request, frozen_image_messages
    from backend.memory_app.kernel.image_read import read_images
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    from backend.memory_app.v2.settings import _receipts
    from backend.memory_app.v2.privacy import egress_allowed
    from backend.memory_app.v2.policies import get
    from backend.shared.llm.image_input import image_content
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    calls = []
    def transport(**request):
        calls.append(request)
        return {'choices': [{'message': {'content': json.dumps({'images': [
            {'text': '第一张原文', 'description': '第一张画面'},
            {'text': '第二张原文', 'description': '第二张画面'}]})}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 2048, 'completion_tokens': 80}}
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore(), completion_fn=transport)
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    parts = upload_fixture(records, tmp_path, 'image-1', count=2)
    materials = [{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}]
    messages = get('image_read').prepare(parts)
    def freeze(kind, **values):
        return freeze_image_request(kind, messages=messages, **values)
    args = dict(project='alpha', key='normal-screenshot-group', materials=materials,
        validate=lambda: None, freeze_request=freeze, validate_request=validate_image_request,
        remote_allowed=egress_allowed, messages=messages,
        load_messages=lambda request: frozen_image_messages(records, models, request))
    first, metadata, turn_id = read_images(records, models, **args)
    second, _, replay_id = read_images(records, models, **args)
    assert first == second and turn_id == replay_id and len(calls) == 1
    assert calls[0]['messages'][-1]['content'][1:] == parts
    request = records.list('v2_memory_turn_keys')[0].payload['request']
    assert request['desired_outcome'] == 'media.image_read'
    assert request['execution_policy']['purpose'] == 'aux'
    assert request['capability_policy']['allowed'] == []
    assert frozen_image_messages(records, models, request) == messages
    assert len(request['input']['text']) < 2000
    # The former base64-in-text shape exceeds the existing contract's limit.
    assert len(json.dumps(messages, ensure_ascii=False)) > 100000
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[3] / 'core-contracts/ai/turn-request.schema.json').read_text('utf8'))
    assert not list(Draft202012Validator(schema['properties']['input']).iter_errors(request['input']))
    from copy import deepcopy
    old_input = deepcopy(request['input'])
    old_input['text'] = json.dumps(messages, ensure_ascii=False)
    assert any(error.validator == 'maxLength' for error in
        Draft202012Validator(schema['properties']['input']).iter_errors(old_input))
    assert metadata['usage']['total_tokens'] == 2128
    groups = kernel_call_groups(tmp_path, turn_id=turn_id, records=records)
    assert groups[0]['calls'][0]['egress']['settings_revision'] == {'vision': 1}
    assert _receipts(records, 10, runtime_root=tmp_path)[0]['purpose'] == '识图'
    models.update('vision', {'model': 'changed-model', 'expected_revision': 1})
    with pytest.raises(ValueError, match='image_configuration_changed'):
        validate_image_request(records, models, request)


def test_image_freeze_rejects_mismatched_material_snapshot_and_input_refs(tmp_path):
    from copy import deepcopy
    from backend.memory_app.v2.image_read import freeze_image_request, validate_image_request
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore())
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    parts = upload_fixture(records, tmp_path, 'image-1')
    upload_fixture(records, tmp_path, 'image-2')
    request = freeze_image_request('media.image_read', records=records, models=models,
        project_id='alpha', materials=[{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
        messages=[{'role': 'user', 'content': parts}],
        turn_id='turn-' + '3' * 32, session_id='image-read-session', operation_id='op-image-read-session',
        idempotency_key='image-read-key-000003', created_at='2026-10-04T12:00:00Z')
    tampered = deepcopy(request)
    tampered['privacy']['material_refs'][0]['id'] = 'image-2'
    with pytest.raises(ValueError, match='image_material_binding_invalid'):
        validate_image_request(records, models, tampered)


@pytest.mark.parametrize('change', ['ordinal', 'refs_count', 'snapshots_count', 'file', 'configuration', 'permission', 'mode'])
def test_changed_image_binding_never_reaches_provider(tmp_path, change):
    from copy import deepcopy
    from backend.memory_app.v2.image_read import freeze_image_request, frozen_image_messages
    from backend.memory_app.kernel.image_read import ImageReadOutput
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    calls = []
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore(),
        completion_fn=lambda **request: calls.append(request))
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    parts = upload_fixture(records, tmp_path, 'image-1', count=2)
    request = freeze_image_request('media.image_read', records=records, models=models, project_id='alpha',
        materials=[{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
        messages=[{'role': 'user', 'content': parts}], turn_id='turn-' + '4' * 32,
        session_id='image-read-session', operation_id='op-image-read-session',
        idempotency_key='image-read-key-000004', created_at='2026-10-04T12:00:00Z')
    request = deepcopy(request)
    if change == 'ordinal':
        frozen = json.loads(request['input']['text']); frozen[0]['content'][0]['image']['ordinal'] = 2
        request['input']['text'] = json.dumps(frozen)
    elif change == 'refs_count':
        request['input']['refs'] = []
    elif change == 'snapshots_count':
        request['privacy']['source_snapshots'] = []
    elif change == 'file':
        (tmp_path / 'workspace/image-1-0.png').write_bytes(b'changed-original')
    elif change == 'configuration':
        models.update('vision', {'api_key': 'changed-synthetic-value', 'expected_revision': 1})
    elif change == 'permission':
        models.update('vision', {'allow_remote': False, 'expected_revision': 1})
    else:
        models.update_vision_mode(mode='local', expected_revision=1)
    with pytest.raises(ValueError):
        messages = frozen_image_messages(records, models, request)
        models.complete_vision(messages, response_model=ImageReadOutput)
    assert calls == []


def test_workspace_escape_fails_before_binding_commit(tmp_path):
    from backend.memory_app.v2.image_read import freeze_image_request
    from backend.shared.llm.image_input import image_content
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore())
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    (tmp_path / 'workspace').mkdir()
    outside = tmp_path / 'outside.png'; outside.write_bytes(screenshot_bytes())
    with records.begin() as tx:
        tx.put('workspace_items', 'image-1', {'id': 'image-1', 'project_id': 'alpha', 'input_kind': 'image',
            'original_path': str(outside), 'source_text': '', 'status': 'processing'}, expected_revision=0)
        tx.commit()
    with pytest.raises(ValueError, match='image_original_changed'):
        freeze_image_request('media.image_read', records=records, models=models, project_id='alpha',
            materials=[{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
            messages=[{'role': 'user', 'content': [image_content(outside.read_bytes())]}], turn_id='turn-' + '5' * 32,
            session_id='image-read-session', operation_id='op-image-read-session',
            idempotency_key='image-read-key-000005', created_at='2026-10-04T12:00:00Z')
    assert records.list('v2_image_read_bindings') == ()


def test_failed_key_persistence_leaves_only_inert_binding_and_reentry_calls_once(tmp_path):
    import sqlite3
    from backend.memory_app.v2.image_read import freeze_image_request, validate_image_request, frozen_image_messages
    from backend.memory_app.kernel.image_read import read_images
    from backend.memory_app.v2.privacy import egress_allowed
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    calls = []
    def transport(**request):
        calls.append(request)
        return {'choices': [{'message': {'content': json.dumps({'images': [{'text': '原文', 'description': ''}]})},
            'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 30, 'completion_tokens': 10}}
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore(), completion_fn=transport)
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    parts = upload_fixture(records, tmp_path, 'image-1')
    messages = [{'role': 'user', 'content': parts}]
    arguments = dict(project='alpha', key='key-persistence-failure',
        materials=[{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
        validate=lambda: None, freeze_request=lambda kind, **values: freeze_image_request(kind, messages=messages, **values),
        validate_request=validate_image_request, remote_allowed=egress_allowed, messages=messages,
        load_messages=lambda request: frozen_image_messages(records, models, request))
    with sqlite3.connect(records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_image_key BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_memory_turn_keys' BEGIN SELECT RAISE(ABORT,'injected-key-failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected-key-failure'):
        read_images(records, models, **arguments)
    assert calls == [] and records.list('v2_memory_turn_keys') == ()
    orphan = records.list('v2_image_read_bindings')[0]
    from backend.memory_app.kernel.memory_turn import MemoryTurn
    assert MemoryTurn.store_for(records).get_immutable_payload(orphan.object_id, 'memory-generation-output-v1') is None
    with sqlite3.connect(records.database_path) as connection:
        connection.execute('DROP TRIGGER reject_image_key')
    first = read_images(records, models, **arguments)
    second = read_images(records, models, **arguments)
    assert first[2] != orphan.object_id and first == second and len(calls) == 1


def test_prepare_freeze_decide_are_bound_when_active_version_changes(tmp_path):
    from backend.memory_app.v2.policies import ACTIVE, get, register
    from backend.memory_app.v2.policies.pipelines import versions_for_turn
    from backend.memory_app.v2.policies.types import ModelPolicy
    from backend.memory_app.v2.image_read import freeze_image_request
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore())
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-private-value', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    parts = upload_fixture(records, tmp_path, 'image-1')
    original_version = ACTIVE['image_read']
    baseline = get('image_read')
    replacement = ModelPolicy(prepare=lambda images: [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'replacement policy'}, *images]}],
        decide=lambda images: {'source_text': 'replacement result', 'descriptions': []})
    register('image_read', '@881')(replacement)
    try:
        versions = versions_for_turn('media.image_read')
        prepared = get('image_read').prepare(parts)
        ACTIVE['image_read'] = '@881'
        request = freeze_image_request('media.image_read', records=records, models=models, project_id='alpha',
            materials=[{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
            messages=prepared, policy_versions=versions, turn_id='turn-' + '8' * 32,
            session_id='image-read-session', operation_id='op-image-read-session',
            idempotency_key='image-read-key-000008', created_at='2026-10-04T12:00:00Z')
        assert request['policy_versions'] == versions == {'image_read': original_version, 'retry': '@1'}
        assert json.loads(request['input']['text'])[0]['content'][0] == prepared[0]['content'][0]
        assert get('image_read', version=request['policy_versions']['image_read']).decide(
            [{'text': '旧版识别', 'description': ''}]) == baseline.decide([{'text': '旧版识别', 'description': ''}])
    finally:
        ACTIVE['image_read'] = original_version
