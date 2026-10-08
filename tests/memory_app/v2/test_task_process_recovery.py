import json
from pathlib import Path
import subprocess
import sys
import time


def test_killed_worker_does_not_rebill_completed_child(tmp_path):
    command = [sys.executable, '-m', 'tests.memory_app.v2.task_crash_worker', str(tmp_path)]
    with (tmp_path/'start.log').open('wb') as log:
        process = subprocess.Popen(command+['start'], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic()+120
            while not (tmp_path/'ready.json').exists() and time.monotonic()<deadline:
                assert process.poll() is None, (tmp_path/'start.log').read_text(errors='replace')[-3000:]
                time.sleep(.1)
            assert (tmp_path/'ready.json').exists(), 'first child must complete while second transport is in flight'
            before = json.loads((tmp_path/'ready.json').read_text())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
    with (tmp_path/'recover.log').open('wb') as log:
        recovered = subprocess.run(command+['recover'], stdout=log, stderr=subprocess.STDOUT, timeout=140)
    assert recovered.returncode == 0, (tmp_path/'recover.log').read_text(errors='replace')[-4000:]
    recovery = json.loads((tmp_path/'recovered.json').read_text())
    receipt = recovery['receipt']
    completed = next(row for row in before['division'] if row['state']=='done')
    assert any(row['turn_id']==completed['turn_id'] and row['state']=='done' for row in receipt['division'])
    calls = [json.loads(line) for line in (tmp_path/'calls.jsonl').read_text().splitlines()]
    assert not [call for call in calls if call['phase']=='recover'], 'neither completed nor uncertain wire calls may be replayed'
    assert recovery['lease_status'] == 'quarantined'
