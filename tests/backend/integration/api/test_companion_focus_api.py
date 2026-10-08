from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.routes import companion as companion_route

ROOT=Path(__file__).parents[4]

def client_for(tmp_path): return TestClient(create_app(SimpleNamespace(root_dir=tmp_path/"vault",companion_mode="development",companion_repository_root=str(ROOT))))

def test_focus_api_start_observe_pause_and_privacy(tmp_path,monkeypatch) -> None:
    monkeypatch.setattr(companion_route,"sample_foreground_process",lambda:"steam.exe")
    client=client_for(tmp_path)
    started=client.post("/api/rebuild/companion/focus/start",json={"duration_minutes":5,"supervision_enabled":True,"work_processes":["code.exe"],"distracting_processes":["steam.exe"]})
    assert started.status_code==201
    first=client.post("/api/rebuild/companion/focus/observe",json={"locked":False,"sleeping":False,"game_quiet":False})
    second=client.post("/api/rebuild/companion/focus/observe",json={"locked":False,"sleeping":False,"game_quiet":False})
    assert first.json()["session"]["classification"]=="distracting"
    assert second.json()["session"]["should_warn"] is True
    assert "title" not in str(second.json()).lower()
    session=second.json()["session"]
    paused=client.post(f"/api/rebuild/companion/focus/{session['session_id']}/action",json={"action":"pause","expected_revision":session["revision"]})
    assert paused.status_code==200 and paused.json()["session"]["status"]=="paused"

def test_focus_api_rejects_client_process_and_unknown_fields(tmp_path) -> None:
    client=client_for(tmp_path)
    client.post("/api/rebuild/companion/focus/start",json={"duration_minutes":5,"supervision_enabled":False,"work_processes":[],"distracting_processes":[]})
    response=client.post("/api/rebuild/companion/focus/observe",json={"locked":False,"sleeping":False,"game_quiet":False,"process_name":"fake.exe"})
    assert response.status_code==400

def test_focus_api_only_allows_one_active_session(tmp_path) -> None:
    client=client_for(tmp_path); body={"duration_minutes":5,"supervision_enabled":False,"work_processes":[],"distracting_processes":[]}
    assert client.post("/api/rebuild/companion/focus/start",json=body).status_code==201
    assert client.post("/api/rebuild/companion/focus/start",json=body).status_code==409
