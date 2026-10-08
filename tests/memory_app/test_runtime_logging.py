import asyncio
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.shared.deployment import DeploymentLayout


def install(app, root):
    from backend.shared.runtime_logging import install_runtime_logging
    install_runtime_logging(app, DeploymentLayout('desktop', root))
    return app


def contents(root):
    return ''.join(p.read_text(encoding='utf8') for p in sorted((root / 'logs').glob('app-*.log')))


def control_app(root, name):
    app = FastAPI()
    @app.post('/control')
    async def control():
        logging.getLogger('control.domain').warning('domain %s', name)
        await asyncio.to_thread(logging.getLogger('control.thread').warning, 'thread %s', name)
        return {'name': name}
    @app.get('/error')
    async def error():
        raise ValueError('private exception question https://example.invalid/?secret=never')
    @app.on_event('startup')
    async def background():
        async def work():
            await asyncio.to_thread(logging.getLogger('control.background').warning, 'background %s', name)
        await asyncio.create_task(work())
    return install(app, root)


def test_two_apps_request_thread_background_and_close_are_isolated(tmp_path):
    first_root, second_root = tmp_path / 'first', tmp_path / 'second'
    first, second = control_app(first_root, 'first'), control_app(second_root, 'second')
    levels = (logging.getLogger().level, logging.getLogRecordFactory())
    with TestClient(first) as one, TestClient(second) as two:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda client: client.post('/control', json={'text': 'private question body'}, headers={'X-Request-Id': 'untrusted-id'}), [one, two]))
        ids = [response.headers['X-Request-Id'] for response in responses]
        assert ids[0] != ids[1] and 'untrusted-id' not in ids
        assert first_root.joinpath('logs').is_dir() and second_root.joinpath('logs').is_dir()
        for root, name, other, request_id in [(first_root, 'first', 'second', ids[0]), (second_root, 'second', 'first', ids[1])]:
            text = contents(root)
            assert f'domain {name}' in text and f'thread {name}' in text and f'background {name}' in text
            assert f'domain {other}' not in text and f'background {other}' not in text
            assert request_id in text and 'request_id=-' in text
            assert 'private question body' not in text
    saved = contents(first_root), contents(second_root)
    logging.getLogger('control.domain').warning('outside both roots')
    assert saved == (contents(first_root), contents(second_root))
    assert levels == (logging.getLogger().level, logging.getLogRecordFactory())


def test_same_root_reference_counts_and_exception_behavior(tmp_path):
    first, second = control_app(tmp_path, 'first'), control_app(tmp_path, 'second')
    initial = tuple(logging.getLogger().handlers)
    with TestClient(second, raise_server_exceptions=False) as two:
        with TestClient(first) as one:
            assert one.post('/control').status_code == 200
        response = two.get('/error?question=never')
        assert response.status_code == 500 and response.text == 'Internal Server Error'
        assert response.headers['X-Request-Id'] in contents(tmp_path)
        assert 'ValueError' in contents(tmp_path)
        assert 'private exception question' not in contents(tmp_path) and '?question=' not in contents(tmp_path)
        assert two.post('/control').status_code == 200
    assert tuple(logging.getLogger().handlers) == initial


def test_formatted_redaction_size_and_fourteen_day_retention_preserve_unknown(tmp_path):
    from backend.shared.runtime_logging import RuntimeLogHandler, runtime_log_scope
    root = tmp_path / 'logs'
    root.mkdir()
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    expired = root / 'app-2026-10-06.log'
    expired.write_text('old')
    boundary = root / 'app-2026-10-07.1.log'
    boundary.write_text('retain')
    unknown = root / 'operator-2026-01-01.log'
    unknown.write_text('operator')
    handler = RuntimeLogHandler(root, now=lambda: now)
    record = logging.LogRecord('test', logging.WARNING, __file__, 1, 'URL %s secret %s key %s', ('https://example.invalid/search?question=private', 'sk-' + 'S'*30, 'api_key="'+'K'*28+'"'), None)
    with runtime_log_scope(root):
        handler.handle(record)
        for _ in range(12):
            handler.handle(logging.LogRecord('test', logging.WARNING, __file__, 1, 'x'*1024*1024, (), None))
    handler.close()
    files = list(root.glob('app-2026-10-20*.log'))
    assert len(files) >= 2 and all(file.stat().st_size <= 10*1024*1024 for file in files)
    text = ''.join(file.read_text() for file in files)
    assert '?question=' not in text and 'sk-'+'S'*30 not in text and 'K'*28 not in text
    assert '[REDACTED_SECRET]' in text and 'https://example.invalid/search' in text
    assert not expired.exists() and boundary.exists() and unknown.read_text() == 'operator'


@pytest.mark.parametrize('mode', ['desktop', 'server'])
def test_real_application_factory_uses_deployment_log_root(tmp_path, monkeypatch, mode):
    root = tmp_path / mode
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', mode)
    user = root if mode == 'desktop' else root / 'users/local-user'
    (user / 'config').mkdir(parents=True)
    (user / 'config/settings.toml').write_text('[asr]\nprovider="faster_whisper"\n[asr.faster_whisper]\ndevice="cpu"\nmodel_size="tiny"\ncompute_type="int8"\n')
    from backend.memory_app.app import create_app
    app = create_app(runtime_root=user, legacy_app=FastAPI())
    log_root = root / 'logs' if mode == 'desktop' else root / 'server/logs'
    assert not log_root.exists()
    with TestClient(app) as client:
        response = client.get('/api/v2/jobs?question=private')
        assert response.status_code == (200 if mode == 'desktop' else 401)
        log_root = root / 'logs' if mode == 'desktop' else root / 'server/logs'
        text = ''.join(file.read_text() for file in log_root.glob('app-*.log'))
        assert response.headers['X-Request-Id'] in text and '?question=' not in text
        assert not (user / 'logs').exists() if mode == 'server' else True


def test_child_turn_binding_does_not_mutate_parent_or_sibling_context(tmp_path):
    from backend.shared.runtime_logging import _context, bind_turn_id
    app = FastAPI()
    @app.get('/children')
    async def children():
        ready, release = asyncio.Event(), asyncio.Event()
        async def first():
            bind_turn_id('turn-first')
            ready.set()
            await release.wait()
            assert _context.get().turn_id == 'turn-first'
        async def second():
            await ready.wait()
            bind_turn_id('turn-second')
            release.set()
            assert _context.get().turn_id == 'turn-second'
        await asyncio.gather(first(), second())
        assert _context.get().turn_id == '-'
        return {'ok': True}
    with TestClient(install(app, tmp_path)) as client:
        assert client.get('/children').status_code == 200


def test_overlapping_requests_both_keep_safe_access_records(tmp_path):
    import threading
    entered, release = threading.Event(), threading.Event()
    app = FastAPI()
    @app.get('/slow')
    async def slow():
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return {'ok': True}
    @app.get('/fast')
    async def fast():
        return {'ok': True}
    with TestClient(install(app, tmp_path)) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(client.get, '/slow')
            try:
                assert entered.wait(5)
                fast_result = client.get('/fast')
            finally:
                release.set()
            slow_result = first.result(timeout=10)
        text = contents(tmp_path)
        assert 'path=/fast' in text and 'path=/slow' in text
        assert fast_result.headers['X-Request-Id'] in text and slow_result.headers['X-Request-Id'] in text


def test_without_lifespan_background_retains_root_until_completion_then_closes(tmp_path):
    import httpx
    app = FastAPI()
    before = tuple(logging.getLogger().handlers)
    async def scenario():
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        @app.get('/detach')
        async def detach():
            async def child():
                entered.set()
                await release.wait()
                await asyncio.to_thread(logging.getLogger('control.detached').warning, 'detached root-owned')
                finished.set()
            asyncio.create_task(child())
            return {'accepted': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=install(app,tmp_path)),base_url='http://test') as client:
            response = await client.get('/detach')
            assert response.status_code == 200 and response.headers['X-Request-Id']
            await entered.wait()
            release.set()
            await finished.wait()
            await asyncio.sleep(0)
            assert 'detached root-owned' in contents(tmp_path)
    asyncio.run(scenario())
    assert tuple(logging.getLogger().handlers) == before


@contextmanager
def linked_log_directory():
    import os, stat, subprocess, tempfile
    # F: does not support junction creation here; use an owned system-temp
    # root on the filesystem where real Windows reparse points are available.
    with tempfile.TemporaryDirectory(prefix='c14-loglink-') as temporary:
        parent=Path(temporary).resolve()
        root,outside=parent/'application',parent/'outside'
        root.mkdir();outside.mkdir()
        old=outside/'app-2000-01-01.log';old.write_text('operator data')
        link=root/'logs'
        if os.name=='nt':
            harness=parent/'junction.ps1'
            harness.write_text('param($Link,$Target)\n$ErrorActionPreference="Stop"\nNew-Item -ItemType Junction -Path $Link -Target $Target')
            subprocess.run(['pwsh','-NoProfile','-File',str(harness),str(link),str(outside)],check=True,capture_output=True)
            assert getattr(link.lstat(),'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        else:
            link.symlink_to(outside,target_is_directory=True)
        try:
            yield root,outside,old
        finally:
            assert link.absolute().parent==root.absolute() and root.absolute().is_relative_to(parent)
            link.unlink() if link.is_symlink() else link.rmdir()


def test_logs_junction_never_writes_or_prunes_outside_authorized_directory(tmp_path):
    with linked_log_directory() as (root,outside,old):
        with TestClient(control_app(root,'linked')) as client:
            assert client.post('/control').status_code==200
        assert old.read_text()=='operator data' and list(outside.iterdir())==[old]


def test_multiline_and_oversized_unicode_records_keep_request_prefix_on_each_line(tmp_path):
    app = FastAPI()
    @app.get('/format')
    async def formatted():
        logging.getLogger('control.format').warning('first\nsecond\rthird')
        logging.getLogger('control.format').warning('界'*(4*1024*1024))
        return {'ok':True}
    with TestClient(install(app,tmp_path)) as client:
        response=client.get('/format')
        request_id=response.headers['X-Request-Id']
    files=list((tmp_path/'logs').glob('app-*.log'))
    assert len(files)>=2 and all(path.stat().st_size<=10*1024*1024 for path in files)
    lines=[line for path in files for line in path.read_text(encoding='utf8').splitlines() if line]
    assert all(f'request_id={request_id} turn_id=-' in line for line in lines)
    assert sum(line.count('界') for line in lines)==4*1024*1024


def turn_application(tmp_path, monkeypatch, timings='1'):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY','desktop')
    monkeypatch.setenv('CHRIPTMAS_TURN_TIMINGS', timings)
    (tmp_path/'config').mkdir()
    (tmp_path/'config/settings.toml').write_text('[asr]\nprovider="faster_whisper"\n[asr.faster_whisper]\ndevice="cpu"\nmodel_size="tiny"\ncompute_type="int8"\n')
    from tests.memory_app.test_api import TurnModels, _shutdown
    from backend.memory_app.app import create_app
    from backend.memory_app.storage_authority import resolve_recognition_document_store
    records, _ = resolve_recognition_document_store(tmp_path)
    model = TurnModels(records,tmp_path)
    model.update('generation', {'base_url':'https://example.test','model':'test-model','api_key':'synthetic-only','allow_remote':True,'expected_revision':0})
    app = create_app(runtime_root=tmp_path,legacy_app=FastAPI(),model_configuration=model)
    return app, records


@pytest.mark.parametrize('timings', ['1','0'])
@pytest.mark.parametrize('intent', ['ask','do'])
def test_actual_turn_access_logs_match_receipt_and_optional_timing(tmp_path, monkeypatch, timings, intent):
    app, records = turn_application(tmp_path, monkeypatch, timings)
    from tests.memory_app.test_api import _shutdown
    try:
        with TestClient(app) as client:
            response = client.post('/api/v2/workbench/turns',json={'project_id':'project-a','intent':intent,'text':'private question body unique'},headers={'X-Request-Id':'untrusted'})
            assert response.status_code == 200, response.text
            saved = response.json()['turn']
            identity = saved['id']
            assert records.read('v2_turns',identity).payload['receipt'] == saved['receipt']
            text = contents(tmp_path)
            lines = [line for line in text.splitlines() if 'http method=POST path=/api/v2/workbench/turns ' in line]
            assert len(lines)==1
            assert f'turn_id={identity}' in lines[0]
            assert f"request_id={response.headers['X-Request-Id']}" in lines[0]
            assert 'private question body unique' not in text
            if timings=='1':
                from time import monotonic, sleep
                deadline=monotonic()+10
                timing=records.read('v2_turn_timings',identity)
                while timing is None and monotonic()<deadline:
                    sleep(0.05)
                    timing=records.read('v2_turn_timings',identity)
                assert timing is not None and timing.payload['turn_id']==identity
            else:
                assert records.read('v2_turn_timings',identity) is None
    finally:
        _shutdown(TestClient(app))


def test_exception_message_arguments_and_uvicorn_tuple_are_redacted_without_mutation(tmp_path):
    app=FastAPI()
    original=[]
    @app.get('/exception-format')
    async def formatted():
        try:
            raise ValueError('private exception body unique')
        except ValueError as error:
            logging.getLogger('control.exception').exception('details=%s',error)
        return {'ok':True}
    with TestClient(install(app,tmp_path)) as client:
        response=client.get('/exception-format')
    text=contents(tmp_path)
    assert 'private exception body unique' not in text and 'ValueError' in text
    from backend.shared.runtime_logging import RuntimeLogHandler,runtime_log_scope
    handler=RuntimeLogHandler(tmp_path/'logs')
    record=logging.LogRecord('uvicorn.access',logging.INFO,__file__,1,'%s - "%s %s HTTP/%s" %d',('127.0.0.1','GET','/search?question=private','1.1',200),None)
    message=record.getMessage()
    synthetic_pem_label = 'PRIVATE' + ' KEY'
    synthetic_pem = '-----BEGIN ' + synthetic_pem_label + '-----\nsynthetic private block\n-----END ' + synthetic_pem_label + '-----'
    with runtime_log_scope(tmp_path/'logs'):
        handler.handle(record)
        handler.handle(logging.LogRecord('test',logging.WARNING,__file__,1,'PEM %s',(synthetic_pem,),None))
    handler.close()
    assert record.getMessage()==message and 'question=private' not in contents(tmp_path)
    assert 'synthetic private block' not in contents(tmp_path)


@pytest.mark.parametrize('shape',['bad-format','broken-string'])
def test_diagnostic_record_failure_never_changes_http_response(tmp_path, shape):
    from backend.shared.runtime_logging import RuntimeLogHandler
    app=FastAPI()
    class BrokenText:
        def __str__(self):
            raise RuntimeError('synthetic formatter failure')
    @app.get('/bad-log')
    async def bad_log():
        handler=next(item for item in logging.getLogger().handlers if isinstance(item,RuntimeLogHandler) and item.root==tmp_path/'logs')
        logger=logging.Logger('control.bad',level=logging.WARNING)
        logger.propagate=False
        logger.addHandler(handler)
        try:
            if shape=='bad-format':
                logger.warning('number=%d','invalid-number')
            else:
                logger.warning('object=%s',BrokenText())
        finally:
            logger.removeHandler(handler)
        return {'ok':True}
    with TestClient(install(app,tmp_path),raise_server_exceptions=False) as client:
        response=client.get('/bad-log')
        assert response.status_code==200 and response.json()=={'ok':True}


@pytest.mark.parametrize('shape',['message','mapping'])
def test_known_exception_objects_log_only_the_type(tmp_path, shape):
    from backend.shared.runtime_logging import RuntimeLogHandler
    app=FastAPI()
    @app.get('/exception-object')
    async def object_log():
        handler=next(item for item in logging.getLogger().handlers if isinstance(item,RuntimeLogHandler) and item.root==tmp_path/'logs')
        logger=logging.Logger('control.exception-object',level=logging.WARNING)
        logger.propagate=False
        logger.addHandler(handler)
        try:
            if shape=='message':
                logger.warning(ValueError('private exception object unique'))
            else:
                logger.warning('error=%(error)s',{'error':ValueError('private exception object unique')})
        finally:
            logger.removeHandler(handler)
        return {'ok':True}
    with TestClient(install(app,tmp_path)) as client:
        assert client.get('/exception-object').status_code==200
    text=contents(tmp_path)
    assert 'private exception object unique' not in text and 'ValueError' in text


def test_relative_root_query_is_removed_from_real_formatted_record(tmp_path):
    app=FastAPI()
    @app.get('/root-log')
    async def root_log():
        logging.getLogger('control.root-query').warning('GET %s HTTP/1.1','/?question=private-root-query')
        return {'ok':True}
    with TestClient(install(app,tmp_path)) as client:
        assert client.get('/root-log').status_code==200
    text=contents(tmp_path)
    assert 'GET / HTTP/1.1' in text and 'question=private-root-query' not in text


def test_original_access_buffer_entry_removes_relative_root_query():
    from backend.api.access_log import make_access_log_entry
    entry=make_access_log_entry(method='GET',path='/?question=private-root-query',status=200,duration_ms=1,timestamp='synthetic')
    assert entry['path']=='/' and entry['stage']=='GET /'
    assert set(entry)=={'id','component','method','path','status','duration_ms','timestamp','stage'}


def test_real_completed_ask_replay_keeps_turn_and_uses_new_request_id(tmp_path, monkeypatch):
    app,records=turn_application(tmp_path,monkeypatch)
    from tests.memory_app.test_api import _shutdown
    body={'project_id':'project-a','intent':'ask','text':'private replay question unique'}
    try:
        with TestClient(app) as client:
            first=client.post('/api/v2/workbench/turns',json=body,headers={'Idempotency-Key':'logging-replay-control'})
            second=client.post('/api/v2/workbench/turns',json=body,headers={'Idempotency-Key':'logging-replay-control'})
            assert first.status_code==second.status_code==200
            assert first.json()==second.json()
            identity=first.json()['turn']['id']
            assert first.headers['X-Request-Id']!=second.headers['X-Request-Id']
            lines=[line for line in contents(tmp_path).splitlines() if 'http method=POST path=/api/v2/workbench/turns ' in line]
            assert len(lines)==2
            for response,line in zip([first,second],lines):
                assert f'turn_id={identity}' in line
                assert f"request_id={response.headers['X-Request-Id']}" in line
            assert len(records.list('v2_turns'))==1
            assert 'private replay question unique' not in contents(tmp_path)
    finally:
        _shutdown(TestClient(app))


def test_real_logs_are_excluded_from_existing_verified_backup_owner(tmp_path):
    from backend.memory_app.backup import backup_runtime
    from core.storage_provider import SQLiteStructuredRecordStore
    root=tmp_path/'application'
    root.mkdir()
    records=SQLiteStructuredRecordStore(root/'records.sqlite3')
    with records.begin() as tx:
        tx.put('synthetic','item',{'value':'local synthetic'},expected_revision=0)
        tx.commit()
    with TestClient(control_app(root,'backup-control')) as client:
        assert client.post('/control').status_code==200
    assert contents(root)
    snapshot=backup_runtime(root,tmp_path/'backups',snapshot_id='snap-log-exclusion')
    assert not (snapshot.snapshot_root/'payload/logs').exists()
    assert (snapshot.snapshot_root/'payload/records.sqlite3').is_file()


def test_closed_application_context_cannot_write_through_another_same_root_app(tmp_path):
    from contextvars import copy_context
    import threading
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    first=FastAPI()
    @first.get('/spawn-thread')
    async def spawn():
        context=copy_context()
        def work():
            entered.set()
            try:
                assert release.wait(10)
                logging.getLogger('control.stale').warning('closed application stale context')
            finally:
                finished.set()
        worker=threading.Thread(target=context.run,args=(work,),daemon=True)
        first.state.worker=worker
        worker.start()
        return {'ok':True}
    try:
        with TestClient(install(first,tmp_path)) as client:
            assert client.get('/spawn-thread').status_code==200
            assert entered.wait(5)
        with TestClient(control_app(tmp_path,'fresh')) as client:
            release.set()
            assert finished.wait(5)
            first.state.worker.join(5)
            assert client.post('/control').status_code==200
            assert 'domain fresh' in contents(tmp_path)
            assert 'closed application stale context' not in contents(tmp_path)
    finally:
        release.set()
        if hasattr(first.state,'worker'):
            first.state.worker.join(5)


def test_python311_path_surface_still_logs_and_refuses_real_junction(tmp_path, monkeypatch):
    # Environment compatibility control, not a replacement for the handler.
    monkeypatch.delattr(Path,'is_junction',raising=False)
    normal=tmp_path/'normal'
    with TestClient(control_app(normal,'python311-surface')) as client:
        assert client.post('/control').status_code==200
    assert 'domain python311-surface' in contents(normal)
    with linked_log_directory() as (root,outside,old):
        with TestClient(control_app(root,'python311-linked')) as client:
            assert client.post('/control').status_code==200
        assert old.read_text()=='operator data' and list(outside.iterdir())==[old]
