"""Repeatable offline product benchmark; only synthetic data in temporary roots."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import ipaddress
import math
import os
from pathlib import Path
import platform
import socket
import shutil
import subprocess
import sys
from tempfile import mkdtemp, gettempdir
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
PROJECT = 'perf-synthetic'
COLLECTION = 'v2_turn_timings'


@contextmanager
def offline():
    """Fail closed at the socket boundary even if a provider is misconfigured."""
    original = socket.socket.connect
    def denied(connection, address):
        # Windows asyncio builds its self-pipe using loopback sockets.
        # The only model adapter in this benchmark is FixedCompletion.
        if isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return original(connection, address)
            except ValueError:
                pass
        raise RuntimeError('perf_bench forbids network connections')
    socket.socket.connect = denied
    try:
        yield
    finally:
        socket.socket.connect = original


class FixedCompletion:
    """Provider adapter; all production gateway/guard/schema code still executes."""
    def __init__(self, *, sleep=time.sleep):
        self.sleep = sleep
        self.calls = 0
        self.wait_ms = 0.0

    def wait(self, seconds):
        started = time.perf_counter()
        self.sleep(seconds)
        self.wait_ms += (time.perf_counter() - started) * 1000

    def __call__(self, **request):
        self.calls += 1
        instruction = '\n'.join(str(m.get('content', '')) for m in request['messages'] if m['role'] == 'system')
        if '"queries"' in instruction:
            value = {'queries': []}
        elif 'insights' in instruction:
            value = {'insights': [{'text': '合成项目每周核对进度。', 'conditions': []}]}
        elif 'summary' in instruction and 'facts' in instruction:
            value = dict(title='合成记住资料', summary='合成项目每周核对进度。',
                         facts=[], topics=[], todos=[], uncertainties=[], people=[], dates=[], suggestions=[])
        else:
            value = {'answer': '合成资料显示项目每周核对进度。', 'citations': [1]}
        raw = json.dumps(value, ensure_ascii=False)
        if not request.get('stream'):
            self.wait(1.0)
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 30}}
        def stream():
            self.wait(0.3)
            # The first chunk contains answer text, not just a JSON prefix.
            yield {'choices': [{'delta': {'content': raw[:-1]}, 'finish_reason': None}]}
            self.wait(0.7)
            yield {'choices': [{'delta': {'content': raw[-1:]}, 'finish_reason': None}]}
            yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 100, 'completion_tokens': 30}}
        return stream()


def make_application(root):
    """Use the shipping app composition, including legacy routes and lifespans."""
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    config = root / 'config/settings.toml'
    config.parent.mkdir(parents=True, exist_ok=True)
    if not config.exists():
        config.write_bytes((ROOT / 'config/settings.toml.example').read_bytes())
    from backend.memory_app.app import create_app
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    from backend.memory_app.model_config import ModelConfiguration
    from backend.security.secrets import InMemorySecretStore
    records, _ = resolve_recognition_document_store(root)
    provider = FixedCompletion()
    models = ModelConfiguration(records, root, InMemorySecretStore(), completion_fn=provider)
    existing = models.public()['generation']['revision']
    models.update('generation', {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'api_key': 'synthetic-benchmark-only', 'allow_remote': True, 'expected_revision': existing})
    application = create_app(runtime_root=root, model_configuration=models)
    return application, records, application.state.recognition_documents, provider


def seed_corpus(root, records, documents, count):
    from core.storage_provider import JsonObjectStore
    from core.document_engine.ports import DocumentDraft
    sources = JsonObjectStore(root / '.rebuild-data')
    for index in range(count):
        key = f'perf-source-{index:05d}'
        title = f'资料{index:05d}'
        body = f'合成项目 资料{index:05d} 每周核对进度。交付期限为周五，负责人为合成成员{index % 20:02d}。'
        sources.write('sources', key, {'id': key, 'title': title, 'project_id': PROJECT,
            'metadata': {'content': body}, 'created_at': '2026-10-03T00:00:00Z'}, expected_revision=0)
        markdown = f'# {title}\n\n## 摘要\n{body}\n\n## 正文\n{body}\n'
        doc = documents.create(DocumentDraft(title=title, document_type='legacy-material',
            markdown=markdown, source_refs=({'source_id': key, 'locator': 'text:0'},), project_id=PROJECT))
        with records.begin() as tx:
            identity = 'review-' + key
            tx.put('workspace_review_intents', identity, {'id': identity, 'source_id': key,
                'document_id': doc['id'], 'project_id': PROJECT, 'state': 'confirmed',
                'document_revision': doc['revision'], 'expected_document_id': None,
                'expected_document_revision': None, 'confirmed_markdown': markdown}, expected_revision=0)
            tx.commit()
        if (index + 1) % 100 == 0:
            print(f'seed {index + 1}/{count}', flush=True)


def distribution(values):
    values = sorted(float(v) for v in values)
    if not values:
        raise ValueError('no performance samples')
    def percentile(q):
        index = (len(values) - 1) * q
        low = math.floor(index)
        return round(values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (index - low), 3)
    return {'p50': percentile(.5), 'p95': percentile(.95), 'min': round(values[0], 3), 'max': round(values[-1], 3)}


def optional_distribution(values):
    available = [value for value in values if value is not None]
    return distribution(available) if available else None


def summarize(rows):
    stages = sorted({key for row in rows for key in row['stages_ms']})
    observed = {key: [r['stages_ms'][key] for r in rows
        if r.get('stage_observations', {}).get(key, int(r['stages_ms'].get(key, 0) > 0)) > 0] for key in stages}
    result = {'samples': len(rows),
        'successes': sum(r.get('status', 'success') == 'success' for r in rows),
        'failures': sum(r.get('status', 'success') != 'success' for r in rows),
        'total_ms': distribution([r['total_ms'] for r in rows]),
        'http_wall_ms': optional_distribution([r.get('http_wall_ms') for r in rows]),
        'local_ms': optional_distribution([max(0, r['total_ms'] - r.get('provider_wait_ms', 0))
            if r.get('provider_wait_ms', 0) is not None else None for r in rows]),
        'provider_wait_ms': optional_distribution([r.get('provider_wait_ms', 0) for r in rows]),
        'connection_count': distribution([r['connection_count'] for r in rows]),
        'statement_count': distribution([r['statement_count'] for r in rows]),
        'stages_ms': {key: optional_distribution(values) for key, values in observed.items()},
        'stage_observed_samples': {key: len(values) for key, values in observed.items()}}
    result['failure_rate'] = result['failures'] / len(rows)
    result['errors'] = {code: sum(r.get('error_code') == code for r in rows)
                        for code in sorted({r['error_code'] for r in rows if r.get('error_code')})}
    measured = [key for key in stages if result['stages_ms'][key] is not None]
    result['slowest_three_stages'] = sorted(measured, key=lambda key: result['stages_ms'][key]['p50'], reverse=True)[:3]
    result['stage_observations'] = {key: sum(r.get('stage_observations', {}).get(key, 0) for r in rows) for key in stages}
    result['slowest_three_local_stages'] = sorted((key for key in measured if key not in {'first_token', 'generation'}),
        key=lambda key: result['stages_ms'][key]['p50'], reverse=True)[:3]
    return result


def sse_outcome(body, http_status):
    terminal = []
    for frame in body.replace('\r\n', '\n').replace('\r', '\n').split('\n\n'):
        event, data = None, []
        for line in frame.splitlines():
            if line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:'):
                data.append(line[5:].lstrip())
        if event in {'done', 'error'}:
            terminal.append((event, json.loads('\n'.join(data))))
    if len(terminal) != 1:
        return {'status': 'error', 'error_code': 'missing_or_multiple_sse_terminal', 'http_status': http_status}
    event, value = terminal[0]
    if event == 'error':
        return {'status': 'error', 'error_code': value.get('code', 'unknown_sse_error'), 'http_status': http_status}
    no_match = value['turn']['receipt']['ask'].get('no_match', False)
    return {'status': 'error' if no_match else 'success',
            'error_code': 'no_match' if no_match else None, 'http_status': http_status,
            'response_turn_id': value['turn']['id']}


def timing_after(records, previous, *, operation, poll=time.sleep):
    # Keep observing the same operation after HTTP error; no new request or
    # arbitrary timeout may replace an in-flight product execution.
    next_report = time.monotonic() + 60
    while True:
        rows = [r.payload for r in records.list(COLLECTION)
                if r.object_id not in previous and r.payload['operation'] == operation]
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise RuntimeError('ambiguous benchmark timing observation')
        if time.monotonic() >= next_report:
            print(f'waiting for original {operation} background timing', flush=True)
            next_report = time.monotonic() + 60
        poll(1)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def complete_scale(scale, *, smoke=False):
    expected = {'ask': 1, 'library': 1, 'remember': 1} if smoke else {'ask':20, 'library':20, 'remember':5}
    return (all(len(scale.get('raw_samples', {}).get(key, [])) == count for key, count in expected.items())
            and len(scale.get('cold_starts', [])) == (1 if smoke else 3))


def resume_result(path, fresh, *, resume):
    if not resume or not path.exists():
        return fresh
    existing = json.loads(path.read_text(encoding='utf-8'))
    if existing.get('smoke', False) != fresh.get('smoke', False):
        raise ValueError('cannot mix smoke and acceptance samples')
    return existing


def cold_start(root):
    """Separate interpreter: timer starts before product imports and ends at lifespan ready."""
    started = time.perf_counter()
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    from backend.memory_app.app import app
    from fastapi.testclient import TestClient
    with TestClient(app):
        ready_ms = (time.perf_counter() - started) * 1000
    return {'ready_ms': ready_ms, 'pid': os.getpid()}


def measure_scale(count, *, root=None, ask_count=20, library_count=20, remember_count=5, cold_count=3):
    from fastapi.testclient import TestClient
    root = Path(root or mkdtemp(prefix=f'chriptmas-perf-{count}-'))
    checkpoint = root / 'measurement-checkpoint.json'
    state = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {
        'documents':count, 'source_revision':repository_revision(), 'command':sys.argv,
        'seeded':False, 'cold_starts':[], 'raw_samples':{'ask':[], 'library':[], 'remember':[]}, 'provider_calls':0}
    if state['documents'] != count:
        raise ValueError('checkpoint corpus size mismatch')
    if state.get('pending'):
        raise RuntimeError('interrupted request checkpoint retained for diagnosis; do not replay its idempotency key')
    app, records, documents, provider = make_application(root)
    if not state['seeded']:
        if documents.list():
            raise RuntimeError('incomplete corpus seed; preserve this root and use a fresh synthetic root')
        seed_corpus(root, records, documents, count)
        state['seeded'] = True
        save_json(checkpoint, state)
    print(f'{count}: corpus ready', flush=True)
    cold, samples = state['cold_starts'], state['raw_samples']
    state.setdefault('runs', []).append({'revision':repository_revision(), 'command':sys.argv})
    save_json(checkpoint, state)
    for index in range(len(cold), cold_count):
        print(f'{count}: cold start {index + 1}/{cold_count}', flush=True)
        target = root / f'cold-{index}.json'
        started = time.perf_counter()
        subprocess.run([sys.executable, str(Path(__file__)), '--cold-root', str(root), '--output', str(target)],
                       check=True, env={**os.environ, 'CHRIPTMAS_APP_ROOT': str(root)}, stdout=subprocess.DEVNULL)
        value = json.loads(target.read_text(encoding='utf-8'))
        value['process_wall_ms'] = (time.perf_counter() - started) * 1000
        cold.append(value)
        save_json(checkpoint, state)
    calls_before = state['provider_calls']
    with TestClient(app) as client:
        for kind, amount in (('ask',ask_count), ('library',library_count), ('remember',remember_count)):
            for index in range(len(samples[kind]), amount):
                previous = {r.object_id for r in records.list(COLLECTION)}
                state['pending'] = {'kind':kind, 'operation':kind, 'index':index, 'previous':sorted(previous)}
                save_json(checkpoint, state)
                wait_before = provider.wait_ms
                started = time.perf_counter()
                if kind == 'library':
                    response = client.get('/api/v2/library/notes', params={'project_id':PROJECT})
                else:
                    text = f'资料{index % count:05d}' if kind == 'ask' else f'合成新增资料{index}：合成项目每周核对进度。'
                    response = client.post('/api/v2/workbench/turns',
                        json={'project_id':PROJECT, 'intent':kind, 'text':text},
                        headers={'Idempotency-Key':f'perf-{kind}-{index}',
                                 **({'Accept':'text/event-stream'} if kind == 'ask' else {})})
                wall = (time.perf_counter() - started) * 1000
                if kind == 'ask' and 'text/event-stream' in response.headers.get('content-type', ''):
                    outcome = sse_outcome(response.text, response.status_code)
                elif kind == 'ask':
                    outcome = {'status':'error', 'http_status':response.status_code, 'error_code':'unexpected_ask_response'}
                else:
                    outcome = {'status':'success' if response.is_success else 'error',
                        'http_status':response.status_code, 'error_code':None if response.is_success else 'http_error'}
                outcome['http_wall_ms'] = wall
                state['pending']['outcome'] = outcome
                save_json(checkpoint, state)
                measured = timing_after(records, previous, operation=kind)
                if kind == 'remember' and outcome['status'] == 'success':
                    turn = records.read('v2_turns', measured['turn_id'])
                    remembered = turn.payload['receipt']['remember'] if turn else {}
                    if remembered.get('state') != 'done':
                        outcome.update(status='error', error_code=remembered.get('error') or 'remember_not_done')
                samples[kind].append({**measured, **outcome, 'provider_wait_ms':provider.wait_ms-wait_before})
                state['provider_calls'] = None if calls_before is None else calls_before + provider.calls
                state.pop('pending')
                save_json(checkpoint, state)
                print(f'{count}: {kind} {index + 1}/{amount} {outcome["status"]}', flush=True)
    return {'documents':count, 'provider_calls':state['provider_calls'],
            'source_revision':state['source_revision'], 'command':state['command'],
            'measurement_schema':2, 'runs':state['runs'],
            'measurement':{'outcome':'HTTP/SSE outcome remains authoritative even after late background completion',
                'unobserved_stage':'null distribution with zero observed_samples',
                'resume':'completed request checkpoints only; interrupted requests are preserved without replay'},
            'operations':{kind:summarize(rows) for kind,rows in samples.items()},
            'cold_start_ms':distribution([row['ready_ms'] for row in cold]),
            'cold_starts':cold, 'raw_samples':samples}


def repository_revision():
    """Read the existing Git identity; this does not hash working files."""
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
            stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/qa/T14.0/baseline.json')
    parser.add_argument('--scales', type=int, nargs='+', default=[50, 500, 2000])
    parser.add_argument('--resume', action='store_true', help='Preserve completed scales and resume retained request checkpoints')
    parser.add_argument('--smoke', action='store_true', help='One sample per operation; not an acceptance baseline')
    parser.add_argument('--cold-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--scale-root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if any(n <= 0 for n in args.scales):
        parser.error('scales must be positive')
    os.environ['CHRIPTMAS_TURN_TIMINGS'] = '1'
    with offline():
        if args.cold_root:
            result = cold_start(args.cold_root)
        elif args.scale_root:
            kwargs = dict(ask_count=1, library_count=1, remember_count=1, cold_count=1) if args.smoke else {}
            result = measure_scale(args.scales[0], root=args.scale_root, **kwargs)
        else:
            result = {'schema_version': 1, 'python': platform.python_version(), 'platform': platform.platform(),
                'repository_revision': repository_revision(),
                'fixed_model': {'first_token_ms': 300, 'generation_ms': 1000, 'paid_calls': 0},
                'scope': 'shipping create_app, real ASGI routes, native structured streaming gateway, synthetic sources/documents',
                'measurement': {'local_ms': 'total_ms minus measured provider sleep only; generation includes local guards/schema and overlaps first_token',
                    'cold_start': 'new interpreter product import through production lifespan ready; OS filesystem cache not flushed',
                    'background': 'shipping lifespan background services retained; independent workers close before temporary-root cleanup',
                    'query_workload': 'explicit ask with exact unique synthetic document title; 20 distinct titles, fresh threads',
                    'vector': 'contextual vector eligibility, cache reads and enabled vector work; nonzero does not imply a remote embedding call',
                    'sqlite': 'trace statement count including transactions and PRAGMA; observer persistence excluded'},
                'smoke': args.smoke, 'scales': []}
            result = resume_result(args.output, result, resume=args.resume)
            result.setdefault('resume_history', []).append({'revision':repository_revision(), 'command':sys.argv})
            synthetic = Path(gettempdir()).resolve()
            for scale in args.scales:
                if any(row['documents'] == scale and complete_scale(row, smoke=args.smoke) for row in result['scales']):
                    print(f'{scale}: preserving completed scale', flush=True)
                    continue
                pending = result.get('pending_scale')
                if pending and pending['documents'] != scale:
                    raise RuntimeError('resume the retained pending scale before another scale')
                root = Path(pending['root']) if pending else Path(mkdtemp(prefix=f'chriptmas-perf-{scale}-'))
                result['pending_scale'] = {'documents':scale, 'root':str(root.resolve())}
                save_json(args.output, result)
                target = root / 'scale-result.json'
                command = [sys.executable, str(Path(__file__)), '--scale-root', str(root),
                           '--scales', str(scale), '--output', str(target)]
                if args.smoke:
                    command.append('--smoke')
                subprocess.run(command, check=True)
                completed = json.loads(target.read_text(encoding='utf-8'))
                if not complete_scale(completed, smoke=args.smoke):
                    raise RuntimeError('worker returned incomplete sample counts')
                result['scales'].append(completed)
                result.pop('pending_scale')
                save_json(args.output, result)
                if root.resolve().parent == synthetic and root.name.startswith('chriptmas-perf-'):
                    shutil.rmtree(root)

    save_json(args.output, result)
    print(str(args.output), flush=True)


if __name__ == '__main__':
    main()
