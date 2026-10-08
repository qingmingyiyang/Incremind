"""Offline CLI checks real comment admission and extraction, never model quality."""
import json
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from backend.memory_app.v2.policies import override
from tools.memory_eval import evaluate


ROOT = Path(__file__).resolve().parents[3]
COMMENT_FIXTURE = ROOT / 'tests/fixtures/memory_eval/comment_supplement.json'


def bundle(cases):
    return {'documents': [], 'insights': [], 'questions': [], 'comment_extraction': cases}


def evaluate_comments(tmp_path, cases, policy='@3'):
    path = tmp_path / 'comments.json'
    path.write_text(json.dumps(bundle(cases), ensure_ascii=False), encoding='utf-8')
    with override(extract=policy):
        return evaluate(path)


def test_cli_must_evaluate_comment_capture_owners_instead_of_ignoring_them(tmp_path):
    body, comment = '店铺目前每日营业。', '2026年10月已关门。'
    raw_body = ('来源：B站视频\n视频链接：https://www.bilibili.com/video/BV1xx411c7mD/\n'
        '标题（页面元数据）：合成材料\n\n视频语音内容（官方字幕）：\n[00:00:01] ' + body)
    raw = raw_body + '\n\n## 评论区\n\n- 8 赞 · ' + comment
    start = len(raw) - len(comment)
    owner = {'source_type': 'original_item', 'source_id': 'owner', 'project_id': 'alpha',
        'revision': 6, 'coordinate_space': 'workspace_source_text_v1', 'text': raw,
        'body': {'start': 0, 'end': len(raw_body)},
        'comment_section': {'start': len(raw_body), 'end': len(raw)},
        'comments': [{'ordinal': 1, 'start': start, 'end': len(raw)}]}
    case = {'id': 'closed', 'project_id': 'alpha', 'neighbors': [], 'projects': [],
        'capture': {'bvid': 'BV1xx411c7mD', 'aid': 123, 'cid': 101, 'title': '合成材料',
            'body': body, 'comments': [{'rpid': 101, 'like_count': 8, 'text': comment}]},
        'synthetic_completion': {'@2': {'insights': [], 'supports': []},
            '@3': {'insights': [], 'supports': []}},
        'expected_owner': owner, 'expected': {'candidates': [], 'supports': []}}
    fixture = tmp_path / 'comments.json'
    fixture.write_text(json.dumps({'documents': [], 'insights': [], 'questions': [],
        'comment_extraction': [case]}, ensure_ascii=False), encoding='utf-8')
    output = tmp_path / 'report.json'
    result = subprocess.run([sys.executable, str(ROOT / 'tools/memory_eval.py'),
        '--cases', str(fixture), '--policy', 'extract=@3', '--output', str(output)],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert 'comment_supplement' in report['categories'], report
    row = report['questions'][0]
    assert row['owner_actual'] == owner
    assert row['owner_valid'] and row['frozen_comments_match'] and row['request_matches']
    assert row['no_auto_publication'] and row['hit']


def test_six_independent_annotations_run_real_capture_freeze_and_pending_proposals(tmp_path):
    fixture = json.loads(COMMENT_FIXTURE.read_text(encoding='utf-8'))
    cases = fixture['comment_extraction']
    assert len(cases) == 6 and {case['id'] for case in cases} == {case['id'] for case in fixture['cases']}
    assert fixture['base_corpus'] == 'corpus.json' and fixture['validation'] == 'pure-protocol-only'
    reports = {policy: evaluate_comments(tmp_path, cases, policy) for policy in ('@2', '@3')}
    assert reports['@2']['categories']['comment_supplement']['hits'] == 2
    assert reports['@3']['categories']['comment_supplement']['hits'] == 6
    for policy, report in reports.items():
        assert report['remote_model_attempts'] == 0 and report['model_attempts'] == 0
        assert report['synthetic_capture_model_attempts'] == 6 and report['synthetic_model_attempts'] == 6
        for row in report['questions']:
            assert row['owner_actual'] == row['expected_owner'] and row['owner_valid']
            assert row['request_matches'] and row['request_unchanged_on_reentry']
            assert row['frozen_neighbors_match'] and row['no_auto_publication']
            assert row['capture_http_requests'] == 5
            assert row['synthetic_capture_model_attempts'] == row['synthetic_model_attempts'] == 1
            assert row['errors'] == []
            if policy == '@3':
                assert row['actual'] == row['expected'] and row['frozen_comments_match']
                for candidate in row['actual']['candidates']:
                    proof = candidate['comment_source']
                    owner = row['owner_actual']
                    assert candidate['state'] == 'pending'
                    assert proof['ordinal'] == 1 and proof['revision'] == owner['revision'] == 6
                    assert owner['text'][proof['start']:proof['end']] == proof['quote']
                    assert owner['comments'][0]['start'] <= proof['start'] < proof['end'] <= owner['comments'][0]['end']
    before = next(row for row in reports['@2']['questions'] if row['id'] == 'neighbor-correction')['actual']['candidates'][0]
    after = next(row for row in reports['@3']['questions'] if row['id'] == 'neighbor-correction')['actual']['candidates'][0]
    assert {key: before[key] for key in ('text', 'conditions', 'relation', 'target_id', 'scope_hint', 'state')} == {
        key: after[key] for key in ('text', 'conditions', 'relation', 'target_id', 'scope_hint', 'state')}
    assert before['target_id'] == after['target_id'] == 'old'
    assert before['comment_source'] is None and after['comment_source'] is not None


@pytest.mark.parametrize('field,value', [('quote', '不在评论原文中'), ('ordinal', 2),
    ('ordinal', True), ('revision', 999), ('source_id', 'foreign-owner')])
def test_forged_model_references_cannot_create_a_candidate_or_repair_the_owner(tmp_path, field, value):
    case = deepcopy(json.loads(COMMENT_FIXTURE.read_text(encoding='utf-8'))['comment_extraction'][0])
    case['synthetic_completion']['@3']['insights'][0]['comment'][field] = value
    row = evaluate_comments(tmp_path, [case])['questions'][0]
    assert row['actual'] == {'candidates': [], 'supports': []}
    assert row['owner_actual'] == case['expected_owner'] and row['owner_valid']
    assert row['frozen_comments_match'] and row['no_auto_publication']
    # StrictInt rejects bool before pure quote decoding; the real caller uses
    # its transport failure code rather than the valid-model decode code.
    error = 'insight_generation_failed' if type(value) is bool else 'insight_invalid_output'
    assert row['synthetic_model_attempts'] == 1 and row['errors'] == [error]
    assert not row['hit']


@pytest.mark.parametrize('revocation', ['private', 'remote_off'])
def test_real_authority_revocation_after_dispatch_discards_the_synthetic_response(tmp_path, revocation):
    case = deepcopy(json.loads(COMMENT_FIXTURE.read_text(encoding='utf-8'))['comment_extraction'][0])
    case['revoke_on_extract'] = revocation
    row = evaluate_comments(tmp_path, [case])['questions'][0]
    assert row['actual'] == {'candidates': [], 'supports': []}
    assert row['owner_actual'] == case['expected_owner'] and row['no_auto_publication']
    assert row['synthetic_model_attempts'] == 1 and row['remote_model_attempts'] == 0
    assert 'insight_generation_failed' in row['errors'] and not row['hit']


def test_expected_coordinates_remain_independent_of_the_accepted_source_proof(tmp_path):
    case = deepcopy(json.loads(COMMENT_FIXTURE.read_text(encoding='utf-8'))['comment_extraction'][0])
    case['expected']['candidates'][0]['comment_source']['end'] += 1
    row = evaluate_comments(tmp_path, [case])['questions'][0]
    assert row['actual']['candidates'][0]['comment_source']['end'] == case['expected']['candidates'][0]['comment_source']['end'] - 1
    assert row['owner_actual'] == case['expected_owner'] and row['request_matches']
    assert not row['hit']


def test_existing_comparative_objects_keep_all_fields_when_at3_adapts_only_provider_body_origin(tmp_path):
    corpus = json.loads((COMMENT_FIXTURE.parent / 'corpus.json').read_text(encoding='utf-8'))
    path = tmp_path / 'comparative.json'
    path.write_text(json.dumps({'documents': [], 'insights': [], 'questions': [],
        'comparative_extraction': corpus['comparative_extraction']}, ensure_ascii=False), encoding='utf-8')
    reports = []
    for policy in ('@2', '@3'):
        with override(extract=policy):
            reports.append(evaluate(path))
    assert all(report['categories']['comparative_extraction']['hits'] == 6 for report in reports)
    costs = {'estimated_prompt_tokens', 'estimated_completion_tokens'}
    deltas = []
    for before, after in zip(reports[0]['questions'], reports[1]['questions'], strict=True):
        assert {key: value for key, value in before.items() if key not in costs | {'policy'}} == {
            key: value for key, value in after.items() if key not in costs | {'policy'}}
        assert before['actual'] == after['actual'] == before['expected'] == after['expected']
        assert all(type(row[key]) is int and row[key] > 0 for row in (before, after) for key in costs)
        delta = {key: after[key] - before[key] for key in costs}
        # These are real UTF-8 estimates of each actual wire. @3 adds its
        # comment prompt and body origin; preserve, never replace, that cost.
        assert delta['estimated_prompt_tokens'] > 0
        assert delta['estimated_completion_tokens'] >= 0
        deltas.append(delta)
    assert any(delta['estimated_completion_tokens'] > 0 for delta in deltas)
