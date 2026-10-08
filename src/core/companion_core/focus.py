from __future__ import annotations

import ctypes
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import psutil

from .errors import CompanionConflict, CompanionIntegrityError, CompanionRepositoryError
from .repository import CompanionRepository
from .state import CompanionStateReducer

_PROCESS = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


@dataclass(frozen=True, slots=True)
class FocusSnapshot:
    session_id: str; status: str; target_seconds: int; elapsed_seconds: int
    supervision_enabled: bool; work_processes: tuple[str, ...]; distracting_processes: tuple[str, ...]
    warning_count: int; last_warning_at: str | None; reward_state: str; revision: int; updated_at: str
    classification: str = "unknown"; should_warn: bool = False


class CompanionFocusService:
    def __init__(self, repository: CompanionRepository, *, reducer: CompanionStateReducer, monotonic: Callable[[], float] = time.monotonic, now: Callable[[], datetime] | None = None) -> None:
        self.repository, self.reducer, self.monotonic = repository, reducer, monotonic
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._anchor: dict[str, float] = {}; self._streak: dict[str, int] = {}

    def current(self) -> FocusSnapshot | None:
        self.repository.initialize()
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_focus_sessions ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None: return None
            if row["status"] == "running" and row["session_id"] not in self._anchor:
                connection.execute("UPDATE companion_focus_sessions SET status='paused', revision=revision+1, updated_at=? WHERE session_id=?", (self._now(), row["session_id"]))
                row = connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?", (row["session_id"],)).fetchone()
            snapshot = _snapshot(self._advance(connection, row))
        return self._reward_if_complete(snapshot)

    def start(self, *, duration_minutes: object, supervision_enabled: object, work_processes: object, distracting_processes: object) -> FocusSnapshot:
        if not isinstance(duration_minutes, int) or isinstance(duration_minutes, bool) or not 5 <= duration_minutes <= 240: raise CompanionRepositoryError("focus duration is invalid")
        if not isinstance(supervision_enabled, bool): raise CompanionRepositoryError("focus supervision flag is invalid")
        work, distracting = _processes(work_processes), _processes(distracting_processes)
        if set(work) & set(distracting): raise CompanionRepositoryError("focus process categories overlap")
        self.repository.initialize(); now = self._now(); session_id = "focus_" + uuid.uuid4().hex
        with self.repository._transaction() as connection:
            active = connection.execute("SELECT 1 FROM companion_focus_sessions WHERE status IN ('running','paused')").fetchone()
            if active: raise CompanionConflict("a focus session is already active")
            connection.execute("INSERT INTO companion_focus_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (session_id,"running",duration_minutes*60,0.0,int(supervision_enabled),json.dumps(work),json.dumps(distracting),0,None,"none",1,now,now))
            row = connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?", (session_id,)).fetchone()
        self._anchor[session_id] = self.monotonic(); self._streak[session_id] = 0
        return _snapshot(row)

    def act(self, session_id: object, action: object, expected_revision: object) -> FocusSnapshot:
        if not isinstance(session_id, str) or not session_id.startswith("focus_") or action not in {"pause","resume","cancel"} or not isinstance(expected_revision, int): raise CompanionRepositoryError("focus action is invalid")
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None: raise CompanionRepositoryError("focus session was not found")
            if int(row["revision"]) != expected_revision: raise CompanionConflict("focus session revision conflict")
            row = self._advance(connection, row)
            current = str(row["status"]); targets = {"pause":("running","paused"),"resume":("paused","running"),"cancel":(("running","paused"),"cancelled")}
            allowed, target = targets[action]
            if current not in ((allowed,) if isinstance(allowed,str) else allowed): raise CompanionConflict("focus action is not allowed in current state")
            connection.execute("UPDATE companion_focus_sessions SET status=?, revision=revision+1, updated_at=? WHERE session_id=?", (target,self._now(),session_id))
            saved = connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?", (session_id,)).fetchone()
        if target == "running": self._anchor[session_id] = self.monotonic()
        else: self._anchor.pop(session_id, None)
        return _snapshot(saved)

    def observe(self, *, process_name: object, locked: object = False, sleeping: object = False, game_quiet: object = False) -> FocusSnapshot:
        name = normalize_process_name(process_name)
        if not all(isinstance(x, bool) for x in (locked,sleeping,game_quiet)): raise CompanionRepositoryError("focus observation flags are invalid")
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_focus_sessions WHERE status IN ('running','paused') ORDER BY created_at DESC LIMIT 1").fetchone()
            if row is None: raise CompanionRepositoryError("active focus session was not found")
            row = self._advance(connection,row); session_id = str(row["session_id"])
            if (locked or sleeping) and row["status"] == "running":
                connection.execute("UPDATE companion_focus_sessions SET status='paused',revision=revision+1,updated_at=? WHERE session_id=?",(self._now(),session_id)); self._anchor.pop(session_id,None); row=connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?",(session_id,)).fetchone()
            work=set(json.loads(row["work_processes_json"])); distracting=set(json.loads(row["distracting_processes_json"])); classification="work" if name in work else "distracting" if name in distracting else "unknown"
            streak=self._streak.get(session_id,0)+1 if classification=="distracting" else 0; self._streak[session_id]=streak
            should_warn=False
            if row["status"]=="running" and bool(row["supervision_enabled"]) and not game_quiet and streak>=2:
                previous=datetime.fromisoformat(row["last_warning_at"]) if row["last_warning_at"] else None; now_dt=self.now()
                if previous is None or (now_dt-previous).total_seconds()>=60:
                    should_warn=True; connection.execute("UPDATE companion_focus_sessions SET warning_count=warning_count+1,last_warning_at=?,revision=revision+1,updated_at=? WHERE session_id=?",(now_dt.isoformat(),now_dt.isoformat(),session_id)); row=connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?",(session_id,)).fetchone()
            snapshot=_snapshot(row,classification=classification,should_warn=should_warn)
        return self._reward_if_complete(snapshot)

    def _advance(self, connection, row):
        if row["status"] != "running": return row
        session_id=str(row["session_id"]); current=self.monotonic(); anchor=self._anchor.get(session_id)
        if anchor is None: return row
        delta=max(0.0,min(10.0,current-anchor)); self._anchor[session_id]=current
        if delta < 0.001: return row
        elapsed=min(float(row["target_seconds"]),float(row["elapsed_seconds"])+delta); completed=elapsed>=float(row["target_seconds"])
        connection.execute("UPDATE companion_focus_sessions SET elapsed_seconds=?,status=?,reward_state=?,revision=revision+1,updated_at=? WHERE session_id=?",(elapsed,"completed" if completed else "running","pending" if completed else row["reward_state"],self._now(),session_id))
        if completed: self._anchor.pop(session_id,None)
        return connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?",(session_id,)).fetchone()

    def _reward_if_complete(self, snapshot: FocusSnapshot) -> FocusSnapshot:
        if snapshot.status != "completed" or snapshot.reward_state != "pending": return snapshot
        state="rewarded"
        try: self.reducer.apply(command="focus_complete",idempotency_key=f"focus:{snapshot.session_id}:complete",subject_id=snapshot.session_id)
        except CompanionConflict: state="limited"
        with self.repository._transaction() as connection:
            connection.execute("UPDATE companion_focus_sessions SET reward_state=?,revision=revision+1,updated_at=? WHERE session_id=?",(state,self._now(),snapshot.session_id)); row=connection.execute("SELECT * FROM companion_focus_sessions WHERE session_id=?",(snapshot.session_id,)).fetchone()
        return _snapshot(row,classification=snapshot.classification,should_warn=snapshot.should_warn)

    def _now(self) -> str:
        value=self.now()
        if value.tzinfo is None or value.utcoffset()!=timezone.utc.utcoffset(value): raise CompanionRepositoryError("focus clock must use UTC")
        return value.isoformat()


def sample_foreground_process() -> str:
    if sys.platform != "win32": return "unknown"
    hwnd=ctypes.windll.user32.GetForegroundWindow()
    if not hwnd: return "unknown"
    pid=ctypes.c_ulong(); ctypes.windll.user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
    try: return normalize_process_name(psutil.Process(pid.value).name())
    except (psutil.NoSuchProcess,psutil.AccessDenied,psutil.Error): return "unknown"

def normalize_process_name(value: object) -> str:
    if not isinstance(value,str): return "unknown"
    name=value.strip().lower().rsplit("/",1)[-1].rsplit("\\",1)[-1]
    return name if _PROCESS.fullmatch(name) else "unknown"

def _processes(value: object) -> tuple[str,...]:
    if not isinstance(value,list) or len(value)>32: raise CompanionRepositoryError("focus process list is invalid")
    result=tuple(dict.fromkeys(normalize_process_name(x) for x in value))
    if "unknown" in result: raise CompanionRepositoryError("focus process name is invalid")
    return result

def _snapshot(row, *, classification="unknown", should_warn=False) -> FocusSnapshot:
    if row is None: raise CompanionIntegrityError("focus session row is missing")
    return FocusSnapshot(str(row["session_id"]),str(row["status"]),int(row["target_seconds"]),int(float(row["elapsed_seconds"])),bool(row["supervision_enabled"]),tuple(json.loads(row["work_processes_json"])),tuple(json.loads(row["distracting_processes_json"])),int(row["warning_count"]),row["last_warning_at"],str(row["reward_state"]),int(row["revision"]),str(row["updated_at"]),classification,should_warn)
