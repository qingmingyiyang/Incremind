"""Real capture, item/confirmation owners and SQLite; fake only DNS/HTTP/model."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from tests.memory_app.v2.test_xhs_comment_intake import xhs_admitted
from tests.memory_app.v2.test_vision_intake import vision_env
from fastapi import FastAPI

from backend.memory_app.workspace import install_workspace_routes
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.recognition import RecognitionConflict, RecognitionService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.test_workspace_bilibili_media import (
    BVID, _comment, _comments_page, _official_comment_boundary,
)
from tests.memory_app.v2.test_insight_generation import Model


@pytest.mark.parametrize('change', ['header_span', 'bool_owner', 'wrong_project', 'wrong_run', 'group_revision', 'bool_image_ordinal', 'bool_file_mtime'])
def test_xhs_section_proof_rejects_corruption_and_ocr_header_forgery(xhs_admitted, change):
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    env = xhs_admitted
    with env.records.begin() as tx:
        row = tx.read('v2_original_sections', env.item_id)
        payload = deepcopy(row.payload)
        if change == 'header_span':
            payload['ocr_read']['spans'][0]['start'] = -1
            payload['comments'][0]['start'] = payload['ocr_range']['start'] - 1
        elif change == 'bool_owner':
            payload['input_binding']['owner_revision'] = True
        elif change == 'wrong_project':
            payload['project_id'] = 'beta'
        elif change == 'wrong_run':
            payload['capture_run_id'] = 'other-run'
        elif change == 'bool_image_ordinal':
            payload['input_binding']['images'][0]['ordinal'] = True
        elif change == 'bool_file_mtime':
            payload['input_binding']['images'][0]['identity']['mtime_ns'] = False
        else:
            payload['input_binding']['group_revision'] += 1
        tx.put('v2_original_sections', env.item_id, payload, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict, match='comment_source_invalid'):
        resolve_comment_sources(env.records, env.documents, 'alpha', env.doc)


@pytest.fixture
def source_env(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    view, calls = _official_comment_boundary(monkeypatch, {
        1: _comments_page([_comment(11, 8, '周末已经关门 😀\r\n请提前确认。'),
            _comment(12, 2, '工作日需要预约。')]), 2: _comments_page([])},
        speech='营业到晚上。\r\n## 评论区\r\n这是原正文中的自然标题。')
    view['data']['aid'] = 123
    records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data' / 'structured-records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    model, service = Model(), RecognitionService(records)
    domains = install_workspace_routes(FastAPI(), runtime_root=tmp_path, records=records,
        models=model, documents=documents, service=service)
    item = asyncio.run(domains.intake.add_link({'project_id': 'alpha',
        'url': f'https://www.bilibili.com/video/{BVID}/'}))
    ready = asyncio.run(domains.intake.process(item['id'], {'project_id': 'alpha'}))
    return SimpleNamespace(root=tmp_path, records=records, documents=documents,
        model=model, service=service, domains=domains, item=item, ready=ready, calls=calls)


def admit(env):
    result = asyncio.run(process_and_confirm(env.domains, env.item['id'], 'alpha'))
    env.doc = result['document_id']
    env.model.intake = False
    return env.doc


def resolve(env, **options):
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    return resolve_comment_sources(env.records, env.documents, 'alpha', env.doc, **options)


def test_capture_and_raw_are_committed_once_with_exact_ranges_and_restart(source_env):
    env = source_env
    proof = env.records.read('v2_original_sections', env.item['id'])
    assert proof is not None and proof.payload['state'] == 'bound'
    row = env.records.read('workspace_items', env.item['id'])
    assert row.revision == 4  # create, claim, read, ready; no extra owner mutation
    assert proof.payload['source_text'] == row.payload['source_text']
    assert not {'source_sections', 'owner_birth', 'capture_id'} & row.payload.keys()
    assert len(env.calls) == 5
    admit(env)
    frozen = resolve(env)
    source = frozen['sources'][0]
    assert source['source_id'] == env.item['id'] and source['revision'] == 6
    assert source['coordinate_space'] == 'workspace_source_text_v1'
    assert '营业到晚上。 ## 评论区 这是原正文中的自然标题。' in source['text'][source['body']['start']:source['body']['end']]
    assert [source['text'][span['start']:span['end']] for span in source['comments']] == [
        '周末已经关门 😀\n  请提前确认。', '工作日需要预约。']
    assert [span['ordinal'] for span in source['comments']] == [1, 2]
    restarted = SQLiteStructuredRecordStore(env.records.database_path)
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    assert resolve_comment_sources(restarted, SQLiteDocumentRepository(restarted), 'alpha', env.doc) == frozen


def test_draft_save_revision_and_confirmation_do_not_invalidate_raw_proof(source_env):
    env = source_env
    saved = asyncio.run(env.domains.review.save_draft(env.item['id'], {
        'project_id': 'alpha', 'expected_revision': env.ready['revision'],
        **env.ready['draft'], 'summary': '人工核对摘要'}))
    assert saved['revision'] == 5
    admit(env)
    assert resolve(env)['sources'][0]['revision'] == 7


def test_raw_changed_then_restored_never_restores_capture(source_env):
    env = source_env
    admit(env)
    assert env.documents.read(env.doc) is not None
    original = env.ready['source_text']
    env.domains.items.update(env.item['id'], 'alpha', {'confirmed'}, source_text=original + '改动')
    env.domains.items.update(env.item['id'], 'alpha', {'confirmed'}, source_text=original)
    assert env.records.read('v2_original_sections', env.item['id']).payload['state'] == 'invalidated'
    with pytest.raises(RecognitionConflict):
        resolve(env)


def test_same_id_recreated_through_owner_has_a_new_birth_and_no_capture(source_env):
    env = source_env
    old = env.records.read('v2_original_sections', env.item['id'])
    assert old is not None
    row = env.records.read('workspace_items', env.item['id'])
    with env.records.begin() as tx:
        tx.delete('workspace_items', row.object_id, expected_revision=row.revision)
        tx.commit()
    env.domains.items.create_upload(row.payload)
    new = env.records.read('v2_original_sections', env.item['id'])
    assert new.payload['owner_birth'] != old.payload['owner_birth']
    assert new.payload['state'] == 'unbound' and 'capture_id' not in new.payload


@pytest.mark.parametrize('change', ['range_bool', 'overlap', 'project', 'snapshot', 'ordinal', 'state',
    'capture_revision_bool', 'rpid_duplicate', 'like_bool', 'extra_field', 'birth'])
def test_corrupt_existing_proof_is_rejected_not_reclassified_as_ordinary_body(source_env, change):
    env = source_env
    admit(env)
    row = env.records.read('v2_original_sections', env.item['id'])
    assert row is not None
    value = deepcopy(row.payload)
    if change == 'range_bool':
        value['comments'][0]['start'] = True
    elif change == 'overlap':
        value['comments'][1]['start'] = value['comments'][0]['start']
    elif change == 'project':
        value['project_id'] = 'beta'
    elif change == 'snapshot':
        value['source_text'] += '伪造'
    elif change == 'ordinal':
        value['comments'][1]['ordinal'] = value['comments'][0]['ordinal']
    elif change == 'state':
        value['state'] = 'forged'
    elif change == 'capture_revision_bool':
        value['capture_owner_revision'] = True
    elif change == 'rpid_duplicate':
        value['comments'][1]['rpid'] = value['comments'][0]['rpid']
    elif change == 'like_bool':
        value['comments'][0]['like_count'] = True
    elif change == 'extra_field':
        value['forged'] = 'unknown'
    else:
        value['owner_birth'] = 'invalid'
    with env.records.begin() as tx:
        tx.put(row.collection, row.object_id, value, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        resolve(env)


def test_owner_and_section_writes_rollback_together_under_real_uow(source_env):
    env = source_env
    from backend.memory_app.v2.source_sections import record_source_change
    original = env.records.read('workspace_items', env.item['id'])
    proof = env.records.read('v2_original_sections', env.item['id'])
    with pytest.raises(RuntimeError, match='abort'):
        with env.records.begin() as tx:
            changed = tx.put(original.collection, original.object_id,
                {**original.payload, 'source_text': '临时改动'}, expected_revision=original.revision)
            record_source_change(tx, original, changed)
            raise RuntimeError('abort')
    assert env.records.read(original.collection, original.object_id) == original
    assert env.records.read(proof.collection, proof.object_id) == proof


def test_foreign_project_and_wrong_document_binding_are_refused(source_env):
    env = source_env
    admit(env)
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    with pytest.raises(RecognitionConflict):
        resolve_comment_sources(env.records, env.documents, 'beta', env.doc)
    row = env.records.read('workspace_items', env.item['id'])
    with env.records.begin() as tx:
        tx.put(row.collection, row.object_id, {**row.payload, 'document_id': 'other'}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        resolve(env)


def test_capture_cannot_be_attached_to_an_ordinary_text_owner(source_env):
    env = source_env
    from backend.memory_app.v2.bilibili_comments import build_bilibili_comment_source
    from backend.memory_app.v2.source_sections import trimmed_capture
    from tests.memory_app.v2.test_bilibili_comments import _network, _page, _reply
    item = asyncio.run(env.domains.intake.add_text({'project_id': 'alpha', 'text': '普通原文'}))
    claimed = env.domains.items.processing_lease.claim(item['id'], 'alpha', 1, 'plain-run', None, {})
    network, _ = _network({1: _page([_reply(11, 8, '真实评论')]), 2: _page([])})
    captured = build_bilibili_comment_source('普通原文', network, 123, maximum=60000)
    with pytest.raises(RecognitionConflict):
        env.domains.items.update(item['id'], 'alpha', {'processing'}, expected_run_id='plain-run',
            source_text=captured['source_text'],
            source_sections=trimmed_capture(captured['source_text'], captured['sections']))
    assert env.records.read('workspace_items', item['id']).revision == claimed['revision']
    assert env.records.read('v2_original_sections', item['id']).payload['state'] == 'unbound'


def test_intake_strip_translation_preserves_original_crlf_and_absolute_emoji_offsets():
    from backend.memory_app.v2.bilibili_comments import build_bilibili_comment_source
    from backend.memory_app.v2.source_sections import trimmed_capture
    from tests.memory_app.v2.test_bilibili_comments import _network, _page, _reply
    body = ' \t\r\n标题😀\r\n正文。\r\n- [ ] 待办'
    network, _ = _network({1: _page([_reply(1, 8, '纠正😀')]), 2: _page([])})
    result = build_bilibili_comment_source(body, network, 123, maximum=60000)
    capture = trimmed_capture(result['source_text'], result['sections'])
    raw = capture['source_text']
    assert raw[capture['body']['start']:capture['body']['end']] == body.lstrip()
    assert '\r\n正文。\r\n- [ ] 待办' in raw
    span = capture['comments'][0]
    assert raw[span['start']:span['end']] == '纠正😀'


@pytest.mark.parametrize('field,bound,value', [('body', 'start', -1),
    ('comment_section', 'end', 99999), ('body', 'start', True)])
def test_strip_must_not_repair_forged_raw_coordinates(field, bound, value):
    from backend.memory_app.v2.bilibili_comments import build_bilibili_comment_source
    from backend.memory_app.v2.source_sections import trimmed_capture
    from tests.memory_app.v2.test_bilibili_comments import _network, _page, _reply
    network, _ = _network({1: _page([_reply(1, 1, '评论😀')]), 2: _page([])})
    result = build_bilibili_comment_source('标题\r\n正文', network, 123, maximum=60000)
    result['sections'][field][bound] = value
    with pytest.raises(RecognitionConflict):
        trimmed_capture(result['source_text'], result['sections'])
