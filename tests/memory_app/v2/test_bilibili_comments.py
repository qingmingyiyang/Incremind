"""Real yt-dlp traversal and real network boundary; only HTTP transport is fake."""
import importlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp.extractor.bilibili import BilibiliBaseIE

from backend.security.network_adapter import BoundedHttpResponse, SafeTextNetworkAdapter


def _reply(identity, likes=1, text='评论原文', *, children=None):
    return {'rpid': identity, 'like': likes, 'content': {'message': text},
        'member': {'uname': '合成人', 'mid': 1}, 'parent': 0, 'replies': children or []}


def _page(replies):
    return {'code': 0, 'data': {'replies': replies}}


def _network(pages, *, addresses=('8.8.8.8',), response_limit=4 * 1024 * 1024):
    calls = []
    def transport(request):
        calls.append(request)
        query = parse_qs(urlsplit(request.target).query)
        page = int(query['pn'][0])
        response = pages[page]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, BoundedHttpResponse):
            return response
        body = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return BoundedHttpResponse(200, {'Content-Type': 'application/json; charset=utf-8'}, body.encode())
    return SafeTextNetworkAdapter(resolver=lambda *_: addresses, transport=transport,
        allowed_hosts=('api.bilibili.com', 'aisubtitle.hdslb.com'), max_redirects=0,
        max_response_bytes=response_limit, timeout_seconds=20), calls


def _capture(network, aid=123):
    return importlib.import_module('backend.memory_app.v2.bilibili_comments').capture_bilibili_comments(network, aid)


def test_inherited_generator_collects_children_and_later_pages_before_top_thirty():
    first = [_reply(n, n, f'第{n}条') for n in range(1, 21)]
    second = [_reply(n, n, f'第{n}条') for n in range(21, 36)]
    second[-1]['replies'] = [_reply(999, 1000, '孩子纠错 😀')]
    network, calls = _network({1: _page(first), 2: _page(second), 3: _page([])})
    comments = _capture(network)
    assert [row['id'] for row in comments] == ['999', *map(str, range(35, 6, -1))]
    assert comments[0] == {'id': '999', 'text': '孩子纠错 😀', 'like_count': 1000}
    assert len(comments) == 30 and len(calls) == 3
    assert all(call.host == 'api.bilibili.com' and call.addresses == ('8.8.8.8',)
        and call.timeout_seconds == 20 and call.max_response_bytes == 4 * 1024 * 1024
        and not any(key.lower() in {'cookie', 'authorization'} for key in call.headers) for call in calls)
    assert [parse_qs(urlsplit(call.target).query)['sort'] for call in calls] == [['2']] * 3
    implementation = importlib.import_module('backend.memory_app.v2.bilibili_comments')._BoundaryCommentsIE
    assert implementation._get_comments is BilibiliBaseIE._get_comments
    assert implementation._get_all_children is BilibiliBaseIE._get_all_children


def test_same_capture_builds_exact_saved_codepoint_sections_and_preserves_string_api():
    from backend.memory_app.v2.bilibili_comments import build_bilibili_comment_source, append_bilibili_comments
    body = '标题 😀\r\n## 评论区\r\n- [ ] 原正文待办'
    pages = {1: _page([_reply(11, 9, '😀纠错\r\n后续文字'), _reply(12, 2, '补充')]), 2: _page([])}
    network, calls = _network(pages)
    result = build_bilibili_comment_source(body, network, 123, maximum=60000)
    assert len(calls) == 2
    raw, sections = result['source_text'], result['sections']
    assert raw.startswith(body + '\n\n## 评论区\n\n')
    assert sections['body'] == {'start': 0, 'end': len(body)}
    assert sections['comment_section'] == {'start': len(body), 'end': len(raw)}
    assert [raw[row['start']:row['end']] for row in sections['comments']] == ['😀纠错\n  后续文字', '补充']
    assert [(row['ordinal'], row['rpid'], row['like_count']) for row in sections['comments']] == [(1, '11', 9), (2, '12', 2)]
    again, _ = _network(pages)
    assert append_bilibili_comments(body, again, 123, maximum=60000) == raw


def test_ties_are_stable_and_duplicate_id_keeps_first_occurrence_across_pages():
    network, calls = _network({1: _page([_reply(1, 5, '首条', children=[_reply(2, 5, '孩子')]),
        _reply(3, 5, '同行')]), 2: _page([_reply(1, 999, '重复更高赞'), _reply(4, 9, '后页')]), 3: _page(None)})
    assert _capture(network) == ({'id': '4', 'text': '后页', 'like_count': 9},
        {'id': '1', 'text': '首条', 'like_count': 5}, {'id': '2', 'text': '孩子', 'like_count': 5},
        {'id': '3', 'text': '同行', 'like_count': 5})
    assert len(calls) == 3


@pytest.mark.parametrize('likes', [None, True, False, -1, '7', 1.5, {}])
def test_bad_like_is_omitted_and_never_invented_as_zero(likes):
    network, _ = _network({1: _page([_reply(1, likes), _reply(2, 0, '真实零赞')]), 2: _page([])})
    assert _capture(network) == ({'id': '2', 'text': '真实零赞', 'like_count': 0},)


def test_first_duplicate_with_bad_like_cannot_be_replaced_by_later_valid_like():
    network, _ = _network({1: _page([_reply(1, None)]), 2: _page([_reply(1, 99)]), 3: _page([])})
    assert _capture(network) == ()


@pytest.mark.parametrize('text', [None, '', ' \t', 123, {}, '\x00'])
def test_bad_text_is_omitted_without_losing_other_complete_comments(text):
    network, _ = _network({1: _page([_reply(1, 9, text), _reply(2, 1, '完整原文')]), 2: _page([])})
    assert _capture(network) == ({'id': '2', 'text': '完整原文', 'like_count': 1},)


@pytest.mark.parametrize('payload', ['not json', {}, {'code': -412, 'data': {'replies': []}},
    {'code': 0, 'data': {}}, {'code': 0, 'data': {'replies': {}}}, _page(['bad row']),
    _page([{'rpid': 1, 'like': 2, 'content': {'message': '原文'}, 'replies': 'bad children'}])])
def test_bad_response_is_not_mistaken_for_completed_pagination(payload):
    network, calls = _network({1: _page([_reply(1, 99)]), 2: payload})
    assert _capture(network) == ()
    assert len(calls) == 2


@pytest.mark.parametrize('children', [0, False, '', {}, 5, 'bad children'])
def test_falsy_bad_children_are_not_silently_treated_as_an_empty_list(children):
    reply = _reply(1, 9, '不能借坏分页字段成为完整评论')
    reply['replies'] = children
    network, calls = _network({1: _page([reply]), 2: _page([])})
    assert _capture(network) == () and len(calls) == 1


@pytest.mark.parametrize('identity', [None, True, -1, 0, '', {}])
def test_bad_comment_id_cannot_create_or_replace_a_ranked_comment(identity):
    network, _ = _network({1: _page([_reply(identity, 999), _reply(2, 1, '保留正确身份')]), 2: _page([])})
    assert _capture(network) == ({'id': '2', 'text': '保留正确身份', 'like_count': 1},)


def test_append_keeps_every_original_crlf_and_unicode_byte():
    network, calls = _network({1: _page([_reply(1, 5, '完整评论😀')]), 2: _page([])})
    source = '# 中文原文😀\r\n\r\n甲段。\r\n- [X] 旧待办\r\n末段无换行'
    module = importlib.import_module('backend.memory_app.v2.bilibili_comments')
    combined = module.append_bilibili_comments(source, network, 123, maximum=60_000)
    assert combined == source + '\n\n## 评论区\n\n- 5 赞 · 完整评论😀'
    raw = source.encode('utf-8')
    assert combined.encode('utf-8')[:len(raw)] == raw and len(calls) == 2


def test_failure_after_a_valid_page_discards_the_partial_comment_set():
    network, calls = _network({1: _page([_reply(1, 99)]), 2: OSError('synthetic provider failure')})
    assert _capture(network) == () and len(calls) == 2


def test_tenth_page_can_prove_pagination_complete():
    pages = {n: _page([_reply(n, n)]) for n in range(1, 10)}
    pages[10] = _page([])
    network, calls = _network(pages)
    assert [row['id'] for row in _capture(network)] == list(map(str, range(9, 0, -1)))
    assert len(calls) == 10


def test_ten_nonempty_pages_fail_closed_without_requesting_an_eleventh():
    network, calls = _network({n: _page([_reply(n, n)]) for n in range(1, 11)})
    assert _capture(network) == () and len(calls) == 10


def test_exactly_two_thousand_unique_candidates_can_complete_then_rank():
    network, calls = _network({1: _page([_reply(n, n) for n in range(1, 2001)]), 2: _page([])})
    assert [row['id'] for row in _capture(network)] == list(map(str, range(2000, 1970, -1)))
    assert len(calls) == 2


def test_two_thousand_and_one_unique_candidates_discard_every_comment():
    network, calls = _network({1: _page([_reply(n, n) for n in range(1, 2002)])})
    assert _capture(network) == () and len(calls) == 1


def test_children_share_the_unique_candidate_limit():
    network, calls = _network({1: _page([_reply(1, 1,
        children=[_reply(n, n) for n in range(2, 2002)])])})
    assert _capture(network) == () and len(calls) == 1


def test_duplicate_rows_do_not_consume_the_unique_candidate_budget():
    network, calls = _network({1: _page([_reply(1, 2)] * 2100), 2: _page([])})
    assert _capture(network) == ({'id': '1', 'text': '评论原文', 'like_count': 2},)
    assert len(calls) == 2


@pytest.mark.parametrize('aid', [None, False, True, 0, -1, '123'])
def test_missing_or_invalid_aid_performs_no_comment_request(aid):
    network, calls = _network({})
    assert _capture(network, aid) == () and calls == []


def test_real_boundary_rejects_redirect_before_a_second_host_is_contacted():
    redirect = BoundedHttpResponse(302, {'Location': 'https://other.invalid/'}, b'')
    network, calls = _network({1: redirect})
    assert _capture(network) == () and len(calls) == 1


def test_real_boundary_rejects_private_address_before_transport():
    network, calls = _network({}, addresses=('127.0.0.1',))
    assert _capture(network) == () and calls == []


def test_real_boundary_rejects_oversized_response():
    network, calls = _network({1: _page([_reply(1, 2, '大' * 100)])}, response_limit=100)
    assert _capture(network) == () and len(calls) == 1
