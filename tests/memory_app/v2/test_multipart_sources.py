"""File and link source bodies remain separate from routed input spans."""
import json
from threading import Event
import pytest
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_workbench_multipart import _handler, _context


@pytest.mark.parametrize('kind', ['file', 'link'])
def test_multipart_nontext_dependencies_use_input_span(do_env, monkeypatch, kind):
    client, model = do_env
    body = {'project_id':'project-a'}
    span = '记住附件。' if kind == 'file' else 'https://example.test/budget '
    if kind == 'file':
        upload = client.post('/api/v2/workbench/files', data=body,
            files={'file':('budget.txt', '附件正文十万元'.encode(), 'text/plain')})
        assert upload.status_code == 200, upload.text
        body['item_id'] = upload.json()['id']
    else:
        from backend.memory_app import workspace_links
        monkeypatch.setattr(workspace_links, '_fetch_url', lambda url:'网页正文十万元')
    question = '预算多少？'
    body['text'] = span + question
    release = Event()
    def answer(messages):
        from backend.memory_app.v2.part_context import PREFIX
        dependency = [message['content'] for message in messages if message['content'].startswith(PREFIX)]
        assert dependency == [PREFIX + span]
        return json.dumps({'answer':'请补充预算内容', 'citations':[]})
    model.handler = _handler([{'intent':'remember', 'span':span, 'depends_on':[]},
        {'intent':'ask', 'span':question, 'depends_on':[0]}], answer=answer,
        organize=lambda:release.wait(10))
    try:
        result = client.post('/api/v2/workbench/turns', json=body)
        assert result.status_code == 200, result.text
        parts = result.json()['turn']['receipt']['parts']
        assert parts[1]['state'] == 'done', parts
        if kind == 'file':
            assert parts[0]['receipt']['remember']['item_id'] == body['item_id']
        records = _context(client).records
        bindings = records.list('v2_part_contexts')
        content = bindings[0].payload['context']['originals'][0]['content']
        assert content['coordinate_space'] == 'child_turn_user_text_v1'
        assert content['span'] == span
        assert content['identity']['source_text'] != span
    finally:
        release.set()


@pytest.mark.parametrize('change', ['policy', 'private', 'profile'])
def test_completed_answer_dependencies_keep_first_wire_authority(do_env, change):
    from backend.memory_app.v2.part_context import prepare_context, validate_context
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.privacy import set_private_project
    from backend.recognition import RecognitionError, WorkScope
    from tests.memory_app.v2.test_workbench_ask import publish
    client, model = do_env
    context = _context(client)
    recognition, _ = publish(context, text='alpha预算十万元', project='project-a')
    parts = [{'intent':'ask', 'span':'alpha预算是多少？', 'depends_on':[]},
             {'intent':'inspiration', 'span':'灵感先做预算。', 'depends_on':[]}]
    model.handler = _handler(parts, answer=lambda _:json.dumps({'answer':'预算十万元', 'citations':[1]}))
    response = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'text':'alpha预算是多少？灵感先做预算。'})
    assert response.status_code == 200, response.text
    parent_id = response.json()['turn']['id']
    parent = context.records.read('v2_turns', parent_id)
    ask = parent.payload['part_turn_ids'][0]
    query = client.app.state.workspace_domains.query
    frozen = prepare_context(context.records, query, 'project-a', parent_id, [ask])
    validate_context(context.records, query, frozen)
    if change == 'policy':
        authority = SourceEgressService(context.records)
        for revision, purposes in [(0, []), (1, ['generation', 'embedding', 'rerank'])]:
            authority.set_policy(scope=WorkScope('local-user', 'project-a'), source_type='recognition',
                source_id=recognition.id, allowed_purposes=purposes,
                expected_source_revision=recognition.revision, expected_policy_revision=revision)
    elif change == 'private':
        set_private_project(context.records, 'project-a', True, 0)
        set_private_project(context.records, 'project-a', False, 1)
    else:
        publish(context, text='用户偏好直接说明预算', project='me')
    with pytest.raises(RecognitionError):
        validate_context(context.records, query, frozen)


def test_completed_answer_keeps_its_original_dependency_closure(do_env):
    from backend.memory_app.v2.part_context import prepare_context, validate_context
    from backend.recognition import RecognitionError
    client, model = do_env
    model.handler = _handler([{'intent':'remember', 'span':'预算十万元。', 'depends_on':[]},
        {'intent':'ask', 'span':'预算多少？', 'depends_on':[0]}])
    result = client.post('/api/v2/workbench/turns', json={'project_id':'project-a', 'text':'预算十万元。预算多少？'})
    assert result.status_code == 200, result.text
    records = _context(client).records
    parent = records.read('v2_turns', result.json()['turn']['id'])
    query = client.app.state.workspace_domains.query
    frozen = prepare_context(records, query, 'project-a', parent.object_id, [parent.payload['part_turn_ids'][1]])
    item_id = result.json()['turn']['receipt']['parts'][0]['receipt']['remember']['item_id']
    with records.begin() as tx:
        row = tx.read('workspace_items', item_id)
        tx.put('workspace_items', item_id, {**row.payload, 'lease_marker':'background-completed'}, expected_revision=row.revision)
        tx.commit()
    validate_context(records, query, frozen)
    with records.begin() as tx:
        row = tx.read('workspace_items', item_id)
        tx.put('workspace_items', item_id, {**row.payload, 'source_text':'预算二十万元。'}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(RecognitionError):
        validate_context(records, query, frozen)


def test_multipart_inspiration_inherits_explicit_project_tag(do_env):
    client, model = do_env
    created = client.post('/api/v2/projects', json={'name':'合成项目'})
    assert created.status_code == 200, created.text
    project = created.json()['id']
    model.handler = _handler([{'intent':'inspiration', 'span':'灵感做预算。', 'depends_on':[]},
        {'intent':'ask', 'span':'预算多少？', 'depends_on':[]}])
    result = client.post('/api/v2/workbench/turns', json={
        'project_id':'default', 'text':f'#{project} 灵感做预算。预算多少？'})
    assert result.status_code == 200, result.text
    records = _context(client).records
    parent = records.read('v2_turns', result.json()['turn']['id'])
    child = records.read('v2_turns', parent.payload['part_turn_ids'][0])
    assert child.payload['project_id'] == project
    assert child.payload['parent_turn_id'] == parent.object_id


def test_multipart_favorites_keep_discovery_and_fail_dependencies_closed(do_env):
    client, model = do_env
    url = 'https://space.bilibili.com/123/favlist?fid=456'
    seen = []
    client.app.state.bilibili_favorites_discovery = lambda **kwargs:seen.append(kwargs['url']) or []
    model.handler = _handler([{'intent':'remember', 'span':url+' ', 'depends_on':[]},
        {'intent':'ask', 'span':'收藏有哪些？', 'depends_on':[0]}])
    result = client.post('/api/v2/workbench/turns', json={'project_id':'project-a', 'text':url+' 收藏有哪些？'})
    assert result.status_code == 200, result.text
    assert seen == [url]
    assert result.json()['turn']['intent'] == 'multi', result.json()
    parts = result.json()['turn']['receipt']['parts']
    assert parts[0]['state'] == 'failed'
    assert parts[0]['receipt']['remember']['error'] == 'favorites_discovery_failed'
    assert parts[1]['state'] == 'not_started' and parts[1]['error'] == 'dependency_failed'
    assert _context(client).records.list('workspace_items') == ()


@pytest.mark.parametrize('change', ['private', 'content'])
def test_ordinary_followup_rechecks_multipart_original_authority(do_env, change):
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    from backend.memory_app.v2.followup import read_history, validate_history
    from backend.recognition import RecognitionError
    client, model = do_env
    release = Event()
    marker = '仅由依赖原文得出的合成结论'
    model.handler = _handler([
        {'intent':'remember', 'span':'预算十万元。', 'depends_on':[]},
        {'intent':'ask', 'span':'预算多少？', 'depends_on':[0]}],
        answer=lambda _:json.dumps({'answer':marker, 'citations':[]}),
        organize=lambda:release.wait(60))
    try:
        result = client.post('/api/v2/workbench/turns', json={
            'project_id':'project-a', 'text':'预算十万元。预算多少？'})
        assert result.status_code == 200, result.text
        parent = result.json()['turn']
        assert parent['intent'] == 'multi' and parent['receipt']['parts'][1]['state'] == 'done'
        assert parent['receipt']['parts'][1]['receipt']['ask']['citations'] == []
        records = _context(client).records
        query = client.app.state.workspace_domains.query
        history = read_history(records, 'project-a', result.json()['thread_id'], '继续解释', query=query)
        assert marker in history['text']
        item = records.read('workspace_items', parent['receipt']['parts'][0]['receipt']['remember']['item_id'])
        if change == 'private':
            SourceEgressService(records).set_policy(scope=WorkScope('local-user', 'project-a'),
                source_type='original_item', source_id=item.object_id, allowed_purposes=[],
                expected_source_revision=item.revision, expected_policy_revision=0)
        else:
            with records.begin() as tx:
                tx.put('workspace_items', item.object_id, {**item.payload, 'source_text':'预算二十万元。'},
                    expected_revision=item.revision)
                tx.commit()
        with pytest.raises(RecognitionError):
            validate_history(query, 'project-a', history, query.ask_target())
        calls = len(model.calls)
        followup = client.post('/api/v2/workbench/turns', json={
            'project_id':'project-a', 'thread_id':result.json()['thread_id'], 'intent':'ask', 'text':'继续解释'})
        assert followup.status_code == 200, followup.text
        assert marker not in str(model.calls[calls:])
        assert followup.json()['turn']['receipt']['ask']['trace'][0]['history_turn_ids'] == []
    finally:
        release.set()


def test_multipart_favorites_keep_all_expanded_video_receipts_on_refresh(do_env, monkeypatch):
    from backend.memory_app import workspace_bilibili_media
    client, model = do_env
    url = 'https://space.bilibili.com/123/favlist?fid=456'
    videos = ['https://www.bilibili.com/video/BV1000000001', 'https://www.bilibili.com/video/BV1000000002']
    client.app.state.bilibili_favorites_discovery = lambda **kwargs:videos
    monkeypatch.setattr(workspace_bilibili_media, 'read_bilibili_media', lambda video, root, **kwargs:
        {'source_text':'合成视频原文', 'title':'视频', 'canonical_url':video,
         'acquisition_method':'official_subtitle', 'content_kind':'video'})
    model.handler = _handler([{'intent':'remember', 'span':url+' ', 'depends_on':[]},
        {'intent':'ask', 'span':'收藏有哪些？', 'depends_on':[0]}])
    result = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'text':url+' 收藏有哪些？'}, headers={'Idempotency-Key':'multi-favorites'})
    assert result.status_code == 200, result.text
    assert result.json()['turn']['intent'] == 'multi'
    history = client.get(f"/api/v2/workbench/threads/{result.json()['thread_id']}?project_id=project-a")
    assert history.status_code == 200, history.text
    rows = history.json()['turns']
    assert len(rows) == 2
    parent = next(row for row in rows if row['intent'] == 'multi')
    extra = next(row for row in rows if row['intent'] == 'remember')
    first = parent['receipt']['parts'][0]['receipt']['remember']['item_id']
    second = extra['receipt']['remember']['item_id']
    assert first != second and second is not None
    assert extra['user_text'].strip() == videos[1]
    records = _context(client).records
    assert records.read('workspace_items', first).payload['url'] == videos[0]
    assert records.read('workspace_items', second).payload['url'] == videos[1]
    calls = len(model.calls)
    replay = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'text':url+' 收藏有哪些？'}, headers={'Idempotency-Key':'multi-favorites'})
    assert replay.status_code == 200 and replay.json() == result.json()
    assert len(model.calls) == calls
    assert len(records.list('workspace_items')) == 2
