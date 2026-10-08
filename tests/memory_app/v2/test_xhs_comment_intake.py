"""One actual upload/link intake; only HTTP, DNS and OCR/model transports fake."""
import json
import asyncio
from pathlib import Path
import pytest
from fastapi import HTTPException

from backend.security import network_adapter
from tests.memory_app.v2.test_vision_intake import vision_env
from tests.memory_app.v2.test_image_read import screenshot_bytes
from tests.memory_app.v2.test_workbench_remember import post, wait


NOTE_ID = '67e65857000000001901abcd'
NOTE_URL = f'https://www.xiaohongshu.com/explore/{NOTE_ID}'
BODY = '原文证据正文：营业到晚上。'


def public_note(monkeypatch, *, text=BODY):
    note = {'noteId': NOTE_ID, 'title': '合成笔记', 'type': 'text', 'desc': text}
    html = '<script>window.__INITIAL_STATE__=' + json.dumps(
        {'note': {'noteDetailMap': {NOTE_ID: {'note': note}}}}, ensure_ascii=True) + ';</script>'
    calls = []

    def transport(request):
        calls.append(request)
        assert request.host == 'www.xiaohongshu.com'
        return network_adapter.BoundedHttpResponse(200,
            {'Content-Type': 'text/html; charset=utf-8'}, html.encode('utf-8'))

    monkeypatch.setattr(network_adapter, '_resolve_addresses', lambda *_: ('8.8.8.8',))
    monkeypatch.setattr(network_adapter, '_perform_pinned_request', transport)
    return calls


def test_actual_endpoint_keeps_link_body_and_screenshot_in_one_original(vision_env, monkeypatch):
    env = vision_env
    calls = public_note(monkeypatch)
    original = screenshot_bytes()
    env.provider.images = [{'text': '周末已经关门 😀', 'description': '截图说明'}]
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', original, 'image/png')})
    assert uploaded.status_code == 200
    identity = uploaded.json()['id']
    receipt = wait(env, post(env, text=NOTE_URL, item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', identity)
    assert BODY in item.payload['source_text']
    assert NOTE_URL in item.payload['source_text']
    assert '## 评论区' in item.payload['source_text']
    assert '周末已经关门 😀' in item.payload['source_text']
    assert len(env.records.list('workspace_items')) == 1
    assert len(calls) == 1
    assert env.records.read('v2_image_reads', identity).payload['source_text'] == '周末已经关门 😀'
    source = env.domains.confirmations.source_store.read('sources', 'source-' + identity)
    assert source['metadata']['content_snapshot'] == item.payload['source_text']
    assert receipt['document_id'] == item.payload['document_id']
    assert env.http.get(f'/api/v2/workbench/items/{identity}/images/1?project_id=alpha').content == original
    assert env.records.list('recognitions') == ()


@pytest.fixture
def xhs_admitted(vision_env, monkeypatch):
    env = vision_env
    env.network_calls = public_note(monkeypatch)
    env.provider.images = [{'text': '周末已经关门 😀', 'description': '截图说明'}]
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    assert uploaded.status_code == 200
    env.item_id = uploaded.json()['id']
    row = env.records.read('workspace_items', env.item_id)
    asyncio.run(env.domains.intake.bind_link_images(env.item_id, 'alpha', NOTE_URL, expected_revision=row.revision))
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    result = asyncio.run(process_and_confirm(env.domains, env.item_id, 'alpha'))
    assert result['status'] == 'confirmed', result
    env.doc = result['document_id']
    return env


def test_group_spans_keep_real_image_ordinals_and_embedded_fake_headings(vision_env, monkeypatch):
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    env = vision_env
    public_note(monkeypatch)
    env.provider.images = [
        {'text': '甲 😀\n## 第2张\n仍是甲', 'description': ''},
        {'text': '', 'description': ''}, {'text': '乙补充。', 'description': ''}]
    original = screenshot_bytes()
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files=[('file', ('one.png', original, 'image/png')), ('files', ('two.png', original, 'image/png')),
            ('files', ('three.png', original, 'image/png'))])
    assert uploaded.status_code == 200
    identity = uploaded.json()['id']
    receipt = wait(env, post(env, text=NOTE_URL, item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    source = resolve_comment_sources(env.records, env.documents, 'alpha', receipt['document_id'])['sources'][0]
    assert [comment['ordinal'] for comment in source['comments']] == [1, 3]
    assert [source['text'][comment['start']:comment['end']] for comment in source['comments']] == [
        '甲 😀\n## 第2张\n仍是甲', '乙补充。']
    proof = env.records.read('v2_original_sections', identity).payload
    assert proof['origin'] == 'xiaohongshu'
    assert proof['input_binding']['request_url'] == NOTE_URL
    assert all(set(comment) == {'ordinal', 'start', 'end'} for comment in proof['comments'])
    assert env.records.read('v2_image_reads', identity).payload['source_text'] == source['text'][
        proof['ocr_range']['start']:proof['ocr_range']['end']]


@pytest.mark.parametrize('failure, expected', [('empty', 'empty'), ('provider', 'unavailable'), ('budget', 'budget')])
def test_ocr_unavailable_empty_or_whole_budget_keeps_successful_body(vision_env, monkeypatch, failure, expected):
    env = vision_env
    public_note(monkeypatch)
    env.provider.images = [{'text': '字' * 60000 if failure == 'budget' else '', 'description': ''}]
    if failure == 'provider':
        def unavailable():
            raise RuntimeError('synthetic OCR unavailable')
        env.provider.on_images = unavailable
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    identity = uploaded.json()['id']
    receipt = wait(env, post(env, text=NOTE_URL, item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', identity)
    assert BODY in item.payload['source_text'] and '## 评论区' not in item.payload['source_text']
    proof = env.records.read('v2_original_sections', identity).payload
    assert proof['comment_status'] == expected and proof['comments'] == []
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    assert resolve_comment_sources(env.records, env.documents, 'alpha', receipt['document_id'])['sources'] == []
    assert env.http.get(f'/api/v2/workbench/items/{identity}/images/1?project_id=alpha').status_code == 200


@pytest.mark.parametrize('change', ['file', 'group', 'private'])
def test_drift_during_ocr_cannot_be_downgraded_to_body_success(vision_env, monkeypatch, change):
    env = vision_env
    public_note(monkeypatch)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    identity = uploaded.json()['id']
    def drift():
        if change == 'file':
            Path(env.records.read('workspace_items', identity).payload['original_path']).write_bytes(b'changed')
        elif change == 'group':
            with env.records.begin() as tx:
                row = tx.read('v2_original_sections', identity)
                tx.put('v2_original_sections', identity, {**row.payload, 'input_binding': {
                    **row.payload['input_binding'], 'group_revision': 99}}, expected_revision=row.revision)
                tx.commit()
        else:
            from backend.memory_app.v2.privacy import set_private_project
            set_private_project(env.records, 'alpha', True, 0)
    env.provider.on_images = drift
    receipt = wait(env, post(env, text=NOTE_URL, item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'failed', receipt
    assert env.records.read('workspace_items', identity).payload['status'] == 'failed'
    assert env.records.list('documents') == ()
    assert env.records.list('recognition_candidates') == ()


def test_bind_uses_owner_cas_scope_and_is_idempotent_without_network_or_item_revision(vision_env, monkeypatch):
    env = vision_env
    calls = public_note(monkeypatch)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    identity = uploaded.json()['id']
    row = env.records.read('workspace_items', identity)
    bind = env.domains.intake.bind_link_images
    result = asyncio.run(bind(identity, 'alpha', NOTE_URL, expected_revision=row.revision))
    side = env.records.read('v2_original_sections', identity)
    assert result['id'] == identity and result['input_kind'] == 'image' and result['revision'] == row.revision
    assert asyncio.run(bind(identity, 'alpha', NOTE_URL, expected_revision=row.revision)) == result
    assert env.records.read('v2_original_sections', identity).revision == side.revision
    assert env.records.read('workspace_items', identity) == row
    assert calls == []
    for project, url, revision, status in [('beta', NOTE_URL, row.revision, 404),
        ('alpha', NOTE_URL, True, 409), ('alpha', NOTE_URL, row.revision + 1, 409),
        ('alpha', 'https://evil.invalid/', row.revision, 400)]:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(bind(identity, project, url, expected_revision=revision))
        assert caught.value.status_code == status


def test_paired_local_ocr_preserves_text_and_never_uses_remote_vision(vision_env, monkeypatch):
    import subprocess
    import sys
    from backend.memory_app.workspace_audio import build_rebuild_object_store
    from core.product_core.local_ocr_provider_settings import SaveLocalOcrProviderSettings
    from core.product_core.cloud_asr_provider_settings import SaveCloudAsrProviderSettings
    from backend.memory_app.v2.privacy import set_private_project
    env = vision_env
    public_note(monkeypatch)
    env.model.update_vision_mode(mode='local', expected_revision=1)
    store, _ = build_rebuild_object_store(env.root)
    SaveLocalOcrProviderSettings(store).execute(enabled=True, confirm_enable=True,
        command=[sys.executable, '{image_path}'])
    SaveCloudAsrProviderSettings(store, now='2026-10-05T00:00:00Z').execute(
        enabled=True, confirm_enable=True)
    set_private_project(env.records, 'alpha', True, 0)
    text = '本机留言 😀\r\n第二行。'
    def command(args, **options):
        assert Path(args[-1]).is_file()
        return subprocess.CompletedProcess(args, 0, text, '')
    monkeypatch.setattr('core.product_core.local_ocr_provider.subprocess.run', command)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    identity = uploaded.json()['id']
    receipt = wait(env, post(env, text=NOTE_URL, item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', identity)
    saved = env.records.read('v2_image_reads', identity).payload
    assert saved['source_text'] == text and saved['provenance'] == 'local_ocr'
    assert item.payload['source_text'].endswith(text)
    assert all(not isinstance(wire['messages'][-1]['content'], list) for wire in env.wires)


def test_primary_failure_retry_keeps_committed_xhs_body_and_original_ocr_without_recapture(vision_env, monkeypatch):
    env = vision_env
    calls = public_note(monkeypatch)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    identity = uploaded.json()['id']
    row = env.records.read('workspace_items', identity)
    asyncio.run(env.domains.intake.bind_link_images(identity, 'alpha', NOTE_URL, expected_revision=row.revision))
    provider = env.model._completion_fn
    def fail_primary(**request):
        if isinstance(request['messages'][-1]['content'], str):
            raise RuntimeError('synthetic primary unavailable')
        return provider(**request)
    env.model._completion_fn = fail_primary
    failed = asyncio.run(env.domains.intake.process(identity, {'project_id': 'alpha'}))
    assert failed['status'] == 'failed' and BODY in failed['source_text']
    env.model._completion_fn = provider
    asyncio.run(env.domains.items.retry(identity, {'project_id': 'alpha'}))
    retried = asyncio.run(env.domains.intake.process(identity, {'project_id': 'alpha'}))
    assert retried['status'] == 'ready', retried
    assert retried['source_text'] == failed['source_text']
    assert len(calls) == 1
    assert env.records.read('v2_original_sections', identity).payload['state'] == 'bound'
    assert len([wire for wire in env.wires if isinstance(wire['messages'][-1]['content'], list)]) == 1
    from backend.memory_app.v2.image_read import effective_image_read
    assert effective_image_read(env.records, env.records.read('workspace_items', identity)) is not None


@pytest.mark.parametrize('state', ['unbound', 'unknown'])
def test_corrupt_pending_proof_is_rejected_before_ordinary_image_processing(vision_env, state):
    from backend.recognition import RecognitionConflict
    env = vision_env
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('comment.png', screenshot_bytes(), 'image/png')})
    identity = uploaded.json()['id']
    row = env.records.read('workspace_items', identity)
    asyncio.run(env.domains.intake.bind_link_images(identity, 'alpha', NOTE_URL, expected_revision=row.revision))
    with env.records.begin() as tx:
        proof = tx.read('v2_original_sections', identity)
        tx.put('v2_original_sections', identity, {**proof.payload, 'state': state}, expected_revision=proof.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='comment_source_invalid'):
        asyncio.run(env.domains.intake.process(identity, {'project_id': 'alpha'}))
    assert env.records.read('workspace_items', identity).payload['status'] == 'staged'
