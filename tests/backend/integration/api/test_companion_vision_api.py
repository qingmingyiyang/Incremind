from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.desktop_session import DESKTOP_SESSION_HEADER
from backend.api.routes import companion as companion_routes
from backend.security.file_grant import DesktopFileGrant, sign_desktop_file_grant
from core.companion_core import vision as vision_module


SECRET="s"*43;INSTANCE="instance-companion-vision"

def test_sidecar_startup_sweeps_owned_vision_grants_without_using_vision_api(tmp_path, monkeypatch):
    monkeypatch.setattr(vision_module.tempfile, "gettempdir", lambda: str(tmp_path / "system-temp"))
    parent=tmp_path/"system-temp"/"chriptmas-companion-vision";old=parent/("a"*24);old.mkdir(parents=True)
    owned=old/("vision-grant-"+"b"*48+".jpg");owned.write_bytes(b"secret pixels")
    unrelated=old/"keep.txt";unrelated.write_text("keep",encoding="utf-8")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path/"vault"))) as client:
        assert client.get("/api/health").status_code in {200,404}
        assert not hasattr(client.app.state,"companion_vision_grant_store")
    assert not owned.exists() and unrelated.exists()

def configure(monkeypatch, instance=INSTANCE):
    values={"CHRIPTMAS_DESKTOP_SESSION_MODE":"desktop_production","CHRIPTMAS_DESKTOP_SESSION_SECRET":SECRET,"CHRIPTMAS_DESKTOP_INSTANCE_ID":instance,"CHRIPTMAS_DESKTOP_NONCE":"n"*43,"CHRIPTMAS_DESKTOP_PROTOCOL_VERSION":"desktop-loopback/1","CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT":(datetime.now(UTC)+timedelta(hours=1)).isoformat(),"CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN":"http://127.0.0.1:8317"}
    for key,value in values.items():monkeypatch.setenv(key,value)

def headers(content,*,media_type="image/jpeg",source_kind="image",sha256=None,instance=INSTANCE):
    grant=DesktopFileGrant(grant_id="file-grant-"+"a"*43,session_instance_id=instance,display_name="screen.jpg",media_type=media_type,source_kind=source_kind,size_bytes=len(content),sha256=sha256 or hashlib.sha256(content).hexdigest(),expires_at_ms=int((datetime.now(UTC)+timedelta(minutes=1)).timestamp()*1000))
    return{DESKTOP_SESSION_HEADER:SECRET,"X-Chriptmas-File-Grant":grant.grant_id,"X-Chriptmas-File-Session":grant.session_instance_id,"X-Chriptmas-File-Name":base64.urlsafe_b64encode(grant.display_name.encode()).decode().rstrip("="),"X-Chriptmas-File-Media-Type":grant.media_type,"X-Chriptmas-File-Source-Kind":grant.source_kind,"X-Chriptmas-File-Size":str(grant.size_bytes),"X-Chriptmas-File-Sha256":grant.sha256,"X-Chriptmas-File-Expires":str(grant.expires_at_ms),"X-Chriptmas-File-Signature":sign_desktop_file_grant(grant,session_secret=SECRET),"Content-Type":"application/octet-stream"}

def test_screen_grant_is_streamed_once_analyzed_and_removed(tmp_path,monkeypatch):
    configure(monkeypatch);content=b"\xff\xd8\xff"+b"x"*100;app=create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        uploaded=client.post("/api/rebuild/companion/vision/grants",headers=headers(content),content=content)
        assert uploaded.status_code==201;grant_id=uploaded.json()["grant"]["grant_id"]
        stored=app.state.companion_vision_grant_store.root;assert len(list(stored.iterdir()))==1
        analyzed=client.post("/api/rebuild/companion/vision/analyze",headers={DESKTOP_SESSION_HEADER:SECRET},json={"request_id":"vision:api","grant_id":grant_id,"question":"请描述","confirm_egress":True})
        replay=client.post("/api/rebuild/companion/vision/analyze",headers={DESKTOP_SESSION_HEADER:SECRET},json={"request_id":"vision:api","grant_id":grant_id,"question":"请描述","confirm_egress":True})
    assert analyzed.status_code==200 and analyzed.json()["result"]["source"]=="local"
    assert replay.status_code==200 and replay.json()["result"]==analyzed.json()["result"] and list(stored.iterdir())==[]
    assert not (tmp_path/"library"/"assets").exists()


def test_vision_analyze_compatibility_adapter_uses_turn_approval_and_never_calls_legacy_service(tmp_path,monkeypatch):
    configure(monkeypatch)
    class Runtime:
        def __init__(self): self.submitted=[];self.actions=[];self.composition_metadata={"companion_vision_remote_usable":False}
        def submit_turn(self, payload):
            self.submitted.append(payload);return SimpleNamespace(turn_id="turn-"+"a"*32,status="waiting_approval",current_sequence=6,replayed=False)
        def events_after(self, _turn_id): return ({"type":"approval.required","event_id":"event-approval","sequence":6},)
        def apply_action(self, action): self.actions.append(action);return SimpleNamespace(turn_id=action["turn_id"],status="completed",current_sequence=10,replayed=False)
        def presentation_for(self, _turn_id): return {"request_id":"vision:adapter","text":"本地回答","provider_id":"local-fallback","model_name":"","provider_call_performed":False,"replayed":False}
    runtime=Runtime()
    monkeypatch.setattr(companion_routes,"get_or_build_ai_runtime",lambda *_args:runtime)
    assert not hasattr(companion_routes,"build_companion_vision_service")
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response=client.post("/api/rebuild/companion/vision/analyze",headers={DESKTOP_SESSION_HEADER:SECRET},json={"request_id":"vision:adapter","grant_id":"vision-grant-"+"a"*48,"question":"请描述","confirm_egress":True,"project_id":"project-alpha"})
    assert response.status_code==200 and response.json()["result"]["source"]=="local"
    assert runtime.submitted[0]["desired_outcome"]=="companion.vision.analyze"
    assert runtime.submitted[0]["scope"]=={"kind":"project","project_id":"project-alpha","series_id":None}
    assert runtime.submitted[0]["privacy"]["allow_remote"] is False
    assert runtime.actions[0]["type"]=="approve" and runtime.actions[0]["target_event_id"]=="event-approval"

def test_screen_grant_rejects_bad_hash_type_size_and_missing_confirmation(tmp_path,monkeypatch):
    configure(monkeypatch);content=b"\xff\xd8\xfffixture"
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        assert client.post("/api/rebuild/companion/vision/grants",headers=headers(content,source_kind="video"),content=content).status_code==413
        assert client.post("/api/rebuild/companion/vision/grants",headers=headers(content,sha256="0"*64),content=content).status_code==400
        valid=client.post("/api/rebuild/companion/vision/grants",headers=headers(content),content=content).json()["grant"]["grant_id"]
        rejected=client.post("/api/rebuild/companion/vision/analyze",headers={DESKTOP_SESSION_HEADER:SECRET},json={"request_id":"vision:no","grant_id":valid,"question":"看", "confirm_egress":False})
        revoked=client.delete(f"/api/rebuild/companion/vision/grants/{valid}",headers={DESKTOP_SESSION_HEADER:SECRET})
    assert rejected.status_code==400 and revoked.json()["revoked"] is True

def test_desktop_session_change_disposes_old_store_and_cannot_consume_old_grant(tmp_path,monkeypatch):
    configure(monkeypatch);content=b"\xff\xd8\xffsession";app=create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        old=client.post("/api/rebuild/companion/vision/grants",headers=headers(content),content=content).json()["grant"]["grant_id"]
        old_root=app.state.companion_vision_grant_store.root;assert len(list(old_root.iterdir()))==1
        second="instance-companion-vision-b";configure(monkeypatch,second)
        fresh=client.post("/api/rebuild/companion/vision/grants",headers=headers(content,instance=second),content=content)
        assert fresh.status_code==201 and app.state.companion_vision_grant_store.session_id==second
        replay=client.post("/api/rebuild/companion/vision/analyze",headers={DESKTOP_SESSION_HEADER:SECRET},json={"request_id":"vision:old","grant_id":old,"question":"看", "confirm_egress":True})
    assert replay.status_code==409 and (not old_root.exists() or list(old_root.iterdir())==[])
