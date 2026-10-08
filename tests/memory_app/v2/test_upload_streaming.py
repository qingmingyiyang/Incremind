import asyncio
from io import BytesIO
from pathlib import Path
from threading import RLock

import pytest
from fastapi import HTTPException, UploadFile

from backend.memory_app.workspace_intake import WorkspaceIntake
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.workspace_items import WorkspaceItems


class BoundedUpload(UploadFile):
    async def read(self, size=-1):
        assert 0 < size <= 1024 * 1024
        return await super().read(size)


def test_audio_upload_reads_bounded_chunks_and_stages_only_after_copy(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    owner = WorkspaceIntake(tmp_path, WorkspaceItems(records, None, RLock()), None)
    owner.root.mkdir()
    source = b'OggS' + b'x' * (2 * 1024 * 1024)
    result = asyncio.run(owner.add_file('alpha', BoundedUpload(BytesIO(source), filename='sample.ogg')))
    row = records.read('workspace_items', result['id'])
    assert row.payload['status'] == 'staged'
    assert row.payload['input_kind'] == 'audio'
    assert Path(row.payload['original_path']).read_bytes() == source
    assert 'original_path' not in result


@pytest.mark.parametrize('failure', ['empty', 'too_large', 'read_error', 'cancelled'])
def test_failed_copy_leaves_no_item_or_partial_file(tmp_path, failure):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    owner = WorkspaceIntake(tmp_path, WorkspaceItems(records, None, RLock()), None)
    owner.root.mkdir()
    data = b'' if failure == 'empty' else b'x' * (13 * 1024 * 1024 if failure == 'too_large' else 1024 * 1024 + 1)
    class Upload(BoundedUpload):
        reads = 0
        async def read(self, size=-1):
            self.reads += 1
            if self.reads == 2 and failure == 'read_error':
                raise OSError('synthetic read interruption')
            if self.reads == 2 and failure == 'cancelled':
                raise asyncio.CancelledError()
            return await super().read(size)
    expected = asyncio.CancelledError if failure == 'cancelled' else OSError if failure == 'read_error' else HTTPException
    with pytest.raises(expected) as error:
        asyncio.run(owner.add_file('alpha', Upload(BytesIO(data), filename='sample.ogg')))
    if expected is HTTPException:
        assert error.value.status_code == 413
    assert records.list('workspace_items') == ()
    assert tuple(owner.root.iterdir()) == ()


def test_v2_upload_guard_accepts_existing_audio_size_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.toml').write_bytes((Path(__file__).parents[3] / 'config' / 'settings.toml.example').read_bytes())
    from backend.memory_app.app import create_app
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.memory_app.v2.test_workbench_remember import Model
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=Model())
    with TestClient(app) as http:
        response = http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
            files={'file': ('sample.ogg', b'OggS' + b'x' * (256 * 1024), 'audio/ogg')})
        assert response.status_code == 200, response.text
        assert response.json()['input_kind'] == 'audio'


@pytest.mark.parametrize('filename, expected_status, expected_items', [('sample.ogg', 413, 1), ('sample.mp4', 200, 2)])
def test_real_multipart_memory_is_bounded_for_100mb_stream(tmp_path, monkeypatch, filename, expected_status, expected_items):
    import gc
    import tracemalloc
    import threading
    import psutil
    import httpx
    from fastapi import FastAPI
    from tests.memory_app.v2.test_workbench_remember import assemble, Model
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    app, domains = assemble(tmp_path, records, SQLiteDocumentRepository(records), RecognitionService(records), Model())
    boundary = 'intake-boundary'
    header = (f'--{boundary}\r\nContent-Disposition: form-data; name="project_id"\r\n\r\nalpha\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        'Content-Type: audio/ogg\r\n\r\n').encode()
    trailer = f'\r\n--{boundary}--\r\n'.encode()
    async def upload(size):
        path = tmp_path / f'input-{size}.ogg'
        with path.open('wb') as stream:
            stream.truncate(size)
        async def body():
            yield header
            with path.open('rb') as stream:
                while data := stream.read(64 * 1024):
                    yield data
            yield trailer
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            gc.collect()
            process = psutil.Process()
            baseline_rss = process.memory_info().rss
            rss = [baseline_rss]
            stop = threading.Event()
            def sample_rss():
                while not stop.wait(.005):
                    rss[0] = max(rss[0], process.memory_info().rss)
            monitor = threading.Thread(target=sample_rss, daemon=True)
            monitor.start()
            tracemalloc.start()
            try:
                result = await client.post('/api/v2/workbench/files', content=body(),
                    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
                stop.set()
                monitor.join()
        return result, peak, max(0, rss[0] - baseline_rss)
    small, small_peak, small_rss = asyncio.run(upload(2 * 1024 * 1024))
    large, large_peak, large_rss = asyncio.run(upload(100 * 1024 * 1024))
    assert small.status_code == 200, small.text
    assert large.status_code == expected_status, large.text
    assert large_peak <= small_peak + 3 * 1024 * 1024
    assert large_rss <= small_rss + 8 * 1024 * 1024
    assert len(records.list('workspace_items')) == expected_items
    assert len(tuple(domains.intake.root.iterdir())) == expected_items
    print(f'multipart peak bytes: 2MiB={small_peak}; 100MiB={large_peak}; rss deltas={small_rss}/{large_rss}; status={large.status_code}; type={filename}')
