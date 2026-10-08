from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from backend.api.governed_local_asr import (
    FfprobeAudioDurationProbe,
    GovernedLocalAsrError,
    GovernedLocalAsrRunner,
    _run_fixed_command,
)
from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest


def test_default_process_environment_excludes_host_secrets(monkeypatch) -> None:
    from backend.api.governed_local_asr import _run_fixed_command

    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    class Process:
        returncode = 0
        def poll(self):
            return 0

    def popen(argv, **kwargs):
        captured.update(kwargs["env"])
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = _run_fixed_command(("fake",), {}, 1.0, None)
    assert result.returncode == 0
    assert "OPENAI_API_KEY" not in captured and "PATH" in captured


class Probe:
    def __init__(self, duration: int) -> None:
        self.duration = duration
        self.calls = []

    def duration_ms(self, path: Path, *, control_check=None) -> int:
        self.calls.append(path)
        return self.duration


def parts(tmp_path: Path):
    app = tmp_path / "app"
    staging = app / ".rebuild-data" / "media-hands"
    audio = staging / "job" / "audio.m4s"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    model = app / "data" / "models" / "faster-whisper" / "small"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model")
    (model / "config.json").write_text("{}", encoding="utf-8")
    manager = FasterWhisperModelManager(model.parent)
    write_downloaded_model_manifest(model, manager.download_spec("small"))
    return app, staging, audio


def transcript():
    return json.dumps({
        "language": "zh",
        "segments": [
            {"start_seconds": 0, "end_seconds": 1.2, "text": "第一句"},
            {"start_seconds": 1.2, "end_seconds": 2.5, "text": "第二句"},
        ],
    })


def test_runner_uses_fixed_local_cli_and_returns_stable_chunks(tmp_path: Path) -> None:
    app, staging, audio = parts(tmp_path)
    calls = []

    def command(argv, env, timeout, control):
        calls.append((tuple(argv), dict(env), timeout, control))
        return subprocess.CompletedProcess(argv, 0, transcript(), "")

    ticks = iter((0.0, 1.5))
    outcome = GovernedLocalAsrRunner(
        app, staging, "small", Probe(2500), command_runner=command,
        monotonic=lambda: next(ticks),
    ).transcribe(audio, title="测试视频", max_audio_ms=3000, max_wall_ms=5000)

    argv, env, timeout, _control = calls[0]
    assert argv[:3] == (
        str(Path(__import__("sys").executable)), "-m",
        "backend.video_summary.infrastructure.local_asr_cli",
    )
    assert argv[argv.index("--audio") + 1] == str(audio.resolve())
    assert argv[argv.index("--model") + 1] == "small"
    assert env == {"CHRIPTMAS_APP_ROOT": str(app.resolve())}
    assert timeout == 5.0
    assert outcome.audio_duration_ms == 2500 and outcome.wall_ms == 1500
    assert outcome.provider_revision == "local-faster-whisper-cli-r2:small:536b0662742c02347bc0e980a01041f333bce120"
    assert outcome.transcript["source"] == "local_asr" and outcome.chunks


def test_runner_rejects_path_model_and_duration_before_process(tmp_path: Path) -> None:
    app, staging, audio = parts(tmp_path)
    calls = []
    runner = lambda *args: calls.append(args)
    outside = tmp_path / "outside.m4s"
    outside.write_bytes(b"audio")
    with pytest.raises(GovernedLocalAsrError, match="outside_governed_staging"):
        GovernedLocalAsrRunner(app, staging, "small", Probe(1), runner).transcribe(
            outside, title="x", max_audio_ms=10, max_wall_ms=10
        )
    with pytest.raises(GovernedLocalAsrError, match="model_unavailable"):
        GovernedLocalAsrRunner(app, staging, "missing", Probe(1), runner).transcribe(
            audio, title="x", max_audio_ms=10, max_wall_ms=10
        )
    with pytest.raises(GovernedLocalAsrError, match="audio_budget_exceeded"):
        GovernedLocalAsrRunner(app, staging, "small", Probe(11), runner).transcribe(
            audio, title="x", max_audio_ms=10, max_wall_ms=10
        )
    assert calls == []


def test_runner_rejects_model_drift_during_child_execution(tmp_path: Path) -> None:
    app, staging, audio = parts(tmp_path)

    def command(argv, env, timeout, control):
        model = app / "data" / "models" / "faster-whisper" / "small" / "model.bin"
        model.write_bytes(b"drift-during-execution")
        return subprocess.CompletedProcess(argv, 0, transcript(), "")

    with pytest.raises(GovernedLocalAsrError, match="^local_asr_model_unavailable$"):
        GovernedLocalAsrRunner(app, staging, "small", Probe(2500), command).transcribe(
            audio, title="x", max_audio_ms=3000, max_wall_ms=5000,
        )


def test_runner_propagates_control_and_hides_process_output(tmp_path: Path) -> None:
    app, staging, audio = parts(tmp_path)

    class Cancelled(RuntimeError):
        pass

    def cancelled(*args):
        control = args[3]
        assert control is not None
        control()
        raise AssertionError("control must stop command")

    with pytest.raises(Cancelled, match="revoked"):
        GovernedLocalAsrRunner(app, staging, "small", Probe(1), cancelled).transcribe(
            audio, title="x", max_audio_ms=10, max_wall_ms=10,
            control_check=lambda: (_ for _ in ()).throw(Cancelled("revoked")),
        )

    def failed(argv, env, timeout, control):
        return subprocess.CompletedProcess(argv, 2, "private path", "secret diagnostic")

    with pytest.raises(GovernedLocalAsrError, match="^local_asr_failed$"):
        GovernedLocalAsrRunner(app, staging, "small", Probe(1), failed).transcribe(
            audio, title="x", max_audio_ms=10, max_wall_ms=10
        )


def test_ffprobe_duration_uses_fixed_command_and_bounded_output(tmp_path: Path) -> None:
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"exe")
    audio = tmp_path / "audio.m4s"
    audio.write_bytes(b"audio")
    calls = []

    def command(argv, env, timeout, control):
        calls.append((tuple(argv), dict(env), timeout, control))
        return subprocess.CompletedProcess(argv, 0, '{"format":{"duration":"2.501"}}', "")

    assert FfprobeAudioDurationProbe(ffprobe, command_runner=command).duration_ms(audio) == 2501
    assert calls[0][0] == (
        str(ffprobe.resolve()), "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(audio.resolve()),
    )
    assert calls[0][1] == {} and calls[0][2] == 30.0

    def failed(argv, env, timeout, control):
        return subprocess.CompletedProcess(argv, 2, "private", "secret")

    with pytest.raises(GovernedLocalAsrError, match="^ffprobe_failed$"):
        FfprobeAudioDurationProbe(ffprobe, command_runner=failed).duration_ms(audio)


def test_fixed_command_timeout_terminates_real_process_without_stderr_leak() -> None:
    marker = "private-timeout-stderr-marker"
    started = time.monotonic()
    with pytest.raises(GovernedLocalAsrError, match="^local_asr_timed_out$") as error:
        _run_fixed_command(
            (
                sys.executable,
                "-c",
                f"import sys, time; sys.stderr.write('{marker}'); sys.stderr.flush(); time.sleep(30)",
            ),
            {},
            0.2,
            None,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert marker not in str(error.value)


def test_runner_hides_real_nonzero_process_stderr(tmp_path: Path) -> None:
    app, staging, audio = parts(tmp_path)
    marker = "private-crash-stderr-marker"

    def crashing_command(argv, environment, timeout, control):
        return _run_fixed_command(
            (
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write('{marker}'); sys.exit(23)",
            ),
            environment,
            timeout,
            control,
        )

    with pytest.raises(GovernedLocalAsrError, match="^local_asr_failed$") as error:
        GovernedLocalAsrRunner(
            app, staging, "small", Probe(1), command_runner=crashing_command
        ).transcribe(audio, title="x", max_audio_ms=10, max_wall_ms=10_000)

    assert marker not in str(error.value)
