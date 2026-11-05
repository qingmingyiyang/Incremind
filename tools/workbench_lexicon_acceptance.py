import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import monotonic
import traceback
from uuid import uuid4

import httpx
import uvicorn

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPOSITORY), str(REPOSITORY / 'src')]
DIAGNOSTIC_OUTPUT = None


def failure_details(error):
    result = {'error_type': type(error).__name__, 'error_code': 'unexpected_exception'}
    message = str(error)
    if (message.startswith(('acceptance_', 'actual_', 'dedicated_', 'private_real_', 'real_model_', 'duplicate_'))
            and re.fullmatch(r'[a-z0-9_:]{1,160}', message)):
        result['error_code'] = message
    frames = traceback.extract_tb(error.__traceback__)
    owned = [frame for frame in frames if Path(frame.filename).resolve().is_relative_to(REPOSITORY)]
    if owned:
        frame = owned[-1]
        result['location'] = {'file': str(Path(frame.filename).resolve().relative_to(REPOSITORY)),
            'line': frame.lineno, 'function': frame.name}
    if isinstance(error, httpx.HTTPStatusError):
        result['http_status'] = error.response.status_code
    return result


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('acceptance_case_list_required')
    identities = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('case_id'), str)
                or not re.fullmatch(r'[a-z0-9_-]{1,64}', case['case_id'])):
            raise ValueError('acceptance_case_identity_invalid')
        if case['case_id'] in identities:
            raise ValueError('acceptance_case_identity_duplicate')
        identities.add(case['case_id'])
        if not isinstance(case.get('decision'), str) or case['decision'] not in {'approve', 'cancel', 'read'}:
            raise ValueError('acceptance_case_decision_invalid')
        if 'intent' in case and case['intent'] != 'do':
            raise ValueError('acceptance_case_intent_invalid')
    return cases

def load_model_factory(specification):
    filename, name = specification.rsplit(':', 1)
    spec = importlib.util.spec_from_file_location('lexicon_acceptance_private_configuration', filename)
    if spec is None or spec.loader is None:
        raise ValueError('model_factory_module_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def defaults(identity):
    original, renamed, cancelled = ('验收原词' + identity, '验收新词' + identity, '取消验收词' + identity)
    return [{'case_id': 'approve', 'decision': 'approve',
        'seed_operations': [{'action': 'add', 'term': original, 'weight': 3}],
        'text': f'请把热词表中的{original}改成{renamed}，权重设为5，展示变更预览并等待我点击确认保存。',
        'changes': [{'action': 'rename', 'before': {'term': original, 'weight': 3, 'status': 'active'},
            'after': {'term': renamed, 'weight': 5, 'status': 'active'}}]},
        {'case_id': 'cancel', 'decision': 'cancel',
        'text': f'请添加热词{cancelled}，权重为4，展示变更预览并等待我点击确认保存。',
        'changes': [{'action': 'add', 'before': None,
            'after': {'term': cancelled, 'weight': 4, 'status': 'active'}}]}]


def unpack_turn(response):
    response.raise_for_status()
    result = response.json()
    turn = result['turn']
    if 'thread_id' not in turn:
        turn = {**turn, 'thread_id': result['thread_id']}
    if not isinstance(turn.get('id'), str) or not isinstance(turn.get('thread_id'), str):
        raise ValueError('actual_workbench_response_has_no_turn_identity')
    return turn


async def read_turn(client, turn, project):
    response = await client.get('/api/v2/workbench/threads/' + turn['thread_id'], params={'project_id': project})
    response.raise_for_status()
    return next(row for row in response.json()['turns'] if row['id'] == turn['id'])


async def read_words(client):
    response = await client.get('/api/v2/asr/lexicon')
    response.raise_for_status()
    return response.json()


def changes_equal(actual, expected):
    def signatures(changes):
        values = [json.dumps(change, ensure_ascii=False, sort_keys=True) for change in changes]
        if len(values) != len(set(values)):
            raise AssertionError('duplicate_maintenance_change')
        return Counter(values)
    return signatures(actual) == signatures(expected)


async def wait_turn(client, turn, project, meter, *, preview=False, seconds=240, failure_path=None):
    deadline = monotonic() + seconds
    current = None
    while monotonic() < deadline:
        if meter.halted:
            raise RuntimeError('acceptance_transport_stopped')
        current = await read_turn(client, turn, project)
        state = current['receipt']['do']['state']
        if preview and state == 'waiting_approval' and current['receipt']['do'].get('lexicon_preview'):
            return current
        if state in {'done', 'failed', 'partial', 'cancelled', 'interrupted', 'stopped'}:
            if preview:
                if failure_path is not None:
                    save(failure_path, current)
                raise RuntimeError('actual_task_ended_before_lexicon_preview:' + state)
            return current
        await asyncio.sleep(1)
    if failure_path is not None and current is not None:
        save(failure_path, current)
    raise TimeoutError('actual_workbench_state_timeout')


def native_proof(application, turn, decision):
    execution = application.state.recognition_records.read('v2_task_executions', turn['id'])
    session = execution.payload['request']['session_id']
    database = application.state.recognition_runtime_root / '.rebuild-data' / 'ai-turns.sqlite3'
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        events = [json.loads(row[0]) for row in connection.execute(
            "SELECT event_json FROM ai_turn_events WHERE json_extract(event_json,'$.session_id')=?", (session,))]
    kinds = Counter(row['type'] for row in events)
    if decision != 'read' and kinds['approval.required'] < 1:
        raise AssertionError('actual_approval_event_missing')
    if decision == 'read' and kinds['approval.required']:
        raise AssertionError('actual_read_requested_approval')
    if decision == 'approve' and kinds['approval.resolved'] < 1:
        raise AssertionError('actual_approval_resolution_missing')
    if decision == 'cancel' and kinds['turn.cancelled'] < 1:
        raise AssertionError('actual_cancellation_event_missing')
    return dict(kinds)


def verify_words(before, after, changes):
    assert after['revision'] > before['revision'], 'saved_revision_did_not_advance'
    prior = {row['term']: row for row in before['terms']}
    current = {row['term']: row for row in after['terms']}
    affected = set()
    for change in changes:
        old, new = change['before'], change['after']
        actual = current[new['term']]
        assert all(actual[name] == new[name] for name in ('term', 'weight', 'status'))
        affected.add(actual['id'])
        if old is not None:
            assert actual['id'] == prior[old['term']]['id'], 'existing_term_identity_changed'
            if old['term'] != new['term']:
                assert old['term'] not in current
    after_by_id = {row['id']: row for row in after['terms']}
    assert len(after['terms']) == len(before['terms']) + sum(change['before'] is None for change in changes)
    assert all(after_by_id[row['id']] == row for row in before['terms'] if row['id'] not in affected)


async def post_action(client, turn, body, key):
    return await client.post('/api/v2/workbench/turns/' + turn['id'] + '/lexicon-action',
        json=body, headers={'Idempotency-Key': key})


async def wait_ui(output, binding, result, *, seconds):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        signal = output / 'ui-complete.json'
        if signal.exists():
            acknowledgement = json.loads(signal.read_text(encoding='utf-8'))
            if all(acknowledgement.get(name) == value for name, value in binding.items()):
                if acknowledgement.get('passed') is not True:
                    save(output / (binding['case_id'] + '-ui-failed.json'), acknowledgement)
                    raise AssertionError('actual_ui_verification_failed')
                evidence = (output / acknowledgement.get('evidence_path', 'ui-case-' + binding['case_id'] + '.json')).resolve()
                if not evidence.is_relative_to(output) or not evidence.is_file():
                    raise AssertionError('actual_ui_evidence_missing')
                evidence_report = json.loads(evidence.read_text(encoding='utf-8'))
                if not all(evidence_report.get(name) == value for name, value in binding.items()):
                    raise AssertionError('actual_ui_evidence_identity_changed')
                result['ui_evidence'] = str(evidence.relative_to(output))
                result['ui_passed'] = True
                save(output / (binding['case_id'] + '-result.json'), result)
                return result
        await asyncio.sleep(1)
    raise TimeoutError('actual_ui_completion_timeout')


async def run_case(application, client, meter, case, project, output, actions, *, ui_seconds):
    case_id = case['case_id']
    meter.label = case_id
    lexicon = application.state.asr_lexicon
    if case.get('seed_operations'):
        lexicon.change_terms(operations=case['seed_operations'], expected_revision=lexicon.current_revision(), preview=False)
    before = await read_words(client)
    response = await client.post('/api/v2/workbench/turns',
        json={'project_id': project, 'text': case['text'], **({'intent': case['intent']} if 'intent' in case else {})},
        headers={'Accept': 'application/json', 'Idempotency-Key': 'lexicon-submit-' + uuid4().hex})
    turn = unpack_turn(response)
    save(output / (case_id + '-submitted.json'), response.json())
    execution = application.state.recognition_records.read('v2_task_executions', turn['id'])
    frozen = json.loads(execution.payload['request']['input']['text'])
    expected_operation = 'lexicon.read' if case['decision'] == 'read' else 'lexicon.maintain'
    assert frozen.get('data_operation') == expected_operation, 'actual_task_purpose_not_frozen'
    assert (frozen.get('outcome_input') or {}).get('document_id') is None, 'actual_data_task_selected_document'
    binding = {'case_id': case_id, 'project_id': project, 'thread_id': turn['thread_id'], 'turn_id': turn['id']}
    if case['decision'] == 'read':
        current = await wait_turn(client, turn, project, meter,
            failure_path=output / (case_id + '-last-actual-turn.json'))
        receipt = current['receipt']['do']
        proof = receipt.get('lexicon_result', {})
        assert receipt['state'] == 'done' and receipt.get('document_id') is None
        assert proof.get('state') == 'read' and proof.get('verified') is True
        assert proof.get('revision') == before['revision'] and proof.get('terms') == before['terms']
        assert await read_words(client) == before and lexicon.read() == before
        assert 'lexicon_preview' not in receipt
        result = {**binding, 'state': 'done', 'words_unchanged': True,
            'readback_verified': True, 'native_events': native_proof(application, turn, 'read'), 'counts': meter.counts()}
        save(output / (case_id + '-completed-turn.json'), current)
        save(output / (case_id + '-result.json'), result)
        save(output / 'case-result.json', result)
        save(output / 'ready.json', {**binding, 'action': 'read', 'state': 'done',
            'url': 'http://127.0.0.1:4187/#view=workbench&project_id=' + project + '&thread_id=' + turn['thread_id'],
            'counts': meter.counts()})
        print(json.dumps({'state': 'ready', 'case_id': case_id, 'counts': meter.counts()}), flush=True)
        return await wait_ui(output, binding, result, seconds=ui_seconds)
    reviewed = await wait_turn(client, turn, project, meter, preview=True,
        failure_path=output / (case_id + '-last-actual-turn.json'))
    preview = reviewed['receipt']['do']['lexicon_preview']
    assert preview['state'] == 'pending' and changes_equal(preview['changes'], case['changes'])
    assert await read_words(client) == before, 'preview_changed_actual_vocabulary'
    save(output / (case_id + '-preview.json'), reviewed)
    if case.get('stale_import'):
        assert case['decision'] == 'cancel'
        updated = await client.post('/api/v2/asr/lexicon/import', json={
            'text': json.dumps(case['stale_import'], ensure_ascii=False), 'format': 'json',
            'expected_revision': before['revision'], 'preview': False})
        updated.raise_for_status()
        before = await read_words(client)
        stale_body = {'project_id': project, 'decision': 'approve', **{name: preview[name]
            for name in ('preview_id', 'preview_revision', 'event_id', 'sequence')}}
        rejected = await post_action(client, turn, stale_body, 'lexicon-stale-' + uuid4().hex)
        assert rejected.status_code == 409 and await read_words(client) == before
        reviewed = await read_turn(client, turn, project)
        assert reviewed['receipt']['do']['lexicon_preview']['stale'] is True
    preview_physical = meter.counts()['new_physical']
    action_offset = len(actions)
    save(output / 'ready.json', {'case_id': case_id, 'decision': case['decision'], 'action': case['decision'], 'project_id': project,
        'thread_id': turn['thread_id'], 'turn_id': turn['id'], 'state': 'waiting_approval',
        'url': 'http://127.0.0.1:4187/#view=workbench&project_id=' + project + '&thread_id=' + turn['thread_id'],
        'counts': meter.counts()})
    print(json.dumps({'state': 'ready', 'case_id': case_id, 'counts': meter.counts()}), flush=True)
    deadline, duplicated, concurrent = monotonic() + ui_seconds, False, None
    while monotonic() < deadline:
        if meter.halted:
            raise RuntimeError('acceptance_transport_stopped')
        if len(actions) > action_offset and not duplicated:
            actual = actions[action_offset]
            assert actual['body']['decision'] == case['decision']
            concurrent = await post_action(client, turn, actual['body'], actual['key'])
            assert concurrent.status_code in {200, 409}
            duplicated = True
        current = await read_turn(client, turn, project)
        state = current['receipt']['do']['state']
        if state in {'done', 'failed', 'partial', 'cancelled', 'interrupted', 'stopped'}:
            break
        await asyncio.sleep(1)
    else:
        raise TimeoutError('actual_ui_action_timeout')
    assert duplicated, 'no_actual_ui_action_observed'
    after = await read_words(client)
    assert after == lexicon.read(), 'settings_domain_and_http_readback_disagree'
    if case['decision'] == 'approve':
        assert state == 'done' and current['receipt']['do']['lexicon_result']['verified'] is True
        assert current['receipt']['do'].get('document_id') is None, 'actual_data_task_created_document'
        verify_words(before, after, case['changes'])
    else:
        assert state == 'cancelled' and after == before, 'cancel_changed_vocabulary'
        assert meter.counts()['new_physical'] == preview_physical, 'cancel_started_model'
    counts_before_replay = meter.counts()['new_physical']
    actual = actions[action_offset]
    replay = await post_action(client, turn, actual['body'], actual['key'])
    assert replay.status_code == 200 and await read_words(client) == after
    late = await post_action(client, turn, actual['body'], 'lexicon-late-' + uuid4().hex)
    assert late.status_code == 409
    wrong = await post_action(client, turn, {**actual['body'], 'project_id': 'default'}, 'lexicon-wrong-' + uuid4().hex)
    assert wrong.status_code == 404
    assert meter.counts()['new_physical'] == counts_before_replay, 'replay_or_rejection_started_model'
    result = {**binding, 'state': state, 'concurrent_status': concurrent.status_code,
        'replay_status': replay.status_code, 'late_status': late.status_code, 'wrong_project_status': wrong.status_code,
        'preview_preserved_words': True, 'settings_readback_matches': True,
        'native_events': native_proof(application, turn, case['decision']), 'counts': meter.counts()}
    save(output / (case_id + '-result.json'), result)
    save(output / (case_id + '-completed-turn.json'), current)
    save(output / (case_id + '-words-after.json'), after)
    save(output / 'case-result.json', result)
    # 实际界面读取完成后才进入下一例；旧 case 的完成信号不释放新 case。
    return await wait_ui(output, binding, result, seconds=ui_seconds)


async def execute(application, meter, args, output):
    actions = []
    @application.middleware('http')
    async def observe_action(request, call_next):
        if request.url.path.endswith('/lexicon-action'):
            row = {'case_id': meter.label, 'path': request.url.path, 'body': json.loads(await request.body()),
                'key': request.headers.get('Idempotency-Key'), 'started_at': datetime.now(timezone.utc).isoformat()}
            actions.append(row)
            save(output / 'actions.json', actions)
            try:
                response = await call_next(request)
            except Exception as error:
                row.update(completed_at=datetime.now(timezone.utc).isoformat(), error_type=type(error).__name__)
                save(output / 'actions.json', actions)
                raise
            row.update(completed_at=datetime.now(timezone.utc).isoformat(), status=response.status_code)
            save(output / 'actions.json', actions)
            return response
        return await call_next(request)
    server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=18126, log_level='warning', access_log=False))
    serving = asyncio.create_task(server.serve())
    try:
        deadline = monotonic() + 25
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError('actual_server_not_started')
            if monotonic() >= deadline:
                raise TimeoutError('actual_server_start_timeout')
            await asyncio.sleep(0.2)
        async with httpx.AsyncClient(base_url='http://127.0.0.1:18126', timeout=180) as client:
            starting_physical = meter.counts()['new_physical']
            before = await read_words(client)
            for decision in ([], {}):
                bad = await client.post('/api/v2/workbench/turns/missing/lexicon-action',
                    json={'project_id': 'default', 'preview_id': 'none', 'preview_revision': 1,
                        'event_id': 'none', 'sequence': 1, 'decision': decision})
                assert bad.status_code == 400 and await read_words(client) == before
            if args.mode == 'preflight':
                from backend.memory_app.v2.policies import get
                classification_statuses = []
                if (getattr(get('route'), 'classify_manual_do', False)
                        and not application.state.recognition_models.public()['generation']['enabled']):
                    records = application.state.recognition_records
                    counts_before = {name: len(records.list(name)) for name in ('v2_turns', 'workspace_items')}
                    for body in ({'intent': 'do', 'text': '整理这段内容'}, {'text': '请添加热词预检词语'}):
                        rejected = await client.post('/api/v2/workbench/turns', json={'project_id': 'default', **body},
                            headers={'Accept': 'application/json', 'Idempotency-Key': 'lexicon-disabled-' + uuid4().hex})
                        assert rejected.status_code == 409
                        classification_statuses.append(rejected.status_code)
                    assert {name: len(records.list(name)) for name in counts_before} == counts_before
                assert meter.counts()['new_physical'] == starting_physical
                report = {'state': 'preflight_passed', 'http_checks': 3 + len(classification_statuses),
                    'disabled_classification_statuses': classification_statuses, 'words_unchanged': True,
                    'physical_requests_delta': 0, 'counts': meter.counts()}
                save(output / 'preflight.json', report)
                print(json.dumps(report), flush=True)
                return
            identity = uuid4().hex[:8]
            cases = args.validated_cases if args.validated_cases is not None else validate_cases(defaults(identity))
            project_response = await client.post('/api/v2/projects', json={'name': '词表验收' + identity})
            project_response.raise_for_status()
            project = project_response.json()['id']
            results = []
            for case in cases:
                results.append(await run_case(application, client, meter, case, project, output, actions,
                    ui_seconds=args.ui_seconds))
            save(output / 'result.json', {'state': 'succeeded', 'cases': results, 'counts': meter.counts()})
            print(json.dumps({'state': 'succeeded', 'counts': meter.counts()}), flush=True)
    finally:
        server.should_exit = True
        await serving


def main():
    global save, DIAGNOSTIC_OUTPUT
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('preflight', 'run'), required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prior-ledger', type=Path, required=True)
    parser.add_argument('--prior-native-unknown', type=int, required=True)
    parser.add_argument('--model-factory')
    parser.add_argument('--case-file', type=Path)
    parser.add_argument('--policy', action='append', default=[])
    parser.add_argument('--ui-seconds', type=int, default=420)
    args = parser.parse_args()
    root, output = args.data_root.resolve(), args.output.resolve()
    if (root.parent.name != 'work' or not root.name.startswith('wlqa-')
            or not any(parent.name == 'work' for parent in output.parents)):
        raise ValueError('dedicated_work_acceptance_directories_required')
    output.mkdir(parents=True, exist_ok=True)
    DIAGNOSTIC_OUTPUT = output
    args.validated_cases = validate_cases(json.loads(args.case_file.read_text(encoding='utf-8'))) if args.case_file else None
    root.mkdir(parents=True, exist_ok=True)
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    configuration = root / 'config/settings.toml'
    if not configuration.exists():
        configuration.parent.mkdir(parents=True, exist_ok=True)
        configuration.write_bytes((REPOSITORY / 'config/settings.toml.example').read_bytes())
    from tools.acceptance_transport import AcceptanceTransport, save
    meter = AcceptanceTransport(root, args.prior_ledger, prior_native_unknown=args.prior_native_unknown)
    with meter.observe_http(paid=args.mode == 'run'):
        models = None
        if args.mode == 'run':
            if not args.model_factory:
                raise ValueError('private_real_model_factory_required')
            models = load_model_factory(args.model_factory)(root, meter.complete)
            if Path(models.root).resolve() != root or Path(models.records.database_path).resolve() != root / '.rebuild-data/structured-records.sqlite3':
                raise ValueError('real_model_configuration_must_belong_to_isolated_root')
        from backend.memory_app.app import create_app
        from backend.memory_app.v2.policies import override, parse_overrides
        application = create_app(runtime_root=root, model_configuration=models)
        if models is not None and application.state.recognition_models is not models:
            raise ValueError('real_model_configuration_not_injected')
        if Path(application.state.recognition_records.database_path).resolve() != root / '.rebuild-data/structured-records.sqlite3':
            raise ValueError('actual_application_database_not_isolated')
        try:
            with override(**{'retry': '@3', **parse_overrides(args.policy)}):
                asyncio.run(execute(application, meter, args, output))
        except BaseException as error:
            save(output / 'failure.json', {'state': 'failed', **failure_details(error), 'counts': meter.counts()})
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # 仅输出固定错误码和仓库内位置，输入正文及认证参数不进入诊断。
        report = {'state': 'failed', **failure_details(error)}
        if DIAGNOSTIC_OUTPUT is not None:
            target = DIAGNOSTIC_OUTPUT / 'failure.json'
            if target.exists():
                report = {**json.loads(target.read_text(encoding='utf-8')), **report}
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
        raise SystemExit(1) from None
