from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from backend.api.governed_staged_video import GovernedStagedVideoError, GovernedStagedVideoRunner, _run_governed_ffmpeg


def _video(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "staging"
    video = root / "jobs" / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"mp4")
    return root, video


def _success(argv, _environment, _timeout, control):
    if control is not None:
        control()
    audio = Path(argv[argv.index("pcm_s16le") + 1])
    pattern = Path(argv[-1])
    audio.write_bytes(b"wav")
    for number in range(1, 4):
        Path(str(pattern).replace("%03d", f"{number:03d}")).write_bytes(f"frame-{number}".encode())
    return subprocess.CompletedProcess(argv, 0, "private stdout", "private stderr")


def test_fixed_recipe_derives_ordered_staging_only_wav_and_frames(tmp_path: Path) -> None:
    root, video = _video(tmp_path)
    calls = []
    def runner(argv, environment, timeout, control):
        calls.append((tuple(argv), dict(environment), timeout))
        return _success(argv, environment, timeout, control)
    ticks = iter((0.0, 0.25))
    outcome = GovernedStagedVideoRunner(root, command_runner=runner, monotonic=lambda: next(ticks)).derive(
        video, job_id="media_hands:xhs:video", media_type="video/mp4", remaining_wall_ms=1_000,
        remaining_media_cpu_ms=500,
    )
    argv, environment, timeout = calls[0]
    assert argv[0] == "ffmpeg" and argv[1:6] == ("-hide_banner", "-loglevel", "error", "-nostdin", "-y")
    assert "-ac" in argv and argv[argv.index("-ac") + 1] == "1"
    assert "-ar" in argv and argv[argv.index("-ar") + 1] == "16000"
    assert "-vf" in argv and argv[argv.index("-vf") + 1] == "fps=1/5"
    assert "-frames:v" in argv and argv[argv.index("-frames:v") + 1] == "12"
    assert environment == {} and timeout == 0.5
    assert outcome.audio.media_type == "audio/wav" and outcome.audio_bytes == 3
    assert [item.ordinal for item in outcome.frames] == [0, 1, 2]
    assert outcome.frame_bytes == len(b"frame-1frame-2frame-3") and outcome.wall_ms == 250
    assert all(Path(item.staged_path).is_relative_to(root.resolve()) for item in (outcome.audio, *outcome.frames))


def test_rejects_model_controlled_type_outside_symlink_and_budget_before_process(tmp_path: Path) -> None:
    root, video = _video(tmp_path)
    calls = []
    runner = GovernedStagedVideoRunner(root, command_runner=lambda *args: calls.append(args))
    with pytest.raises(GovernedStagedVideoError, match="media_type_unsupported"):
        runner.derive(video, job_id="job", media_type="image/jpeg", remaining_wall_ms=1000)
    outside = tmp_path / "outside.mp4"; outside.write_bytes(b"mp4")
    with pytest.raises(GovernedStagedVideoError, match="path_not_governed"):
        runner.derive(outside, job_id="job", media_type="video/mp4", remaining_wall_ms=1000)
    with pytest.raises(GovernedStagedVideoError, match="budget_exhausted"):
        runner.derive(video, job_id="job", media_type="video/mp4", remaining_wall_ms=0)
    assert calls == []


def test_timeout_cancel_failure_and_output_budget_are_stable_and_cleanup(tmp_path: Path) -> None:
    root, video = _video(tmp_path)
    timed_out = GovernedStagedVideoRunner(root, command_runner=lambda argv, env, timeout, control: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, timeout)))
    with pytest.raises(GovernedStagedVideoError, match="^video_derivative_timed_out$"):
        timed_out.derive(video, job_id="job", media_type="video/mp4", remaining_wall_ms=1000)
    failed = GovernedStagedVideoRunner(root, command_runner=lambda argv, env, timeout, control: subprocess.CompletedProcess(argv, 2, str(tmp_path), str(tmp_path)))
    with pytest.raises(GovernedStagedVideoError, match="^video_derivative_failed$") as error:
        failed.derive(video, job_id="job", media_type="video/mp4", remaining_wall_ms=1000)
    assert str(tmp_path) not in str(error.value)
    cancelled = GovernedStagedVideoRunner(root, command_runner=_success)
    with pytest.raises(GovernedStagedVideoError, match="interrupted"):
        cancelled.derive(video, job_id="job", media_type="video/mp4", remaining_wall_ms=1000, control_check=lambda: (_ for _ in ()).throw(RuntimeError("revoked")))
    excessive = GovernedStagedVideoRunner(root, max_output_bytes=4, command_runner=_success)
    with pytest.raises(GovernedStagedVideoError, match="output_budget_exhausted"):
        excessive.derive(video, job_id="over", media_type="video/mp4", remaining_wall_ms=1000)


def test_noncanonical_frame_name_is_not_accepted(tmp_path: Path) -> None:
    root, video = _video(tmp_path)
    def noncanonical(argv, environment, timeout, control):
        audio = Path(argv[argv.index("pcm_s16le") + 1]); audio.write_bytes(b"wav")
        Path(argv[-1]).parent.joinpath("frame-unexpected.jpg").write_bytes(b"frame")
        return subprocess.CompletedProcess(argv, 0, "", "")
    with pytest.raises(GovernedStagedVideoError, match="frames_invalid"):
        GovernedStagedVideoRunner(root, command_runner=noncanonical).derive(
            video, job_id="job", media_type="video/mp4", remaining_wall_ms=1000
        )


def test_default_process_uses_environment_allowlist_and_terminates_on_control(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    class Process:
        returncode = None
        terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
        def wait(self, timeout): self.returncode = -15
    process = Process()
    def popen(_argv, **kwargs):
        captured.update(kwargs["env"]); return process
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(GovernedStagedVideoError, match="interrupted"):
        _run_governed_ffmpeg(("ffmpeg",), {}, 1.0, lambda: (_ for _ in ()).throw(GovernedStagedVideoError("video_derivative_interrupted")))
    assert process.terminated and "OPENAI_API_KEY" not in captured and "PATH" in captured
