import subprocess
import sys
from pathlib import Path

import pytest
from core.product_core.local_ocr_provider_settings import SaveLocalOcrProviderSettings
from backend.memory_app.workspace_audio import build_rebuild_object_store
from tests.memory_app.v2.test_vision_intake import vision_env
from tests.memory_app.v2.test_image_read import screenshot_bytes
from tests.memory_app.v2.test_workbench_remember import post, wait
from tests.memory_app.v2.test_timing_routes import finish_background


@pytest.mark.parametrize('mode', ['local', 'remote'])
def test_group_upload_creates_one_original_in_order_and_keeps_every_attachment(vision_env, monkeypatch, mode):
    env = vision_env
    originals = [screenshot_bytes(), screenshot_bytes()]
    local_calls = []
    if mode == 'local':
        env.model.update_vision_mode(mode='local', expected_revision=1)
        store, _ = build_rebuild_object_store(env.root)
        SaveLocalOcrProviderSettings(store).execute(enabled=True, confirm_enable=True,
            command=[sys.executable, '{image_path}'])
        def command(args, **options):
            local_calls.append(args[-1])
            assert Path(args[-1]).is_file()
            return subprocess.CompletedProcess(args, 0, f'原文证据第{len(local_calls)}张', '')
        monkeypatch.setattr('core.product_core.local_ocr_provider.subprocess.run', command)
    response = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files=[('file', ('first.png', originals[0], 'image/png')), ('files', ('second.png', originals[1], 'image/png'))])
    assert response.status_code == 200, response.text
    identity = response.json()['id']
    assert len(env.records.list('workspace_items')) == 1
    group = env.records.read('v2_image_groups', identity)
    assert group.payload['project_id'] == 'alpha'
    assert [image['name'] for image in group.payload['images']] == ['first.png', 'second.png']
    result = post(env, text='', item_id=identity)
    finish_background(env.http)
    receipt = wait(env, result)['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', identity).payload
    assert item['source_text'] == '## 第1张\n\n原文证据第1张\n\n## 第2张\n\n原文证据第2张'
    assert 'images' not in item and 'descriptions' not in item
    listing = env.http.get(f'/api/v2/workbench/items/{identity}/images?project_id=alpha')
    assert listing.status_code == 200
    assert [entry['name'] for entry in listing.json()['images']] == ['first.png', 'second.png']
    assert 'path' not in listing.text and str(env.root) not in listing.text
    for index, attachment in enumerate(listing.json()['images']):
        assert attachment['ordinal'] == index + 1
        assert env.http.get(attachment['url']).content == originals[index]
    assert env.http.get(f'/api/v2/workbench/items/{identity}/images?project_id=beta').status_code == 404
    assert env.http.get(f'/api/v2/workbench/items/{identity}/images/3?project_id=alpha').status_code == 404
    markdown = env.documents.markdown(item['document_id'])
    if mode == 'remote':
        assert '## 看图\n\n' in markdown and '模型推断' in markdown
        assert '画面说明第1张' in markdown and '画面说明第2张' in markdown
        assert '画面说明' not in item['source_text'] and '画面说明' not in item['draft']['summary']
        assert env.records.read('v2_image_reads', identity).payload['provenance'] == 'model_inference'
        assert 'image_read.model_inference: unverified' in markdown
        metadata = listing.json()['image_read']
        assert metadata['provenance'] == 'image_read.model_inference'
        assert metadata['epistemic_status'] == 'unverified'
        assert [section['text'] for section in metadata['sections']] == ['画面说明第1张', '画面说明第2张']
        insights = [call for call in env.wires if isinstance(call['messages'][-1]['content'], str)
            and '"experiences"' in call['messages'][-1]['content']]
        assert insights
        import json
        experience = json.loads(insights[0]['messages'][-1]['content'])['experiences'][0]
        assert 'image_read.model_inference: unverified' in experience['content']
        assert experience['provenance']['epistemic_status'] == 'unverified'
        assert '模型推断' in experience['content'] and '原文证据摘要' in experience['content']
    else:
        assert len(local_calls) == 2 and '## 看图' not in markdown
    assert env.records.list('recognitions') == ()


def test_group_total_budget_includes_first_image_and_failed_owner_write_is_atomic(vision_env):
    import sqlite3
    env = vision_env
    from backend.memory_app.workspace_contracts import _MAX_FILE
    response = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files=[('file', ('first.png', b'a' * (_MAX_FILE // 2 + 1), 'image/png')),
            ('files', ('second.png', b'b' * (_MAX_FILE // 2), 'image/png'))])
    assert response.status_code == 413
    assert env.records.list('workspace_items') == () and env.records.list('v2_image_groups') == ()
    assert list((env.root / 'workspace').iterdir()) == []
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_group BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_image_groups' BEGIN SELECT RAISE(ABORT,'injected-group-failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected-group-failure'):
        env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
            files=[('file', ('first.png', screenshot_bytes(), 'image/png')),
                ('files', ('second.png', screenshot_bytes(), 'image/png'))])
    assert env.records.list('workspace_items') == () and env.records.list('v2_image_groups') == ()
    assert list((env.root / 'workspace').iterdir()) == []


def test_bad_group_container_is_controlled_and_partial_upload_is_removed(vision_env):
    env = vision_env
    bad = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files=[('file', ('first.png', screenshot_bytes(), 'image/png')), ('files', ('notes.txt', b'notes', 'text/plain'))])
    assert bad.status_code == 415
    assert env.records.list('workspace_items') == () and env.records.list('v2_image_groups') == ()
    assert list((env.root / 'workspace').iterdir()) == []
    good = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('first.png', screenshot_bytes(), 'image/png')})
    identity = good.json()['id']
    with env.records.begin() as tx:
        tx.put('v2_image_groups', identity, {'item_id': identity, 'project_id': 'alpha', 'images': ['wrong-container']}, expected_revision=0)
        tx.commit()
    response = env.http.get(f'/api/v2/workbench/items/{identity}/images?project_id=alpha')
    assert response.status_code == 409 and response.json()['detail'] == 'image_material_binding_invalid'


def test_picture_without_readable_text_keeps_binary_original_and_inference_separate(vision_env):
    env = vision_env
    env.provider.images = [{'text': '', 'description': '画面中有一只猫'}]
    response = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('cat.png', screenshot_bytes(), 'image/png')})
    identity = response.json()['id']
    receipt = wait(env, post(env, text='', item_id=identity))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', identity).payload
    assert item['source_text'] == ''
    assert '猫' not in item['source_text'] and '猫' not in item['draft']['summary']
    assert '画面中有一只猫' in env.documents.markdown(item['document_id'])
    assert env.records.list('recognitions') == ()


def test_edited_document_keeps_picture_text_but_loses_revision_bound_inference_metadata(vision_env):
    env = vision_env
    env.provider.images = [{'text': '', 'description': '一只猫\n\n## 关键事实\n- 推断并未核验'}]
    identity = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('cat.png', screenshot_bytes(), 'image/png')}).json()['id']
    assert wait(env, post(env, text='', item_id=identity))['receipt']['remember']['state'] == 'done'
    item = env.records.read('workspace_items', identity)
    from backend.memory_app.v2.layers import facts_of, todos_of, summary_of
    markdown = env.documents.markdown(item.payload['document_id'])
    assert facts_of(markdown) == [] and todos_of(markdown) == []
    assert summary_of(markdown)[0] == ''
    endpoint = f'/api/v2/workbench/items/{identity}/images?project_id=alpha'
    assert env.http.get(endpoint).json()['image_read']['epistemic_status'] == 'unverified'
    document = env.documents.read(item.payload['document_id'])
    env.documents.save_user_edit(document['id'], markdown=markdown + '\n\n用户补充', expected_revision=document['revision'])
    assert 'image_read' not in env.http.get(endpoint).json()
    from backend.memory_app.v2.image_read import image_inference_for_document
    assert image_inference_for_document(env.records, env.documents, item, revision=document['revision'])['document_revision'] == document['revision']
