"""Run the governed builtin ASR protocol against the server's shared engine."""
import json
from pathlib import Path
import subprocess
import sys
import time

from backend.shared.server_resources import RESOURCE_POOL
from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
from backend.video_summary.infrastructure.faster_whisper_transcriber import FasterWhisperTranscriber


def is_builtin(argv):
    values = tuple(argv)
    return (len(values) == 11 and values[:4] == (sys.executable, '-m',
        'backend.video_summary.infrastructure.local_asr_cli', '--audio')
        and values[5] == '--model' and values[7:] == ('--mode', 'balanced', '--language', 'zh'))


def run_shared_asr(argv, user_root, timeout_seconds, control_check=None):
    pool = RESOURCE_POOL.get()
    if pool is None or not is_builtin(argv):
        raise ValueError('shared_asr_command_invalid')
    root = Path(user_root).resolve()
    audio = Path(argv[4]).resolve(strict=True)
    if (root.parent != (pool.server_root / 'users').resolve() or not audio.is_relative_to(root)
            or not audio.is_file() or timeout_seconds <= 0):
        raise ValueError('shared_asr_input_invalid')
    if control_check is not None:
        control_check()
    started = time.monotonic()
    def check():
        if control_check is not None:
            control_check()
        if time.monotonic() - started > timeout_seconds:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
    manager = FasterWhisperModelManager(pool.model_path('faster-whisper'))
    model = argv[6]
    if not manager.is_supported(model) or not manager.is_downloaded(model):
        return subprocess.CompletedProcess(argv, 2, '', 'local_asr_model_unavailable')
    transcript = FasterWhisperTranscriber(str(manager.resolve_model_dir(model)), device='auto',
        compute_type='int8', transcription_mode='balanced', language='zh').transcribe(
            audio, root / 'data/tmp/local-asr' / audio.stem,control_check=check)
    # In-process provider inference is synchronous. Keep its execution lease
    # until it really finishes; expired/cancelled work never publishes a result.
    if control_check is not None:
        control_check()
    if time.monotonic() - started > timeout_seconds:
        raise subprocess.TimeoutExpired(argv, timeout_seconds)
    if not manager.is_downloaded(model):
        return subprocess.CompletedProcess(argv, 2, '', 'local_asr_model_unavailable')
    payload = {'language': transcript.language, 'segments': [
        {'start_seconds': part.start_seconds, 'end_seconds': part.end_seconds, 'text': part.text}
        for part in transcript.segments]}
    return subprocess.CompletedProcess(argv, 0, json.dumps(payload, ensure_ascii=False), '')


def asset_runner(user_root):
    if RESOURCE_POOL.get() is None:
        return None
    return lambda argv, timeout: run_shared_asr(argv, user_root, timeout)
