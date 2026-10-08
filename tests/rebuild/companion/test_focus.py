from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.companion_core import CompanionConflict, CompanionFocusService, CompanionRepository, CompanionStateReducer, normalize_process_name

ROOT = Path(__file__).resolve().parents[3]

class Clock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 7, 22, 4, 0, tzinfo=timezone.utc); self.mono = 100.0
    def now(self): return self.wall
    def monotonic(self): return self.mono
    def advance(self, seconds: float): self.wall += timedelta(seconds=seconds); self.mono += seconds

def service(tmp_path, clock):
    repository = CompanionRepository.at_data_root(tmp_path, now=clock.now)
    reducer = CompanionStateReducer(repository, rules_path=ROOT / "config/companion/economy-rules.json", now=clock.now)
    return CompanionFocusService(repository, reducer=reducer, monotonic=clock.monotonic, now=clock.now), reducer

def test_focus_uses_monotonic_time_and_explicit_pause_resume(tmp_path) -> None:
    clock=Clock(); focus,_=service(tmp_path,clock)
    item=focus.start(duration_minutes=5,supervision_enabled=True,work_processes=["Code.EXE"],distracting_processes=["steam.exe"])
    clock.advance(5); item=focus.observe(process_name="code.exe")
    assert item.elapsed_seconds == 5 and item.classification == "work"
    paused=focus.act(item.session_id,"pause",item.revision); clock.advance(30)
    assert focus.current().elapsed_seconds == paused.elapsed_seconds
    resumed=focus.act(item.session_id,"resume",focus.current().revision); clock.wall -= timedelta(hours=1); clock.mono += 4
    assert focus.observe(process_name="unknown-app.exe").elapsed_seconds == resumed.elapsed_seconds + 4

def test_two_distracting_samples_warn_with_cooldown_and_game_quiet(tmp_path) -> None:
    clock=Clock(); focus,_=service(tmp_path,clock)
    focus.start(duration_minutes=5,supervision_enabled=True,work_processes=[],distracting_processes=["steam.exe"])
    assert focus.observe(process_name="steam.exe").should_warn is False
    assert focus.observe(process_name="steam.exe").should_warn is True
    clock.advance(10); assert focus.observe(process_name="steam.exe").should_warn is False
    clock.advance(60); assert focus.observe(process_name="steam.exe",game_quiet=True).should_warn is False
    assert focus.observe(process_name="steam.exe").should_warn is True
    assert focus.observe(process_name="unlisted.exe").classification == "unknown"

def test_lock_pauses_and_restart_never_counts_downtime(tmp_path) -> None:
    clock=Clock(); focus,reducer=service(tmp_path,clock)
    started=focus.start(duration_minutes=5,supervision_enabled=False,work_processes=[],distracting_processes=[])
    clock.advance(7); paused=focus.observe(process_name="code.exe",locked=True)
    assert paused.status == "paused" and paused.elapsed_seconds == 7
    focus.act(started.session_id,"resume",paused.revision); clock.advance(100)
    restarted=CompanionFocusService(reducer.repository,reducer=reducer,monotonic=clock.monotonic,now=clock.now)
    recovered=restarted.current()
    assert recovered.status == "paused" and recovered.elapsed_seconds == 7

def test_completion_rewards_once_and_duplicate_active_session_is_rejected(tmp_path) -> None:
    clock=Clock(); focus,reducer=service(tmp_path,clock)
    started=focus.start(duration_minutes=5,supervision_enabled=False,work_processes=[],distracting_processes=[])
    with pytest.raises(CompanionConflict): focus.start(duration_minutes=5,supervision_enabled=False,work_processes=[],distracting_processes=[])
    latest=started
    for _ in range(30): clock.advance(10); latest=focus.observe(process_name="code.exe")
    assert latest.status == "completed" and latest.reward_state == "rewarded"
    assert reducer.snapshot().coins == 5
    assert focus.current().reward_state == "rewarded" and reducer.wallet(limit=10)[0].delta == 5

def test_process_normalization_never_exposes_paths_or_titles() -> None:
    assert normalize_process_name(r"C:\\Games\\Steam.EXE") == "steam.exe"
    assert normalize_process_name("Visual Studio Code - secrets.txt") == "unknown"

def test_limited_reward_is_terminal_and_never_retried_later(tmp_path) -> None:
    clock=Clock(); focus,reducer=service(tmp_path,clock); started=focus.start(duration_minutes=5,supervision_enabled=False,work_processes=[],distracting_processes=[])
    focus.act(started.session_id,"cancel",started.revision)
    with reducer.repository._transaction() as connection:
        connection.execute("UPDATE companion_focus_sessions SET status='completed',elapsed_seconds=target_seconds,reward_state='limited',revision=revision+1 WHERE session_id=?",(started.session_id,))
    class RejectRetry:
        def apply(self,**_): raise AssertionError("limited reward must not retry")
    focus.reducer=RejectRetry()
    assert focus.current().reward_state=="limited"
