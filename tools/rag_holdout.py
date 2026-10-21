from __future__ import annotations

import argparse
from contextlib import closing
from contextvars import ContextVar
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from threading import RLock
import time
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from tools import rag_dataset, rag_pilot as pilot
from tools.demo_smoke import AttemptMeter

CALL = ContextVar('heldout_completion', default={})


class PhysicalLimit(RuntimeError):
    pass


class HttpObserver:
    def __init__(self, output, target, limit, *, paid):
        self.path, self.target, self.limit = output / 'http-requests.json', urlsplit(target), limit
        self.rows, self.lock, self.blocked = pilot.read(self.path, []), RLock(), 0
        self.paid = paid

    def request(self, request):
        if request.url.host in {'testserver', '127.0.0.1', 'localhost', '::1'}:
            return
        if (request.url.host != self.target.hostname or request.url.port != self.target.port
                or request.url.scheme != self.target.scheme):
            raise RuntimeError('heldout_unexpected_remote_egress')
        if not self.paid:
            raise RuntimeError('heldout_prepare_only_remote_blocked')
        with self.lock:
            if len(self.rows) >= self.limit:
                self.blocked += 1
                raise PhysicalLimit('heldout_physical_limit')
            row = {'number': len(self.rows) + 1, 'completion': dict(CALL.get()),
                   'method': request.method, 'state': 'request_started', 'started': time.time()}
            self.rows.append(row)
            request.extensions['heldout_http_number'] = row['number']
            pilot.save(self.path, self.rows)

    def response(self, response):
        number = response.request.extensions.get('heldout_http_number')
        if number is not None:
            with self.lock:
                self.rows[number - 1].update(state='headers_received', http_status=response.status_code,
                    headers_at=time.time())
                pilot.save(self.path, self.rows)

    def __enter__(self):
        self.sync, self.asynchronous = httpx.Client.__init__, httpx.AsyncClient.__init__
        observer = self

        def sync(client, *args, **kwargs):
            observer.sync(client, *args, **kwargs)
            client.event_hooks['request'].append(observer.request)
            client.event_hooks['response'].append(observer.response)

        async def request(value):
            observer.request(value)

        async def response(value):
            observer.response(value)

        def asynchronous(client, *args, **kwargs):
            observer.asynchronous(client, *args, **kwargs)
            client.event_hooks['request'].append(request)
            client.event_hooks['response'].append(response)

        # 只追加真实客户端观察钩子，保留连接池、TLS、流与生产重试参数。
        httpx.Client.__init__, httpx.AsyncClient.__init__ = sync, asynchronous
        return self

    def __exit__(self, *_):
        httpx.Client.__init__, httpx.AsyncClient.__init__ = self.sync, self.asynchronous


class CompletionObserver(pilot.Budget):
    def complete(self, **request):
        from backend.memory_app.kernel.answer_turns import ACTIVE_ANSWER
        from backend.memory_app.model_config import _load_litellm_completion
        active = ACTIVE_ANSWER.get()
        with self.lock:
            row = {'number': len(self.rows) + 1, 'label': self.label, 'record_type': 'completion_invocation',
                   'turn_id': active[0]['turn_id'] if active is not None else None, 'state': 'started'}
            self.rows.append(row)
            pilot.save(self.output / 'wires.json', self.rows)
            pilot.save(self.output / f"messages-{row['number']}.json", request.get('messages', []))
        token = CALL.set({key: row[key] for key in ('number', 'label', 'turn_id')})
        started = time.monotonic()
        try:
            # 不添加旧试点的词元、期限或重试覆盖。
            result = _load_litellm_completion(**request)
            row['state'] = 'transport_returned'
            return result
        except BaseException as error:
            row.update(state='failed_before_return', error_type=type(error).__name__)
            raise
        finally:
            CALL.reset(token)
            row['return_seconds'] = round(time.monotonic() - started, 6)
            with self.lock:
                pilot.save(self.output / 'wires.json', self.rows)


def read_rows(path, sql, values=()):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, values)]


def configuration(root, database):
    from backend.security.secrets import build_model_secret_store
    rows = read_rows(database, 'SELECT object_id,payload_json FROM crp_structured_records WHERE collection=?',
                     ('recognition_model_config',))
    secrets, values = build_model_secret_store(root), {}
    for row in rows:
        payload = json.loads(row['payload_json'])
        key = secrets.get_snapshot(payload.get('secret_ref', '')).value if payload.get('secret_ref') else ''
        values[row['object_id']] = {name: payload[name] for name in
            ('provider', 'base_url', 'model', 'enabled', 'allow_remote') if name in payload} | {'api_key': key}
    primary = values.get('generation', {})
    if (urlsplit(primary.get('base_url', '')).hostname != 'api.deepseek.com'
            or 'deepseek' not in primary.get('model', '').lower() or not primary.get('api_key')
            or not primary.get('enabled') or not primary.get('allow_remote')):
        raise ValueError('heldout_authorized_deepseek_required')
    extra = read_rows(database, 'SELECT collection,payload_json FROM crp_structured_records WHERE collection IN (?,?)',
        ('recognition_generation_mode', 'v2_generation_fast_model'))
    mode, fast = 'api', None
    for row in extra:
        payload = json.loads(row['payload_json'])
        if row['collection'] == 'recognition_generation_mode':
            mode = payload['mode']
        else:
            fast = payload.get('model')
    if mode != 'api' or fast is not None and 'deepseek' not in fast.lower():
        raise ValueError('heldout_deepseek_api_binding_required')
    return values, fast


def build_application(data, configs, fast, assets, completion):
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.security.secrets import InMemorySecretStore
    from backend.shared.deployment import DeploymentLayout
    os.environ['CHRIPTMAS_APP_ROOT'] = str(data)
    config = data / 'config/settings.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        shutil.copyfile(ROOT / 'config/settings.toml.example', config)
    records, _ = resolve_recognition_document_store(data)
    secrets = InMemorySecretStore()
    models = ModelConfiguration(records, data, secrets, completion_fn=completion.complete, local_models_root=assets)
    for purpose, value in configs.items():
        old = records.read('recognition_model_config', purpose)
        if old is None:
            models.update(purpose, {**value, 'expected_revision': 0})
        else:
            if any(old.payload.get(name) != value.get(name) for name in ('provider', 'base_url', 'model', 'enabled', 'allow_remote')):
                raise ValueError('heldout_model_configuration_changed')
            if old.payload.get('secret_ref'):
                secrets.set(old.payload['secret_ref'], value['api_key'])
    if fast is not None and not models.fast_model()['configured']:
        models.update_fast_model(model=fast, expected_revision=models.fast_model()['revision'],
            expected_generation_revision=models.public()['generation']['revision'],
            expected_mode_revision=models.generation_mode()['revision'])
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=data, model_configuration=models, deployment_layout=DeploymentLayout('desktop', data))
    owner = app.state.memory_embedding_settings
    selected = owner.mode(models.public()['embedding'])
    if selected['mode'] != 'local' or selected['revision'] == 0:
        owner.update_mode(mode='local', expected_revision=selected['revision'])
    return app


def attempt_payloads(database, attempts):
    for attempt in attempts:
        for name in ('dispatch_payload_ref', 'terminal_receipt_ref'):
            ref = attempt.get(name)
            payload = read_rows(database, 'SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=?', (ref,)) if ref else []
            value = json.loads(payload[0]['payload_json']) if payload else None
            if name == 'dispatch_payload_ref' and value is not None:
                value = {key: value[key] for key in ('provider_id', 'model_id', 'model_request_id') if key in value}
            attempt[name.removesuffix('_ref')] = value
    return attempts


def export_native(app, data, identity):
    database = data / '.rebuild-data/ai-turns.sqlite3'
    attempts = attempt_payloads(database, read_rows(database,
        'SELECT * FROM ai_model_attempt_reservations WHERE turn_id=? ORDER BY dispatched_at', (identity,)))
    frozen = read_rows(database, 'SELECT payload_ref,kind,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? ORDER BY kind', (identity,))
    for row in frozen:
        row['payload'] = json.loads(row.pop('payload_json'))
    return {'turn': read_rows(database, 'SELECT * FROM ai_turns WHERE turn_id=?', (identity,)),
        'events': read_rows(database, 'SELECT * FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (identity,)),
        'attempts': attempts, 'immutable': frozen,
        'timings': read_rows(data / '.rebuild-data/structured-records.sqlite3',
            "SELECT * FROM crp_structured_records WHERE collection='v2_turn_timings' AND object_id=?", (identity,))}


def seed_sources(app, client, data, output, sessions, sample):
    from backend.memory_app.original_sources import resolve_turn_material
    from backend.memory_app.source_egress import SourceEgressService
    from backend.recognition import WorkScope
    saved = pilot.read(output / 'project.json')
    if saved is None:
        response = client.post('/api/v2/projects', json={'name': 'LoCoMo ' + sample})
        response.raise_for_status()
        saved = response.json()
        pilot.save(output / 'project.json', saved)
    project = saved['id']
    query = app.state.workspace_domains.query
    store = query.source_store
    authority, proofs = SourceEgressService(query.records), []
    for session in sessions:
        value = {'id': session['id'], 'project_id': project, 'title': session['title'], 'metadata': {'content': session['content']}}
        old = store.read('sources', session['id'])
        if old is None:
            store.write('sources', session['id'], value, expected_revision=0)
        elif any(old.get(key) != value[key] for key in ('project_id', 'title', 'metadata')):
            raise ValueError('heldout_existing_source_changed')
        if store.read('sources', session['id']).get('metadata', {}).get('content') != session['content']:
            raise ValueError('heldout_admitted_session_text_unavailable')
        scope = WorkScope('local-user', project)
        _, roots = resolve_turn_material(query.records, scope, {'type': 'original_source', 'id': session['id'],
            'revision': store.revision('sources', session['id']), 'project_id': project})
        snapshot = authority.snapshot(scope, roots)
        authority.validate_snapshot(scope, snapshot)
        authority.require(snapshot, 'generation')
        proofs.append({'source_id': session['id'], 'snapshot': snapshot})
    rag_dataset.freeze_json(output / 'source-proofs.json', proofs)
    query.retrieval_index.bootstrap()
    return project


def ensure_vectors(directory, output):
    from backend.memory_app import local_vectors
    from backend.memory_app.v2.embedding_settings import vector_policy
    started = time.monotonic()
    if not local_vectors.loaded(directory):
        local_vectors.load(directory, policy=vector_policy())
    while local_vectors.runtime_status(directory)['runtime_status'] == 'loading':
        if time.monotonic() - started > 300:
            raise TimeoutError('heldout_vector_load_timeout')
        time.sleep(1)
    state = local_vectors.runtime_status(directory)
    pilot.save(output / 'vector-load.json', {'seconds': time.monotonic() - started, 'state': state})
    if not local_vectors.loaded(directory):
        raise RuntimeError('heldout_vector_load_failed')


def audit_attempts(output, data_root):
    attempts, local, covered, baselines = {}, {}, set(), {}
    for directory in output.iterdir():
        baseline = pilot.read(directory / 'attempt-baseline.json') if directory.is_dir() else None
        if baseline is None:
            continue
        baselines[directory.name] = baseline
        database = data_root / directory.name / '.rebuild-data/ai-turns.sqlite3'
        local_rows = []
        for row in read_rows(database, "SELECT r.*,json_extract(p.payload_json,'$.provider_id') AS provider_id,"
                "json_extract(p.payload_json,'$.model_id') AS model_id FROM ai_model_attempt_reservations r "
                'JOIN ai_turn_payloads p ON p.payload_ref=r.dispatch_payload_ref'):
            if row['attempt_id'] not in baseline['ids']:
                if row['attempt_id'] in attempts or row['attempt_id'] in local:
                    raise ValueError('heldout_duplicate_attempt_id')
                if row['provider_id'] == 'local':
                    local[row['attempt_id']] = row
                    local_rows.append(row)
                else:
                    if 'deepseek' not in str(row['model_id']).lower():
                        raise ValueError('heldout_unexpected_native_remote_model')
                    attempts[row['attempt_id']] = row
        pilot.save(directory / 'local-native.json', attempt_payloads(database, local_rows))
        for path in directory.glob('question-*.json'):
            result = pilot.read(path)
            native = [result['native']] if 'native' in result else list(result.get('native_failures', {}).values())
            covered.update(row['attempt_id'] for value in native for row in value['attempts'])
    report = {'baselines': baselines, 'new_attempts': list(attempts.values()), 'new_unique_count': len(attempts),
        'local_unique_count': len(local), 'local_pending': sum(row['status'] != 'terminal' for row in local.values()),
        'terminal': sum(row['status'] == 'terminal' for row in attempts.values()),
        'pending': sum(row['status'] != 'terminal' for row in attempts.values()),
        'uncovered_attempt_ids': sorted(set(attempts) - covered)}
    pilot.save(output / 'attempt-audit.json', report)
    return report


def check_meter(meter, *, submitting=False):
    # 本机向量也有真实内核尝试，付费帽只计远端 DeepSeek，原始本机回执另存。
    rows = {identity: row for identity, row in meter.read().items() if identity not in meter.baseline and row[1] != 'local'}
    if any(not isinstance(row[2], str) or 'deepseek' not in row[2].lower() for row in rows.values()):
        raise ValueError('heldout_unexpected_native_remote_model')
    if len(rows) > meter.limit or submitting and len(rows) >= meter.limit:
        raise PhysicalLimit('heldout_native_limit')


def run(args):
    from backend.memory_app import local_vectors
    from backend.memory_app.v2.embedding_settings import vector_policy
    from backend.memory_app.v2.policies.pipelines import versions_for_turn
    from backend.memory_app.v2.turn_execution import _REQUEST_KEY
    from fastapi.testclient import TestClient
    output = args.output.resolve()
    data_root, formal = args.data_root.resolve(), args.config_root.resolve()
    index_path = args.index.resolve()
    index = rag_dataset.read_json(index_path)
    selected = set(args.only_question)
    available = {entry['sample_id'] + ':' + str(identity)
                 for entry in index['conversations'] for identity in entry['qa_indices']}
    if len(selected) != len(args.only_question) or not selected <= available:
        raise ValueError('heldout_question_selection_invalid')
    selected_samples = {value.partition(':')[0] for value in selected}
    if any(not _REQUEST_KEY.fullmatch(args.run_label + '-' + entry['sample_id'] + '-' + str(identity))
           for entry in index['conversations'] for identity in entry['qa_indices']):
        raise ValueError('heldout_run_label_invalid')
    for target in (output, data_root):
        for protected in (formal, index_path.parent, Path(index['raw_data']).resolve().parent):
            if target == protected or target in protected.parents or protected in target.parents:
                raise ValueError('heldout_output_overlaps_authoritative_input')
    output.mkdir(parents=True, exist_ok=True)
    rag_dataset.prepare_dataset(output / 'dataset', Path(index['raw_data']), Path(index['development_manifest']),
        Path(index['license_path']), seed=index['seed'], count=len(index['conversations']), frozen_index=args.index.resolve())
    policies = versions_for_turn('project.answer')
    if any(policies[name] != version for name, version in {'compose': args.expected_compose, 'ladder': '@15', 'retrieve': args.expected_retrieve, 'rerank': '@2'}.items()):
        raise ValueError('heldout_active_policy_changed')
    if args.source_archive is not None:
        with tarfile.open(args.source_archive, 'r:gz') as archive:
            members = archive.getmembers()
            revision = archive.pax_headers.get('comment', '')
            if len(revision) != 40 or any(character not in '0123456789abcdef' for character in revision):
                raise ValueError('heldout_git_archive_revision_missing')
            for member in members:
                if member.isfile():
                    with archive.extractfile(member) as source:
                        if (ROOT / member.name).read_bytes() != source.read():
                            raise ValueError('heldout_git_archive_source_changed:' + member.name)
        code = {'revision': revision, 'dirty': False, 'source': 'verified_git_archive'}
    else:
        code = {'revision': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
            'dirty': bool(subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=no'], text=True).strip())}
    if not args.prepare_only and code['dirty']:
        raise ValueError('heldout_committed_source_required')
    configs, fast = configuration(args.config_root.resolve(), args.config_db.resolve())
    completion = CompletionObserver(output, args.max_calls)
    http = HttpObserver(output, configs['generation']['base_url'], args.max_calls, paid=not args.prepare_only)
    directory = local_vectors.model_directory(args.models_root.resolve())
    planned, attempted = 0, []
    try:
        ensure_vectors(directory, output)
        with http:
            for entry in index['conversations']:
                if planned >= args.questions:
                    break
                sample = entry['sample_id']
                if selected and sample not in selected_samples:
                    continue
                current = output / sample
                data = args.data_root.resolve() / sample
                current.mkdir(parents=True, exist_ok=True)
                manifest, sessions = pilot.prepare_data(current, index['seed'], raw_data=Path(index['raw_data']),
                    manifest_path=args.index.parent / entry['manifest'])
                questions = pilot.prepared_questions(current, manifest, sessions, Path(index['raw_data']), explicit=True)
                if selected:
                    questions = [item for item in questions if sample + ':' + str(item['qa_index']) in selected]
                app = build_application(data, configs, fast, args.models_root.resolve(), completion)
                with TestClient(app, raise_server_exceptions=False) as client:
                    query = app.state.workspace_domains.query
                    query.answer_turns._runtime()
                    meter = AttemptMeter(data, args.max_calls)
                    baseline = current / 'attempt-baseline.json'
                    if not baseline.exists():
                        rag_dataset.freeze_json(baseline, {'ids': sorted(meter.baseline), 'pending': meter.baseline_pending})
                    meter.baseline = set(pilot.read(baseline)['ids'])
                    project = seed_sources(app, client, data, current, sessions, sample)
                    ensure_vectors(directory, current)
                    index_owner = app.state.memory_embedding_index
                    computed = index_owner.run_scoped()
                    while index_owner.lock.locked():
                        time.sleep(1)
                    verified = pilot.verify_index(query)
                    pilot.save(current / 'index.json', {'computed': computed, 'verification': verified})
                    if not verified['total'] or verified['missing'] or verified['oversized']:
                        raise RuntimeError('heldout_index_incomplete')
                    public = app.state.recognition_models.public()
                    effective = {purpose: {key: value[key] for key in
                        ('provider', 'model', 'configured', 'enabled', 'allow_remote', 'mode', 'revision') if key in value}
                        for purpose, value in public.items() if isinstance(value, dict)}
                    effective['fast_model'] = app.state.recognition_models.fast_model()
                    rag_dataset.freeze_json(current / 'public-settings.json', effective)
                    run_config = {'contract': 'heldout-workbench@1', 'source': code, 'policies': policies,
                        'run_label': args.run_label,
                        'index': index, 'questions_planned': len(selected) if selected else 48, 'effective_models': effective,
                        'model_assets': str(args.models_root.resolve()), 'data_root': str(data_root), 'project_id': project,
                        'gold_in_generation': False, 'automatic_judge': False}
                    if selected:
                        run_config['selected_questions'] = sorted(selected)
                    if not args.prepare_only:
                        rag_dataset.freeze_json(current / 'run-config.json', run_config)
                    else:
                        pilot.save(current / 'preparation-config.json', run_config)
                    if args.prepare_only:
                        planned += min(len(questions), args.questions - planned)
                        continue
                    for item in questions:
                        if planned >= args.questions:
                            break
                        planned += 1
                        path = current / f"question-{item['qa_index']}.json"
                        nonce = args.run_label + '-' + sample + '-' + str(item['qa_index'])
                        binding = {'conversation_id': sample, 'qa_index': item['qa_index'], 'project_id': project,
                                   'run_label': args.run_label,
                                   'text': item['question'], 'nonce': nonce, 'source': code}
                        submission_path = current / f"submission-{item['qa_index']}.json"
                        submission = pilot.read(submission_path)
                        if submission is not None and any(submission[key] != value for key, value in binding.items()):
                            raise ValueError('heldout_submission_binding_changed')
                        old = pilot.read(path)
                        if old is not None:
                            if submission is None or any(old[key] != binding[key] for key in ('conversation_id', 'qa_index', 'source')):
                                raise ValueError('heldout_saved_question_identity_changed')
                            attempted.append(old)
                            continue
                        if submission is not None:
                            raise RuntimeError(f'heldout_pending_submission_requires_native_recovery:{submission_path}')
                        completion.label = sample + ':' + str(item['qa_index'])
                        before, started = len(http.rows), time.monotonic()
                        check_meter(meter, submitting=True)
                        database = data / '.rebuild-data/ai-turns.sqlite3'
                        before_turns = {row['turn_id'] for row in read_rows(database, 'SELECT turn_id FROM ai_turns')}
                        submission = {**binding, 'started_at': time.time(), 'before_http': before,
                                      'before_turns': sorted(before_turns)}
                        rag_dataset.freeze_json(submission_path, submission)
                        response = client.post('/api/v2/workbench/turns', json={'project_id': project,
                            'text': item['question'], 'intent': 'ask'},
                            headers={'Idempotency-Key': nonce})
                        body = response.json() if response.headers.get('content-type', '').startswith('application/json') else None
                        turn = body.get('turn', {}) if isinstance(body, dict) else {}
                        identity = turn.get('id')
                        row = {'conversation_id': sample, 'qa_index': item['qa_index'], 'http_status': response.status_code,
                            'seconds': time.monotonic() - started,
                            'physical_http': len(http.rows) - submission['before_http'],
                            'body': body, 'turn_id': identity, 'source': code}
                        if identity is not None:
                            row['native'] = export_native(app, data, identity)
                        else:
                            observed = {call['turn_id'] for call in completion.rows if call.get('label') == completion.label
                                        and call.get('turn_id')}
                            observed.update(record['turn_id'] for record in read_rows(database, 'SELECT turn_id FROM ai_turns')
                                            if record['turn_id'] not in submission['before_turns'])
                            row['native_failures'] = {turn: export_native(app, data, turn) for turn in sorted(observed)}
                        pilot.save(path, row)
                        attempted.append(row)
                        pilot.save(output / 'progress.json', {'questions_recorded': len(attempted), 'physical_http': len(http.rows),
                            'completion_invocations': len(completion.rows), 'last': completion.label})
                        print(json.dumps({'question': completion.label, 'status': response.status_code,
                            'physical_http': len(http.rows), 'artifact': str(path)}), flush=True)
                        if http.blocked:
                            raise PhysicalLimit('heldout_physical_limit')
                        if isinstance(body, dict) and body.get('detail') in {
                                'source_changed_retry', 'private_project_remote_blocked', 'ask_model_target_changed',
                                'remote_disabled', 'model_not_configured', 'generation_not_configured'}:
                            raise RuntimeError('heldout_configuration_or_material_precondition_failed')
                        check_meter(meter)
                app.state.recognition_models.close()
    finally:
        local_vectors.unload(directory, wait=True)
        audit = audit_attempts(output, data_root)
        pilot.save(output / 'closed.json', {'vector_unloaded': not local_vectors.loaded(directory),
            'physical_http': len(http.rows), 'completion_invocations': len(completion.rows),
            'native_new_unique': audit['new_unique_count'], 'native_pending': audit['pending'],
            'local_native_unique': audit['local_unique_count'], 'local_native_pending': audit['local_pending'],
            'uncovered_attempt_ids': audit['uncovered_attempt_ids'], 'prepare_only': args.prepare_only})
    if audit['pending'] or audit['local_pending'] or audit['uncovered_attempt_ids']:
        raise RuntimeError('heldout_native_attempt_audit_incomplete')


def main():
    parser = argparse.ArgumentParser(description='Run frozen independent questions through the stock workbench ask workflow.')
    for name in ('index', 'output', 'data-root', 'config-root', 'config-db', 'models-root'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--questions', type=int, default=48, choices=range(1, 49))
    parser.add_argument('--max-calls', type=int, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--run-label', default='heldout-v1', help='冻结本批提交身份，复验使用新标签以保留旧回合')
    parser.add_argument('--expected-retrieve', default='@7', choices=('@7', '@8'), help='核对实际生产版本，不选择运行策略')
    parser.add_argument('--expected-compose', default='@30', choices=('@30', '@33'), help='核对实际生产版本，不选择运行策略')
    parser.add_argument('--only-question', action='append', default=[], help='仅执行冻结 index 中明确选择的 sample_id:qa_index，可重复指定')
    parser.add_argument('--source-archive', type=Path, help='核对 Git 原生归档内的提交和全部普通文件，用于没有 .git 的部署目录')
    args = parser.parse_args()
    if args.max_calls < 1:
        parser.error('--max-calls must be positive')
    os.environ.update(LITELLM_LOCAL_MODEL_COST_MAP='True', LITELLM_LOG='ERROR', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    run(args)


if __name__ == '__main__':
    main()
