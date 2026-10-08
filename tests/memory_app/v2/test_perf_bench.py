import importlib.util
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[3] / 'tools/perf_bench.py'
    spec = importlib.util.spec_from_file_location('perf_bench_test', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_percentiles_and_local_time_exclude_provider_wait():
    bench = module()
    rows = [{'total_ms': 1500 + i, 'provider_wait_ms': 1000, 'connection_count': 4, 'statement_count': 7,
             'stages_ms': {'generation': 1000, 'first_token': 300, 'keyword': 100}}
            for i in range(20)]
    result = bench.summarize(rows)
    assert result['local_ms']['p50'] == 509.5
    assert result['local_ms']['p95'] == pytest.approx(518.05)
    assert result['connection_count']['p50'] == 4


def test_fixed_model_stream_has_first_and_total_delay():
    bench = module()
    waits = []
    model = bench.FixedCompletion(sleep=waits.append)
    chunks = list(model(stream=True, messages=[{'role': 'user', 'content': '[1] evidence'}]))
    assert waits == [0.3, 0.7]
    assert chunks[0]['choices'][0]['delta']['content'].startswith('{"answer":')
    assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'


def test_offline_adapter_rejects_remote_network():
    import socket
    bench = module()
    with bench.offline(), socket.socket() as connection:
        with pytest.raises(RuntimeError, match='forbids network'):
            connection.connect(('192.0.2.1', 443))


def test_auxiliary_query_model_returns_valid_schema():
    import json
    bench = module()
    from backend.memory_app.v2.multi_query import QueryVariants
    model = bench.FixedCompletion(sleep=lambda _: None)
    output = model(messages=[{'role': 'system', 'content': '只返回JSON {"queries":["问法"]}。'}])
    raw = output['choices'][0]['message']['content']
    assert QueryVariants.model_validate(json.loads(raw)).queries == []


def test_seed_creates_real_visible_documents(tmp_path, monkeypatch):
    bench = module()
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    with bench.offline():
        app, records, documents, _ = bench.make_application(tmp_path)
        bench.seed_corpus(tmp_path, records, documents, 3)
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            response = client.get('/api/v2/library/notes', params={'project_id': bench.PROJECT})
    assert response.status_code == 200
    assert len(documents.list()) == 3
    assert len(records.list('workspace_review_intents')) == 3
    assert '资料00000' in response.text


def test_sse_error_after_keepalives_is_a_failed_request():
    bench = module()
    result = bench.sse_outcome(': keep-alive\r\n\r\nevent: error\r\ndata: {"code":"answer_generation_failed"}\r\n\r\n', 200)
    assert result['status'] == 'error'
    assert result['error_code'] == 'answer_generation_failed'


def test_unobserved_stages_are_missing_not_zero_latency():
    bench = module()
    row = {'status': 'error', 'http_wall_ms': 250000, 'total_ms': 310000,
           'provider_wait_ms': 0, 'connection_count': 5, 'statement_count': 20,
           'stages_ms': {'generation': 0, 'keyword': 20},
           'stage_observations': {'generation': 0, 'keyword': 1}}
    result = bench.summarize([row])
    assert result['successes'] == 0 and result['failures'] == 1
    assert result['stages_ms']['generation'] is None
    assert result['stage_observed_samples']['generation'] == 0
    assert result['http_wall_ms']['p50'] == 250000
    assert result['total_ms']['p50'] == 310000
    assert result['slowest_three_stages'] == ['keyword']


def test_resume_preserves_complete_scale_payloads(tmp_path):
    import json
    bench = module()
    old = {'repository_revision':'original', 'scales':[{
        'documents':50, 'raw_samples':{'ask':[{}]*20,'library':[{}]*20,'remember':[{}]*5},
        'cold_starts':[{}]*3, 'custom_original_evidence':42}]}
    path = tmp_path/'baseline.json'
    path.write_text(json.dumps(old))
    loaded = bench.resume_result(path, old, resume=True)
    assert loaded == old
    assert bench.complete_scale(loaded['scales'][0])
    assert not bench.complete_scale({'documents':2000,'raw_samples':{},'cold_starts':[]})


def test_error_timing_wait_does_not_replace_request_outcome():
    from types import SimpleNamespace
    bench = module()
    class Records:
        def __init__(self): self.calls = 0
        def list(self, collection):
            self.calls += 1
            return [] if self.calls == 1 else [SimpleNamespace(object_id='late', payload={'operation':'ask','total_ms':300001})]
    records = Records()
    outcome = bench.sse_outcome('event: error\ndata: {"code":"answer_generation_failed"}\n\n',200)
    measured = bench.timing_after(records, set(), operation='ask', poll=lambda _:None)
    sample = {**measured, **outcome}
    assert sample['status'] == 'error' and sample['total_ms'] == 300001


def test_interrupted_checkpoint_is_not_replayed_or_opened(tmp_path, monkeypatch):
    import json
    bench = module()
    checkpoint = {'documents':2000, 'seeded':True, 'pending':{'kind':'ask','index':0}}
    (tmp_path/'measurement-checkpoint.json').write_text(json.dumps(checkpoint))
    def forbidden(root):
        raise AssertionError('app must not start for an interrupted request')
    monkeypatch.setattr(bench, 'make_application', forbidden)
    with pytest.raises(RuntimeError, match='do not replay'):
        bench.measure_scale(2000, root=tmp_path)
    assert json.loads((tmp_path/'measurement-checkpoint.json').read_text()) == checkpoint


def test_sse_done_without_match_is_not_model_success():
    import json
    bench = module()
    payload = {'turn':{'id':'turn-one','receipt':{'ask':{'no_match':True}}}}
    outcome = bench.sse_outcome('event: done\ndata: '+json.dumps(payload)+'\n\n',200)
    assert outcome['status'] == 'error' and outcome['error_code'] == 'no_match'


def test_resume_skips_complete_scales_without_workers(tmp_path, monkeypatch):
    import json
    bench = module()
    scale = {'documents':50, 'raw_samples':{'ask':[{}]*20,'library':[{}]*20,'remember':[{}]*5},
             'cold_starts':[{}]*3, 'original_metadata':{'revision':'original'}}
    path = tmp_path/'baseline.json'
    path.write_text(json.dumps({'schema_version':1,'smoke':False,'scales':[scale]}))
    monkeypatch.setattr(bench.sys, 'argv', ['perf_bench.py','--resume','--scales','50','--output',str(path)])
    monkeypatch.setattr(bench, 'repository_revision', lambda:'new-script')
    def forbidden(*args, **kwargs):
        raise AssertionError('completed scale must not launch a worker')
    monkeypatch.setattr(bench.subprocess, 'run', forbidden)
    bench.main()
    assert json.loads(path.read_text())['scales'] == [scale]


def test_real_sse_model_error_finishes_same_request_timing(tmp_path, monkeypatch):
    import time
    bench = module()
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    calls = []
    def failed_provider(self, **request):
        calls.append(request)
        raise RuntimeError('synthetic provider failure')
    monkeypatch.setattr(bench.FixedCompletion, '__call__', failed_provider)
    with bench.offline():
        app, records, documents, _ = bench.make_application(tmp_path)
        bench.seed_corpus(tmp_path, records, documents, 3)
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            before = {row.object_id for row in records.list(bench.COLLECTION)}
            response = client.post('/api/v2/workbench/turns',
                json={'project_id':bench.PROJECT,'intent':'ask','text':'资料00000'},
                headers={'Accept':'text/event-stream','Idempotency-Key':'perf-error-once'})
            outcome = bench.sse_outcome(response.text, response.status_code)
            assert outcome['status'] == 'error', response.text
            assert outcome['error_code'] == 'answer_generation_failed'
            assert calls
            count = len(calls)
            deadline = time.monotonic() + 10
            def bounded_poll(seconds):
                assert time.monotonic() < deadline, 'real error observation did not finish'
                time.sleep(min(seconds, .05))
            measured = bench.timing_after(records, before, operation='ask', poll=bounded_poll)
            sample = {**measured, **outcome}
            assert sample['status'] == 'error'
            assert sample['total_ms'] > 0 and sample['connection_count'] > 0
            assert len(calls) == count
            requests = records.list('v2_turn_requests')
            assert len(requests) == 1 and requests[0].object_id == 'perf-error-once'
            assert requests[0].payload['state'] == 'failed'
            assert len(records.list(bench.COLLECTION)) == len(before) + 1
