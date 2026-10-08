import subprocess
import sys
import pytest
from pathlib import Path

from tests.memory_app.v2.test_workbench_remember import env, post, wait
from core.product_core.local_ocr_provider_settings import SaveLocalOcrProviderSettings
from backend.memory_app.workspace_audio import build_rebuild_object_store


def test_uploaded_image_reuses_local_ocr_and_real_intake_chain(env, monkeypatch):
    store, _ = build_rebuild_object_store(env.root)
    SaveLocalOcrProviderSettings(store).execute(enabled=True, confirm_enable=True,
        command=[sys.executable, '{image_path}'])
    calls = []
    def run(command, **options):
        calls.append(command)
        assert Path(command[-1]).is_file()
        assert Path(command[-1]).parent == env.root / 'workspace'
        return subprocess.CompletedProcess(command, 0, '原文证据图片内容', '')
    monkeypatch.setattr('core.product_core.local_ocr_provider.subprocess.run', run)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('picture.png', b'\x89PNG\r\n\x1a\n', 'image/png')})
    assert uploaded.status_code == 200, uploaded.text
    item = uploaded.json()
    assert item['input_kind'] == 'image'
    receipt = wait(env, post(env, text='', item_id=item['id']))['receipt']['remember']
    assert receipt['state'] == 'done'
    saved = env.records.read('workspace_items', item['id'])
    assert saved.payload['source_text'] == '原文证据图片内容'
    assert saved.payload['status'] == 'confirmed'
    assert receipt['document_id'] == saved.payload['document_id']
    assert receipt['verified'] is False
    assert len(calls) == 1
    assert all(row['state'] == 'pending' for row in receipt['insights'])
    assert env.records.list('recognitions') == ()
    source = env.domains.confirmations.source_store.read('sources', 'source-' + item['id'])
    assert source['type'] == 'image'
    assert source['metadata']['content'] == '原文证据图片内容'
    original = env.http.get(f"/api/workspace/v1/items/{item['id']}/original?project_id=alpha")
    assert original.status_code == 200
    assert original.content == b'\x89PNG\r\n\x1a\n'
    assert store.list('authorized_file_refs') == ()


def test_disabled_local_ocr_fails_without_model_or_automatic_enable(env):
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('picture.png', b'png', 'image/png')})
    assert uploaded.status_code == 200, uploaded.text
    receipt = wait(env, post(env, text='', item_id=uploaded.json()['id']))['receipt']['remember']
    assert receipt['state'] == 'failed'
    assert receipt['error'] == 'processing_failed'
    assert env.model.calls == 0
    assert env.records.list('recognitions') == ()
    assert env.records.list('documents') == ()


@pytest.mark.parametrize('failure', ['provider', 'changed_original'])
def test_image_failure_is_safe_and_does_not_admit_draft(env, monkeypatch, failure):
    store, _ = build_rebuild_object_store(env.root)
    SaveLocalOcrProviderSettings(store).execute(enabled=True, confirm_enable=True,
        command=[sys.executable, '{image_path}'])
    def run(command, **options):
        if failure == 'provider':
            return subprocess.CompletedProcess(command, 1, '', 'synthetic private provider stderr')
        Path(command[-1]).write_bytes(b'changed original bytes')
        return subprocess.CompletedProcess(command, 0, '原文证据', '')
    monkeypatch.setattr('core.product_core.local_ocr_provider.subprocess.run', run)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('picture.png', b'png', 'image/png')})
    assert uploaded.status_code == 200
    receipt = wait(env, post(env, text='', item_id=uploaded.json()['id']))['receipt']['remember']
    assert receipt['state'] == 'failed'
    assert receipt['error'] == 'processing_failed'
    assert env.model.calls == 0
    assert 'synthetic private' not in str(receipt)
    assert env.records.list('documents') == ()
