import wave

from tests.memory_app.v2.test_workbench_remember import env, post, wait
from tests.memory_app.v2.test_video_intake import configure_cloud


def test_long_audio_reuses_real_chunked_asr_and_keeps_complete_transcript(env, monkeypatch):
    store, calls = configure_cloud(env, monkeypatch, offset_ms=5000)
    path = env.root / 'long.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        for second in range(125):
            audio.writeframes(b'\x00\x00' * 8000)
    uploaded = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
        files={'file': ('long.wav', path.read_bytes(), 'audio/wav')})
    assert uploaded.status_code == 200, uploaded.text
    item_id = uploaded.json()['id']
    receipt = wait(env, post(env, text='', item_id=item_id))['receipt']['remember']
    assert receipt['state'] == 'done', receipt
    item = env.records.read('workspace_items', item_id).payload
    assert item['status'] == 'confirmed' and item['source_text']
    assert len(calls) == 3
    output = store.read('media_processing_outputs', item['audio_transcription']['output_id'])
    assert output['metadata']['chunk_count'] == 3
    assert item['source_text'] == output['text'].strip()
    assert len(output['segments']) >= 3
    assert output['segments'][-1]['start_seconds'] >= 110
    assert receipt['verified'] is False
    assert all(row['state'] == 'pending' for row in receipt['insights'])
    assert env.records.list('recognitions') == ()
