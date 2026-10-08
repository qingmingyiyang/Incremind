import json
from pathlib import Path

import av
import numpy as np
import pytest

from tests.memory_app.v2.test_workbench_remember import env, post, wait
from backend.memory_app.workspace_audio import build_rebuild_object_store
from backend.api.tokenhub_asr_provider import tokenhub_egress_manifest
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.security.secrets import InMemorySecretStore
from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF, SaveCloudAsrProviderSettings


def configure_cloud(env, monkeypatch, offset_ms=0):
    store, _ = build_rebuild_object_store(env.root)
    SaveCloudAsrProviderSettings(store, now='2026-10-01T00:00:00Z').execute(enabled=True, confirm_enable=True)
    secrets = InMemorySecretStore({TOKENHUB_ASR_SECRET_REF: 'test-secret'})
    monkeypatch.setattr('backend.api.tokenhub_asr_provider.build_secret_store', lambda root: secrets)
    manifest = tokenhub_egress_manifest(env.root)
    ProviderEgressPolicyStore(env.root).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    env.model.public = lambda: {'generation': {'base_url': 'http://localhost/v1', 'allow_remote': True}}
    response = json.loads((Path(__file__).parents[2] / 'fixtures' / 'tokenhub_hy_asr_sync_completed.json').read_text())['response']
    calls = []
    def wire(url, headers, body, timeout):
        calls.append(len(body))
        result = json.loads(json.dumps(response))
        result['output']['text'] = f"原文证据分段{len(calls)}"
        for sentence in result['output']['sentences']:
            sentence['text'] = result['output']['text']
            sentence['begin_ms'] += offset_ms
            sentence['end_ms'] += offset_ms
        return 200, result
    monkeypatch.setattr('backend.api.tokenhub_asr_provider._http_call', wire)
    return store, calls


@pytest.mark.parametrize('retry', [False, True])
def test_local_video_uses_real_audio_derivative_and_governed_fake_asr(env, monkeypatch, retry):
    store, calls = configure_cloud(env, monkeypatch)
    path = env.root / 'original.mp4'
    with av.open(str(path), 'w') as output:
        video = output.add_stream('mpeg4', rate=1)
        video.width = video.height = 16
        video.pix_fmt = 'yuv420p'
        stream = output.add_stream('aac', rate=16000)
        stream.layout = 'mono'
        for packet in video.encode(av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format='rgb24')):
            output.mux(packet)
        for packet in video.encode(None):
            output.mux(packet)
        frame = av.AudioFrame.from_ndarray(np.zeros((1, 16000), dtype=np.float32), format='flt', layout='mono')
        frame.sample_rate = 16000
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    original = path.read_bytes()
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('original.mp4', original, 'video/mp4')})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()['input_kind'] == 'video'
    item_id = uploaded.json()['id']
    env.model.fail = retry
    result = post(env, text='', item_id=item_id)
    receipt = wait(env, result)['receipt']['remember']
    if retry:
        assert receipt['state'] == 'failed'
        assert len(calls) == 1
        env.model.fail = False
        response = env.http.post(f"/api/v2/workbench/turns/{result['turn']['id']}/retry", json={'project_id': 'alpha'})
        assert response.status_code == 200
        receipt = wait(env, result)['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', item_id).payload
    assert item['status'] == 'confirmed' and item['source_text']
    assert item['audio_transcription']['original_identity']['size'] == len(original)
    assert len(calls) == 1
    assert env.domains.confirmations.source_store.read('sources', 'source-' + item_id)['type'] == 'video'
    downloaded = env.http.get(f'/api/workspace/v1/items/{item_id}/original?project_id=alpha')
    assert downloaded.status_code == 200 and downloaded.content == original
    assert all(insight['state'] == 'pending' for insight in receipt['insights'])
    assert env.records.list('recognitions') == ()


def test_video_capacity_is_two_gib_and_corrupt_video_fails_safely(env):
    from backend.memory_app.v2.intake_media import MAX_VIDEO_BYTES
    assert MAX_VIDEO_BYTES == 2 * 1024 * 1024 * 1024
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('broken.mp4', b'not a video', 'video/mp4')})
    assert uploaded.status_code == 200
    receipt = wait(env, post(env, text='', item_id=uploaded.json()['id']))['receipt']['remember']
    assert receipt['state'] == 'failed'
    assert receipt['error'] == 'processing_failed'
    assert env.model.calls == 0
    assert env.records.list('documents') == ()
