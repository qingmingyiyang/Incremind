from tests.memory_app.v2.test_workbench_remember import env, wait
import pytest

URL = 'https://space.bilibili.com/123/favlist?fid=456'

def videos(count):
    return [f'https://www.bilibili.com/video/BV1{index:09d}' for index in range(count)]

@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    from backend.memory_app import workspace_links, workspace_bilibili_media
    def denied(*args, **kwargs): raise ValueError('synthetic_network_only')
    monkeypatch.setattr(workspace_links, '_fetch_url', denied)
    monkeypatch.setattr(workspace_bilibili_media, 'read_bilibili_media', lambda url, root, **kwargs: {'source_text': '原文证据', 'title': '视频', 'canonical_url': url, 'acquisition_method': 'official_subtitle', 'content_kind': 'video'})


def configure(env, count=3):
    env.model.public = lambda: {'generation': {'base_url': 'https://example.invalid/v1', 'allow_remote': True, 'revision': 1}, 'generation_mode': {'revision': 0}}
    env.app.state.bilibili_favorites_discovery = lambda **kwargs: videos(count)

def post(env, **body):
    return env.http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'intent': 'remember', 'text': URL, **body})

def read(env, result):
    response = env.http.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=alpha")
    assert response.status_code == 200, response.text
    return response.json()['turns']

def test_three_videos_are_independent_receipts_in_one_thread(env):
    configure(env)
    response = post(env)
    assert response.status_code == 200, response.text
    rows = read(env, response.json())
    assert len(rows) == 3
    assert [row['user_text'] for row in rows] == videos(3)
    assert len({row['receipt']['remember']['item_id'] for row in rows}) == 3
    assert len(env.records.list('workspace_items')) == 3

def test_limit_and_first_title_survive_thread_refresh(env):
    configure(env, 53)
    response = post(env)
    assert response.status_code == 200, response.text
    rows = read(env, response.json())
    assert len(rows) == 50
    assert rows[0]['receipt']['remember']['title'].startswith('（共 53，先取 50）')

def test_discovery_failure_creates_one_failed_receipt_and_no_parent_source(env):
    configure(env)
    def fail(**kwargs): raise RuntimeError('sk-test-DO-NOT-LEAK')
    env.app.state.bilibili_favorites_discovery = fail
    response = post(env)
    assert response.status_code == 200, response.text
    rows = read(env, response.json())
    assert len(rows) == 1 and rows[0]['receipt']['remember']['state'] == 'failed'
    assert rows[0]['receipt']['remember']['error'] == 'favorites_discovery_failed'
    assert env.records.list('workspace_items') == ()
    assert 'sk-test-DO-NOT-LEAK' not in str(rows)

def test_unavailable_discovery_returns_503_without_work(env):
    configure(env)
    del env.app.state.bilibili_favorites_discovery
    assert post(env).status_code == 503
    assert env.records.list('v2_turns') == ()

def test_repeat_collection_reuses_video_items(env):
    configure(env)
    first = post(env).json()
    response = post(env, thread_id=first['thread_id'])
    assert response.status_code == 200
    rows = read(env, response.json())
    assert len(rows) == 6
    assert len({row['receipt']['remember']['item_id'] for row in rows}) == 3


def test_failed_discovery_retry_reuses_first_position_without_parent(env):
    configure(env)
    env.app.state.bilibili_favorites_discovery = lambda **kwargs: []
    failed = post(env).json()
    configure(env)
    response = env.http.post(f"/api/v2/workbench/turns/{failed['turn']['id']}/retry", json={'project_id': 'alpha'})
    assert response.status_code == 200, response.text
    rows = read(env, failed)
    assert len(rows) == 3 and rows[0]['id'] == failed['turn']['id']
    assert all(row['receipt']['remember']['item_id'] for row in rows)
    assert len(env.records.list('workspace_items')) == 3
    assert env.http.post(f"/api/v2/workbench/turns/{failed['turn']['id']}/retry", json={'project_id': 'alpha'}).status_code == 409


def test_single_video_failure_can_retry_without_blocking_siblings(env, monkeypatch):
    from backend.memory_app import workspace_bilibili_media
    configure(env)
    fail = [True]
    def reader(url, root, **kwargs):
        if url == videos(3)[0] and fail[0]: raise ValueError('synthetic_video_failed')
        return {'source_text': '原文证据', 'title': '视频', 'canonical_url': url, 'acquisition_method': 'official_subtitle', 'content_kind': 'video'}
    monkeypatch.setattr(workspace_bilibili_media, 'read_bilibili_media', reader)
    first = post(env).json()
    rows = read(env, first)
    final = [wait(env, {'thread_id': first['thread_id'], 'turn': row}) for row in rows]
    assert [row['receipt']['remember']['state'] for row in final] == ['failed', 'done', 'done']
    fail[0] = False
    retry = env.http.post(f"/api/v2/workbench/turns/{first['turn']['id']}/retry", json={'project_id': 'alpha'})
    assert retry.status_code == 200, retry.text
    assert wait(env, first)['receipt']['remember']['state'] == 'done'
    assert len(env.records.list('workspace_items')) == 3


def test_idempotent_request_does_not_expand_twice(env):
    configure(env)
    body = {'project_id': 'alpha', 'intent': 'remember', 'text': URL}
    first = env.http.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'favorite-once'})
    second = env.http.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'favorite-once'})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(read(env, first.json())) == 3


def test_concurrent_collections_share_project_video_items(env):
    from concurrent.futures import ThreadPoolExecutor
    configure(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(env), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert len(env.records.list('workspace_items')) == 3
    assert len(env.records.list('v2_turns')) == 6


def test_privacy_and_disabled_egress_block_discovery_before_network(env):
    from backend.memory_app.v2.privacy import set_private_project
    configure(env)
    calls = []
    env.app.state.bilibili_favorites_discovery = lambda **kwargs: calls.append(1) or videos(3)
    set_private_project(env.records, 'alpha', True, 0)
    assert post(env).status_code == 409 and calls == []
    assert env.records.list('workspace_items') == ()


def test_discovery_adapter_reuses_real_service_with_request_local_store(monkeypatch, tmp_path):
    from backend.api import favorites_discovery as adapter
    from backend.api.bilibili_favorite_collection import build_bilibili_favorite_collection_service
    from tests.backend.unit.api.test_bilibili_favorite_collection import _Network, _page
    from dataclasses import replace
    provider = _Network([_page({'id': 1, 'type': 2, 'bvid': 'BV1xx411c7mD', 'title': '视频'}, has_more=False, count=1)])
    monkeypatch.setattr(adapter, 'build_bilibili_favorite_collection_service', lambda store, **kwargs: replace(build_bilibili_favorite_collection_service(store, **kwargs), network=provider))
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    assert adapter.discover_bilibili_favorites(url=URL, project_id='alpha') == ['https://www.bilibili.com/video/BV1xx411c7mD/']
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('page,expected', [(1, 3), (2, 4)])
def test_existing_video_identity_normalizes_host_but_keeps_page(env, page, expected):
    import asyncio
    configure(env)
    url = (videos(3)[0].replace('www.bilibili.com', 'bilibili.com') if page == 1 else videos(3)[0]) + f'?p={page}'
    old = asyncio.run(env.domains.intake.add_link({'project_id': 'alpha', 'url': url}))
    result = post(env)
    assert result.status_code == 200, result.text
    rows = read(env, result.json())
    assert len(env.records.list('workspace_items')) == expected
    assert (rows[0]['receipt']['remember']['item_id'] == old['id']) is (page == 1)


def test_discovery_retry_preserves_position_before_later_messages(env):
    configure(env)
    env.app.state.bilibili_favorites_discovery = lambda **kwargs: []
    failed = post(env).json()
    later = post(env, text='原文证据', thread_id=failed['thread_id']).json()
    configure(env)
    response = env.http.post(f"/api/v2/workbench/turns/{failed['turn']['id']}/retry", json={'project_id': 'alpha'})
    assert response.status_code == 200
    rows = read(env, failed)
    assert rows[0]['id'] == failed['turn']['id'] and rows[0]['created_at'] == failed['turn']['created_at']
    assert rows[1]['id'] == later['turn']['id']
