"""Real uploads and intake, with only command/provider transports replaced."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.workspace_audio import build_rebuild_object_store
from backend.security.secrets import InMemorySecretStore
from backend.recognition import RecognitionService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from core.product_core.local_ocr_provider_settings import SaveLocalOcrProviderSettings
from tests.memory_app.v2.test_workbench_remember import assemble, post, wait
from tests.memory_app.v2.test_image_read import screenshot_bytes


@pytest.fixture
def vision_env(tmp_path, monkeypatch):
    # Load the existing transport dependency before the unchanged eight-second
    # background assertion starts; first import is slow on this workstation.
    import litellm
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    wires = []
    provider = SimpleNamespace(images=None, on_images=None)
    def transport(**request):
        wires.append(request)
        if isinstance(request['messages'][-1]['content'], list):
            if provider.on_images is not None:
                provider.on_images()
            count = sum(part.get('type') == 'image_url' for part in request['messages'][-1]['content'])
            body = {'images': provider.images if provider.images is not None else (
                [{'text': '原文证据图片内容', 'description': '一个纸质页面'}] if count == 1 else
                [{'text': f'原文证据第{index}张', 'description': f'画面说明第{index}张'} for index in range(1, count + 1)])}
        elif '"insights"' in str(request['messages'][0]['content']):
            from tests.memory_app.v2.test_insight_generation import response_for
            body = json.loads(response_for(request['messages'], json.dumps({'insights': []})))
        else:
            body = {'title': '整理稿', 'summary': '原文证据摘要', 'topics': [], 'facts': [],
                'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        return {'choices': [{'message': {'content': json.dumps(body, ensure_ascii=False)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 240, 'completion_tokens': 40}}
    model = ModelConfiguration(records, tmp_path, secrets=InMemorySecretStore(), completion_fn=transport)
    model.update('generation', {'base_url': 'http://localhost/v1', 'model': 'local-fake',
        'api_key': 'synthetic-local-value', 'allow_remote': False, 'expected_revision': 0})
    model.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-vision-value', 'allow_remote': True, 'expected_revision': 0})
    model.update_vision_mode(mode='remote', expected_revision=0)
    documents, service = SQLiteDocumentRepository(records), RecognitionService(records)
    app, domains = assemble(tmp_path, records, documents, service, model)
    with TestClient(app) as http:
        yield SimpleNamespace(root=tmp_path, records=records, documents=documents, service=service,
            model=model, app=app, domains=domains, http=http, wires=wires, provider=provider)


def test_remote_uploaded_image_creates_original_and_auxiliary_usage_receipt(vision_env):
    env = vision_env
    original = screenshot_bytes()
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('screenshot.png', original, 'image/png')})
    assert uploaded.status_code == 200
    receipt = wait(env, post(env, text='', item_id=uploaded.json()['id']))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', uploaded.json()['id']).payload
    assert item['source_text'] == '原文证据图片内容'
    assert env.domains.confirmations.source_store.read('sources', 'source-' + item['id'])['metadata']['content'] == item['source_text']
    assert env.http.get(f"/api/workspace/v1/items/{item['id']}/original?project_id=alpha").content == original
    vision_calls = [call for call in env.wires if call['model'].endswith('vision-fake')]
    assert len(vision_calls) == 1
    assert vision_calls[0]['messages'][-1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
    row = env.records.read('v2_image_reads', item['id']).payload
    assert row['descriptions'] == ['一个纸质页面'] and row['local_fallback'] is False
    from backend.memory_app.v2.settings import _receipts
    receipts = _receipts(env.records, 20, runtime_root=env.root)
    assert any(row['purpose'] == '识图' and row['usage'] == {'input': 240, 'output': 40} for row in receipts)
    assert env.records.list('recognitions') == ()


@pytest.mark.parametrize('blocked', ['global_off', 'private'])
def test_blocked_vision_uses_local_ocr_and_marks_fallback_without_remote_wire(vision_env, monkeypatch, blocked):
    env = vision_env
    store, _ = build_rebuild_object_store(env.root)
    SaveLocalOcrProviderSettings(store).execute(enabled=True, confirm_enable=True,
        command=[sys.executable, '{image_path}'])
    local_calls = []
    def command(args, **options):
        local_calls.append(args)
        assert Path(args[-1]).is_file()
        return subprocess.CompletedProcess(args, 0, '原文证据本机图片', '')
    monkeypatch.setattr('core.product_core.local_ocr_provider.subprocess.run', command)
    if blocked == 'global_off':
        env.model.update('vision', {'allow_remote': False, 'expected_revision': 1})
    else:
        from backend.memory_app.v2.privacy import set_private_project
        set_private_project(env.records, 'alpha', True, 0)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('screenshot.png', screenshot_bytes(), 'image/png')})
    assert uploaded.status_code == 200
    receipt = wait(env, post(env, text='', item_id=uploaded.json()['id']))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    assert len(local_calls) == 1
    assert not any(call['api_base'].startswith('https://vision.invalid') for call in env.wires)
    row = env.records.read('v2_image_reads', uploaded.json()['id']).payload
    assert row['local_fallback'] is True and row['descriptions'] == []
    assert env.records.list('v2_image_read_bindings') == ()
    jobs = env.http.get('/api/v2/jobs?project_id=alpha').json()['items']
    assert any(job['image_read']['local_fallback'] is True for job in jobs if job.get('image_read'))


def test_retry_fences_old_read_and_seals_fallback_to_the_confirmed_owner(vision_env, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from backend.memory_app.uploaded_media import read_uploaded_image
    env = vision_env
    store, _ = build_rebuild_object_store(env.root)
    SaveLocalOcrProviderSettings(store).execute(enabled=True, confirm_enable=True,
        command=[sys.executable, '{image_path}'])
    monkeypatch.setattr('core.product_core.local_ocr_provider.subprocess.run',
        lambda args, **options: subprocess.CompletedProcess(args, 0, '原文证据本机图片', ''))
    env.model.update('vision', {'allow_remote': False, 'expected_revision': 1})
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('screenshot.png', screenshot_bytes(), 'image/png')}).json()
    identity = uploaded['id']
    items = env.domains.items
    row = items.item_for(identity, 'alpha')
    items.processing_lease.claim(identity, 'alpha', row.revision, 'old-run', None, {})
    source = read_uploaded_image(env.domains.intake, items.item_for(identity, 'alpha').payload,
        'alpha', identity, 'old-run')
    items.update(identity, 'alpha', {'processing'}, expected_run_id='old-run',
        source_text=source, status='failed', error='processing_failed')
    asyncio.run(items.retry(identity, {'project_id': 'alpha'}))
    row = items.item_for(identity, 'alpha')
    items.processing_lease.claim(identity, 'alpha', row.revision, 'new-run', None, {})
    jobs = env.http.get('/api/v2/jobs?project_id=alpha').json()['items']
    assert not any(job.get('image_read') for job in jobs)
    old_sidecar = env.records.read('v2_image_reads', identity)
    with pytest.raises(HTTPException) as conflict:
        items.update(identity, 'alpha', {'processing'}, expected_run_id='old-run', status='ready')
    assert conflict.value.status_code == 409
    assert env.records.read('v2_image_reads', identity) == old_sidecar
    source = read_uploaded_image(env.domains.intake, items.item_for(identity, 'alpha').payload,
        'alpha', identity, 'new-run')
    draft = {'title': '整理稿', 'summary': '原文证据本机图片', 'topics': [], 'facts': [],
        'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
    ready = items.update(identity, 'alpha', {'processing'}, expected_run_id='new-run',
        source_text=source, draft=draft, status='ready')
    asyncio.run(env.domains.review.confirm(identity, {'project_id': 'alpha', 'expected_revision': ready['revision']}))
    item = items.item_for(identity, 'alpha')
    image = env.records.read('v2_image_reads', identity).payload
    assert image['run_id'] == 'new-run' and image['owner_revision'] == item.payload['reviewed_revision']
    assert any(job.get('image_read', {}).get('local_fallback') is True
        for job in env.http.get('/api/v2/jobs?project_id=alpha').json()['items'])


def test_late_provider_result_cannot_write_read_sidecar_after_reclaim(vision_env):
    from backend.recognition import RecognitionConflict
    from backend.memory_app.uploaded_media import read_uploaded_image
    env = vision_env
    identity = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('screenshot.png', screenshot_bytes(), 'image/png')}).json()['id']
    items = env.domains.items
    row = items.item_for(identity, 'alpha')
    items.processing_lease.claim(identity, 'alpha', row.revision, 'old-run', None, {})
    def reclaim():
        assert items.processing_lease.interrupt(identity, 'alpha', 'old-run')
        row = items.item_for(identity, 'alpha')
        items.processing_lease.claim(identity, 'alpha', row.revision, 'new-run', None, {})
    env.provider.on_images = reclaim
    with pytest.raises((RecognitionConflict, ValueError)):
        read_uploaded_image(env.domains.intake, items.item_for(identity, 'alpha').payload,
            'alpha', identity, 'old-run')
    assert items.item_for(identity, 'alpha').payload['processing_run_id'] == 'new-run'
    assert env.records.read('v2_image_reads', identity) is None


def test_settings_vision_mode_cas_is_independent_and_never_returns_secret(vision_env):
    env = vision_env
    before = env.http.get('/api/v2/settings').json()
    vision = before['model']['vision']
    changed = env.http.patch('/api/v2/settings/vision-mode', json={'mode': 'local', 'expected_revision': vision['mode_revision']})
    assert changed.status_code == 200
    stale = env.http.patch('/api/v2/settings/vision-mode', json={'mode': 'remote', 'expected_revision': vision['mode_revision']})
    assert stale.status_code == 409
    after = env.http.get('/api/v2/settings')
    assert after.json()['model']['vision']['mode'] == 'local'
    assert after.json()['model']['generation'] == before['model']['generation']
    assert after.json()['privacy'] == before['privacy']
    assert 'synthetic-vision-value' not in after.text and 'secret_ref' not in after.text
    assert env.http.patch('/api/v2/settings/vision-mode', json={'mode': 'local', 'expected_revision': True}).status_code == 400


def test_optional_rapidocr_missing_on_linux_keeps_uploaded_original_and_fails_safely(vision_env, monkeypatch):
    # OS and import availability are environment inputs; the adapter, settings,
    # upload, processing owners and tray are all the real implementations.
    monkeypatch.setattr('backend.memory_app.local_image_provider.os', SimpleNamespace(name='posix'))
    monkeypatch.setitem(sys.modules, 'rapidocr', None)
    env = vision_env
    env.model.update_vision_mode(mode='local', expected_revision=1)
    settings = env.http.get('/api/v2/settings')
    assert settings.status_code == 200
    assert settings.json()['model']['vision']['local'] == {'provider': 'rapidocr', 'status': 'unavailable'}
    original = screenshot_bytes()
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('screenshot.png', original, 'image/png')})
    assert uploaded.status_code == 200
    identity = uploaded.json()['id']
    receipt = wait(env, post(env, text='', item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'failed' and receipt['error'] == 'processing_failed'
    assert env.http.get(f'/api/workspace/v1/items/{identity}/original?project_id=alpha').content == original
    assert env.records.list('documents') == () and env.records.list('recognitions') == () and env.wires == []
    jobs = env.http.get('/api/v2/jobs?project_id=alpha')
    assert jobs.status_code == 200 and jobs.json()['items'][0]['state'] == 'failed'
    assert not (env.root / 'data' / 'models' / 'rapidocr').exists()


@pytest.mark.parametrize('mode', [{}, [], None, 1])
def test_invalid_vision_mode_container_is_controlled_without_a_write(vision_env, mode):
    env = vision_env
    before = env.records.read('v2_vision_mode', 'default')
    response = env.http.patch('/api/v2/settings/vision-mode', json={'mode': mode, 'expected_revision': 1})
    assert response.status_code == 400 and response.json()['detail'] == 'vision_mode_invalid'
    assert env.records.read('v2_vision_mode', 'default') == before
