from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from shutil import copyfile
import sqlite3

from fastapi.testclient import TestClient
import pytest


def test_real_local_transport_failure_keeps_unknown_but_allows_completed_nomatch(tmp_path):
    allocated = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = tmp_path.resolve(strict=True)
    assert root.is_relative_to(allocated)
    previous_root = os.environ['CHRIPTMAS_APP_ROOT']
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    (root / 'config').mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    from backend.memory_app.app import create_app
    from backend.memory_app.kernel.route_source_snapshot import require_closed_answer
    from backend.recognition import RecognitionConflict

    app = create_app(runtime_root=root)
    records, models = app.state.recognition_records, app.state.recognition_models
    configured = models.public_for_purpose('generation')
    models.update('generation', {'base_url': 'http://127.0.0.1:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-transport-rejection', 'allow_remote': False, 'enabled': True,
        'expected_revision': configured['revision']})
    try:
        with TestClient(app) as client:
            project = client.post('/api/v2/projects', json={'name': '本机终态资格检查'}).json()['id']
            response = client.post('/api/v2/workbench/turns', json={'project_id': project,
                'intent': 'ask', 'text': '这个空项目有哪些已确认资料？'})
            assert response.status_code == 200, response.json()
            row = records.read('v2_turns', response.json()['turn']['id'])
            assert row.payload['receipt']['ask']['no_match'] is True
            store = app.state.ai_turn_store
            assert store.events_after(row.object_id)[-1]['type'] == 'turn.completed'
            database = root / '.rebuild-data/ai-turns.sqlite3'
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                effects = tuple(connection.execute('SELECT operation_id,state FROM effect WHERE turn_id=? ORDER BY operation_id', (row.object_id,)))
                receipts = tuple(connection.execute("SELECT payload_ref,payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind='model-wire-attempt-receipt' ORDER BY payload_ref", (row.object_id,)))
            failed = [json.loads(body) for _, body in receipts]
            assert failed and all(receipt['status'] == 'failed_transport' for receipt in failed)
            assert all(receipt['usage_status'] == 'unavailable' and receipt['usage'] is None for receipt in failed)
            assert sum(state == 'UNKNOWN' for _, state in effects) == len(failed)
            assert require_closed_answer(records, row) == 'completed'
            source = client.get('/api/v2/workbench/turns/' + row.object_id + '/route-correction',
                params={'project_id': project})
            assert source.status_code == 200, source.json()
            assert records.read('v2_route_examples', row.object_id) is not None
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                assert effects == tuple(connection.execute('SELECT operation_id,state FROM effect WHERE turn_id=? ORDER BY operation_id', (row.object_id,)))
                assert receipts == tuple(connection.execute("SELECT payload_ref,payload_json FROM ai_turn_payloads WHERE turn_id=? AND kind='model-wire-attempt-receipt' ORDER BY payload_ref", (row.object_id,)))
            now = datetime.now(timezone.utc)
            lease = store.try_acquire_run_lease(row.object_id, 'actual-source-check-owner',
                now=now, stale_after=now + timedelta(minutes=1))
            assert lease is not None
            try:
                with pytest.raises(RecognitionConflict, match='route_correction_source_unfinished'):
                    require_closed_answer(records, row)
            finally:
                store.release_strict_run_lease(lease)
            assert require_closed_answer(records, row) == 'completed'
    finally:
        os.environ['CHRIPTMAS_APP_ROOT'] = previous_root
