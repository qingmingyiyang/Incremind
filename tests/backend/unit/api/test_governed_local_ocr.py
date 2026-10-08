from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from backend.api.governed_local_ocr import GovernedLocalOcrError, GovernedLocalOcrRunner
from core.product_core.local_ocr_provider_settings import LocalOcrProviderSettings


def _settings(*, enabled: bool = True, command: tuple[str, ...] = ("fake-ocr", "{image_path}"), provider: str = "test-ocr") -> LocalOcrProviderSettings:
    return LocalOcrProviderSettings("ready", enabled, provider, command, "ready", True, False, "not_started")


def _image(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "staging"
    path = root / "job" / "image.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"png")
    return root, path


def test_default_settings_authority_is_disabled_fail_closed(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    class EmptyStore:
        def read(self, collection, record_id):
            assert (collection, record_id) == ("local_ocr_provider_settings", "default")
            return None
    with pytest.raises(GovernedLocalOcrError, match="^local_ocr_disabled$"):
        GovernedLocalOcrRunner(root, object_store=EmptyStore()).extract_text(
            image, media_type="image/png", remaining_wall_ms=1000
        )


def test_missing_command_is_rejected_before_process(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    called = []
    with pytest.raises(GovernedLocalOcrError, match="command_not_configured"):
        GovernedLocalOcrRunner(root, settings_reader=lambda: _settings(command=()), command_runner=lambda *args: called.append(args)).extract_text(
            image, media_type="image/png", remaining_wall_ms=1000
        )
    assert called == []


def test_custom_runner_receives_only_configured_command_and_bounded_timeout(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    calls = []
    def command(argv, env, timeout, control):
        calls.append((tuple(argv), dict(env), timeout, control))
        return subprocess.CompletedProcess(argv, 0, "  OCR  text ", "")
    ticks = iter((0.0, 0.25))
    result = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings(), command_runner=command, default_timeout_seconds=30, monotonic=lambda: next(ticks)).extract_text(
        image, media_type="image/png", remaining_wall_ms=500
    )
    assert calls[0][0] == ("fake-ocr", str(image.resolve())) and calls[0][1] == {}
    assert calls[0][2] == 0.5
    assert result.text == "OCR text" and result.provider_id == "governed-local-ocr"
    assert result.provider_revision.startswith("ocr-") and result.wall_ms == 250


def test_media_cpu_budget_tightens_process_deadline(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    calls = []
    runner = GovernedLocalOcrRunner(
        root,
        settings_reader=lambda: _settings(),
        command_runner=lambda argv, env, timeout, control: (
            calls.append(timeout) or subprocess.CompletedProcess(argv, 0, "text", "")
        ),
    )
    runner.extract_text(
        image,
        media_type="image/png",
        remaining_wall_ms=5_000,
        remaining_media_cpu_ms=250,
    )
    assert calls == [0.25]
    with pytest.raises(GovernedLocalOcrError, match="budget_exhausted"):
        runner.extract_text(
            image,
            media_type="image/png",
            remaining_wall_ms=5_000,
            remaining_media_cpu_ms=0,
        )


def test_default_process_environment_excludes_host_secrets(monkeypatch) -> None:
    from backend.api.governed_local_ocr import _run_governed_command

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
    result = _run_governed_command(("fake",), {}, 1.0, None)
    assert result.returncode == 0
    assert "OPENAI_API_KEY" not in captured and "PATH" in captured


def test_builtin_command_uses_existing_windows_semantics(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    calls = []
    def command(argv, env, timeout, control):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "built in", "")
    result = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings(command=("builtin:windows-ocr",), provider="builtin-windows-ocr"), command_runner=command).extract_text(
        image, media_type="image/png", remaining_wall_ms=1000
    )
    assert calls[0][0] == "powershell.exe" and "-ImagePath" in calls[0]
    assert result.provider_revision.startswith("ocr-")


def test_timeout_cancel_empty_error_and_path_leak_are_bounded(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    runner = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings())
    with pytest.raises(GovernedLocalOcrError, match="budget_exhausted"):
        runner.extract_text(image, media_type="image/png", remaining_wall_ms=0)
    with pytest.raises(GovernedLocalOcrError, match="local_ocr_interrupted") as interrupted:
        runner.extract_text(image, media_type="image/png", remaining_wall_ms=1000, control_check=lambda: (_ for _ in ()).throw(RuntimeError("revoked")))
    assert "revoked" not in str(interrupted.value)
    timed_out = GovernedLocalOcrRunner(
        root, settings_reader=lambda: _settings(),
        command_runner=lambda argv, env, timeout, control: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(argv, timeout)
        ),
    )
    with pytest.raises(GovernedLocalOcrError, match="^local_ocr_timed_out$"):
        timed_out.extract_text(image, media_type="image/png", remaining_wall_ms=1000)
    empty = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings(), command_runner=lambda argv, env, timeout, control: subprocess.CompletedProcess(argv, 0, " ", ""))
    with pytest.raises(GovernedLocalOcrError, match="^local_ocr_empty$"):
        empty.extract_text(image, media_type="image/png", remaining_wall_ms=1000)
    marker = str(tmp_path / "private-secret-path")
    failed = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings(), command_runner=lambda argv, env, timeout, control: subprocess.CompletedProcess(argv, 2, marker, marker))
    with pytest.raises(GovernedLocalOcrError, match="^local_ocr_failed$") as error:
        failed.extract_text(image, media_type="image/png", remaining_wall_ms=1000)
    assert marker not in str(error.value)


def test_non_raster_outside_symlink_and_wall_overrun_are_rejected(tmp_path: Path) -> None:
    root, image = _image(tmp_path)
    runner = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings())
    with pytest.raises(GovernedLocalOcrError, match="media_type_unsupported"):
        runner.extract_text(image, media_type="video/mp4", remaining_wall_ms=1000)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    with pytest.raises(GovernedLocalOcrError, match="path_not_governed"):
        runner.extract_text(outside, media_type="image/png", remaining_wall_ms=1000)
    def command(argv, env, timeout, control):
        return subprocess.CompletedProcess(argv, 0, "text", "")
    ticks = iter((0.0, 2.0))
    overrun = GovernedLocalOcrRunner(root, settings_reader=lambda: _settings(), command_runner=command, monotonic=lambda: next(ticks))
    with pytest.raises(GovernedLocalOcrError, match="budget_exhausted"):
        overrun.extract_text(image, media_type="image/png", remaining_wall_ms=1000)
