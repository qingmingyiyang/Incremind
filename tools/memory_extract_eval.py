"""Synthetic transport checks extraction binding, not model semantic judgment."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import override, version
from core.storage_provider.connection_scope import with_connection_scope
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.litellm_gateway import _estimate_input_tokens


def evaluate_cases(root, cases, seeder):
    return [_evaluate_case(root, case, seeder) for case in cases]


def _extract_key(records):
    return next((row for row in records.list('v2_memory_turn_keys')
        if row.payload['identity']['kind'] == 'memory.propose_insights'), None)


def _frozen_neighbors(records, key, sent):
    frozen = records.read('v2_extract_inputs', key.object_id)
    refs = {row['id'] for row in key.payload['request']['privacy']['material_refs']
        if row['type'] == 'recognition'}
    return (frozen is not None and frozen.payload['neighbors'] == sent.get('neighbors')
        and refs == {row['id'] for row in frozen.payload['neighbors']})


@with_connection_scope
def _evaluate_case(root, case, seeder):
    own = Path(root) / case['id']
    source = case['material']
    fixture = {'documents': [{'id': 'material', 'source_id': 'source-' + case['id'],
        'project_id': source['project_id'], 'title': '合成材料', 'summary': source['text'],
        'body': source['text'], 'original': source['text'], 'created_at': '2026-10-03T00:00:00+00:00'}],
        'insights': [{'id': row['id'], 'project_id': row['project_id'], 'text': row['text']}
            for row in case['neighbors']]}
    query, identities = seeder(own, fixture)
    with query.records.begin() as tx:
        for project in case['projects']:
            tx.put('v2_projects', project['id'], {'name': project['name'], 'scenes': [],
                'builtin': None}, expected_revision=0)
        tx.commit()
    calls, tokens = [], []
    def complete(**request):
        messages = request['messages']
        calls.append(messages)
        comparative = 'neighbors' in json.loads(messages[-1]['content'])
        output = deepcopy(case['synthetic_completion']['comparative' if comparative else 'legacy'])
        # Only the external synthetic provider adapts its ordinary body response.
        # Use the already frozen request; @1/@2 retain their exact six fields.
        key = _extract_key(query.records)
        if key.payload['request']['policy_versions']['extract'] == '@3':
            for row in output['insights']:
                row['origin'] = 'body'
        text = json.dumps(output, ensure_ascii=False)
        prompt = _estimate_input_tokens(messages)
        completion = _estimate_input_tokens([{'role': 'assistant', 'content': text}])
        tokens.append((prompt, completion))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                'total_tokens': prompt + completion}}
    models = ModelConfiguration(query.records, own, InMemorySecretStore(), completion_fn=complete)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1',
        'model': 'synthetic-extraction', 'api_key': 'synthetic-only', 'allow_remote': True,
        'expected_revision': 0})
    selected = version('extract')
    candidates = generate_insights(models, query.service, query.documents,
        source['project_id'], identities['material'])
    actual = {'candidates': [{'text': row['text'], 'conditions': row['conditions'],
        'relation': row.get('hint', {}).get('relation'),
        'target_id': row.get('hint', {}).get('target_id'),
        'scope_hint': row.get('hint', {}).get('scope_hint')} for row in candidates],
        'supports': [{'relation': 'duplicate_of', 'target_id': row.payload['target_id'],
            'evidence': row.payload['evidence']} for row in query.records.list('v2_insight_evidence_support')]}
    frozen_match = None
    if selected in {'@2', '@3'}:
        key = _extract_key(query.records)
        sent = json.loads(calls[0][-1]['content']) if calls else {}
        frozen_match = _frozen_neighbors(query.records, key, sent)
    return {'id': case['id'], 'category': 'comparative_extraction',
        'expected': case['expected'], 'actual': actual,
        'hit': actual == case['expected'] and (selected not in {'@2', '@3'} or frozen_match) and len(calls) == 1,
        'frozen_neighbors_match': frozen_match, 'policy': selected,
        'synthetic_model_attempts': len(calls), 'remote_model_attempts': 0,
        'estimated_prompt_tokens': sum(row[0] for row in tokens),
        'estimated_completion_tokens': sum(row[1] for row in tokens)}


def evaluate_comment_cases(root, cases, seeder):
    return [_evaluate_comment_case(root, case, seeder) for case in cases]


def _comment_transport(capture, calls):
    """Replace only the external official HTTP/DNS responses, never a parser."""
    from backend.security.network_adapter import BoundedHttpResponse

    def send(request):
        calls.append({'host': request.host, 'path': urlsplit(request.target).path})
        parsed = urlsplit(request.target)
        if request.host == 'api.bilibili.com' and parsed.path == '/x/web-interface/view':
            response = {'code': 0, 'data': {'bvid': capture['bvid'], 'aid': capture['aid'],
                'title': capture['title'], 'desc': '', 'duration': 12, 'owner': {},
                'pages': [{'page': 1, 'cid': capture['cid'], 'duration': 12, 'part': capture['title']}]}}
        elif request.host == 'api.bilibili.com' and parsed.path == '/x/player/v2':
            response = {'code': 0, 'data': {'subtitle': {'subtitles': [
                {'lan': 'zh-Hans', 'subtitle_url': 'https://aisubtitle.hdslb.com/bfs/ai_subtitle/eval.json'}]}}}
        elif request.host == 'aisubtitle.hdslb.com' and parsed.path == '/bfs/ai_subtitle/eval.json':
            response = {'body': [{'from': 1.2, 'to': 2.5, 'content': capture['body']}]}
        elif request.host == 'api.bilibili.com' and parsed.path == '/x/v2/reply':
            query = parse_qs(parsed.query)
            assert query['oid'] == [str(capture['aid'])] and query['sort'] == ['2']
            page = int(query['pn'][0])
            assert page in {1, 2}, 'synthetic capture exceeded its two pages'
            replies = [{'rpid': row['rpid'], 'like': row['like_count'],
                'content': {'message': row['text']}, 'member': {}, 'parent': 0, 'replies': []}
                for row in capture['comments']] if page == 1 else []
            response = {'code': 0, 'data': {'replies': replies}}
        else:
            raise AssertionError('unexpected external HTTP boundary')
        return BoundedHttpResponse(200, {'Content-Type': 'application/json; charset=utf-8'},
            json.dumps(response, ensure_ascii=False).encode('utf-8'))
    return send


def _logical_proof(value, item_id):
    if value is None:
        return None
    result = deepcopy(value)
    if result.get('id') == item_id:
        result['id'] = 'owner'
    return result


@with_connection_scope
def _evaluate_comment_case(root, case, seeder):
    from fastapi import FastAPI
    from backend.memory_app.workspace import install_workspace_routes
    from backend.memory_app.v2.auto_confirm import process_and_confirm
    from backend.memory_app.v2.source_sections import resolve_comment_sources
    from backend.security import network_adapter

    own = Path(root) / case['id']
    project, selected = case['project_id'], version('extract')
    query, _ = seeder(own, {'documents': [], 'insights': case['neighbors']})
    with query.records.begin() as tx:
        for row in case['projects']:
            tx.put('v2_projects', row['id'], {'name': row['name'], 'scenes': [], 'builtin': None}, expected_revision=0)
        tx.commit()
    network_calls, capture_calls, calls, tokens = [], [], [], []

    def complete(**request):
        messages = request['messages']
        if '"insights"' not in str(messages[0]['content']):
            capture_calls.append(messages)
            output = {'title': case['capture']['title'], 'summary': case['capture']['body'],
                'topics': [], 'facts': [], 'todos': [], 'uncertainties': [],
                'people': [], 'dates': [], 'suggestions': []}
        else:
            calls.append(messages)
            key = _extract_key(query.records)
            frozen_version = key.payload['request']['policy_versions']['extract']
            output = deepcopy(case['synthetic_completion'][frozen_version])
            sent = json.loads(messages[-1]['content'])
            for row in output['insights']:
                if row.get('origin') == 'comment':
                    source = sent['comment_sources'][0]
                    if row['comment']['source_id'] == '$owner':
                        row['comment']['source_id'] = source['source_id']
                    if row['comment']['revision'] == '$owner_revision':
                        row['comment']['revision'] = source['revision']
            # Boundary fixtures can revoke actual authority while a response is
            # in flight; the production validation still decides acceptance.
            if case.get('revoke_on_extract') == 'private':
                from backend.memory_app.v2.privacy import set_private_project
                set_private_project(query.records, project, True, 0)
            elif case.get('revoke_on_extract') == 'remote_off':
                models.update('generation', {'allow_remote': False, 'expected_revision': 1})
        text = json.dumps(output, ensure_ascii=False)
        prompt = _estimate_input_tokens(messages)
        completion = _estimate_input_tokens([{'role': 'assistant', 'content': text}])
        if '"insights"' in str(messages[0]['content']):
            tokens.append((prompt, completion))
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                'total_tokens': prompt + completion}}

    models = ModelConfiguration(query.records, own, InMemorySecretStore(), completion_fn=complete)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic-extraction',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    with patch.dict(os.environ, {'CHRIPTMAS_APP_ROOT': str(own)}):
        domains = install_workspace_routes(FastAPI(), runtime_root=own, records=query.records,
            models=models, documents=query.documents, service=query.service)
        with patch.object(network_adapter, '_resolve_addresses', return_value=('8.8.8.8',)), \
                patch.object(network_adapter, '_perform_pinned_request', _comment_transport(case['capture'], network_calls)):
            item = asyncio.run(domains.intake.add_link({'project_id': project,
                'url': 'https://www.bilibili.com/video/' + case['capture']['bvid'] + '/'}))
            admitted = asyncio.run(process_and_confirm(domains, item['id'], project))
        if admitted['status'] != 'confirmed':
            raise AssertionError('synthetic comment admission failed')
        frozen_owner = resolve_comment_sources(query.records, query.documents, project, admitted['document_id'])
        source = frozen_owner['sources'][0]
        owner_actual = {**deepcopy(source), 'source_id': 'owner' if source['source_id'] == item['id'] else source['source_id']}
        original = domains.confirmations.source_store.read('sources', admitted['source_id'])
        operation = query.records.read('workspace_confirmation_operations', 'confirm-' + item['id'])
        owner_valid = (operation is not None and operation.payload['state'] == 'committed'
            and original['project_id'] == project and original['metadata']['content_snapshot'] == source['text']
            and bool(frozen_owner['bindings']) and len(network_calls) == 5)
        before = {collection: [(row.object_id, row.revision) for row in query.records.list(collection)]
            for collection in ('recognitions', 'recognition_relations')}
        errors = []
        candidates = generate_insights(models, query.service, query.documents, project, admitted['document_id'], on_error=errors.append)
        key = _extract_key(query.records)
        request = deepcopy(key.payload['request']) if key else None
        with override(extract='@2' if selected == '@3' else '@3'):
            replay = generate_insights(models, query.service, query.documents, project, admitted['document_id'], on_error=errors.append)
        same_request = key is not None and _extract_key(query.records).payload['request'] == request and replay == candidates

    actual = {'candidates': [], 'supports': [{'relation': 'duplicate_of', 'target_id': row.payload['target_id'],
        'evidence': row.payload['evidence']} for row in query.records.list('v2_insight_evidence_support')]}
    for row in candidates:
        marker = query.records.read('v2_comment_candidates', row['id'])
        proof = marker.payload if marker else {}
        actual['candidates'].append({'text': row['text'], 'conditions': row['conditions'],
            'relation': row.get('hint', {}).get('relation'), 'target_id': row.get('hint', {}).get('target_id'),
            'scope_hint': row.get('hint', {}).get('scope_hint'), 'state': row['state'],
            'comment_source': _logical_proof(proof.get('comment_source'), item['id']),
            'comparison_source': _logical_proof(proof.get('comparison_source'), item['id'])})
    sent = json.loads(calls[0][-1]['content']) if calls else {}
    comment_inputs = query.records.read('v2_comment_extract_inputs', key.object_id) if key else None
    frozen_comments = None
    if selected == '@3':
        frozen_comments = (comment_inputs is not None and comment_inputs.payload == frozen_owner
            and sent.get('comment_sources') == frozen_owner['sources'])
    frozen_neighbors = _frozen_neighbors(query.records, key, sent) if key else False
    request_matches = (request is not None and request['policy_versions']['extract'] == selected
        and frozen_neighbors and same_request and len(calls) == 1)
    if selected == '@3' and request_matches:
        refs = request['privacy']['material_refs']
        request_matches = all(any(ref['type'] == value['source_type'] and ref['id'] == value['source_id']
            and ref['revision'] == value['revision'] for ref in refs) for value in frozen_owner['sources'])
    no_publication = before == {collection: [(row.object_id, row.revision) for row in query.records.list(collection)]
        for collection in before} and all(row['state'] == 'pending' for row in candidates)
    return {'id': case['id'], 'category': 'comment_supplement', 'policy': selected,
        'expected': case['expected'], 'actual': actual, 'expected_owner': case['expected_owner'],
        'owner_actual': owner_actual, 'owner_valid': owner_valid,
        'frozen_comments_match': frozen_comments, 'frozen_neighbors_match': frozen_neighbors,
        'request_matches': request_matches, 'request_unchanged_on_reentry': same_request,
        'no_auto_publication': no_publication, 'errors': sorted(set(errors)),
        'hit': actual == case['expected'] and owner_actual == case['expected_owner'] and owner_valid
            and request_matches and no_publication and (selected != '@3' or frozen_comments),
        'synthetic_model_attempts': len(calls), 'synthetic_capture_model_attempts': len(capture_calls),
        'capture_http_requests': len(network_calls), 'remote_model_attempts': 0,
        'estimated_prompt_tokens': sum(row[0] for row in tokens),
        'estimated_completion_tokens': sum(row[1] for row in tokens)}
