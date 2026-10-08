from __future__ import annotations

import hashlib

import pytest
from core.companion_core import vision as vision_module

from core.companion_core import (
    CompanionConflict,
    CompanionModelRouteError,
    CompanionModelRouter,
    CompanionRepository,
    CompanionVisionGrantStore,
    CompanionVisionService,
    compose_companion_prompt,
)


def jpeg(size: int = 64) -> bytes:
    return b"\xff\xd8\xff" + b"\0" * (size - 3)


class Router:
    def __init__(self): self.calls = []
    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "completed", "source": "provider", "text": "屏幕里是测试内容。", "trace": {"usage": {"input_tokens": 1, "output_tokens": 2, "cost_usd": 0.0}}}


def test_grant_is_single_use_integrity_checked_and_deleted(tmp_path):
    store = CompanionVisionGrantStore(session_id="session", root=tmp_path / "grants")
    data = jpeg(); grant = store.issue(media_type="image/jpeg", data=data, expected_sha256=hashlib.sha256(data).hexdigest())
    assert set(grant.public()) == {"grant_id", "media_type", "byte_length", "sha256"}
    assert grant.path.exists()
    consumed, payload = store.consume(grant.grant_id)
    assert consumed.sha256 == grant.sha256 and payload == data and not grant.path.exists()
    with pytest.raises(CompanionConflict, match="already consumed"):
        store.consume(grant.grant_id)


def test_inspect_is_zero_consume_and_never_reads_pixels(tmp_path, monkeypatch):
    store = CompanionVisionGrantStore(session_id="session", root=tmp_path / "grants")
    data = jpeg(); grant = store.issue(media_type="image/jpeg", data=data, expected_sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(type(grant.path), "read_bytes", lambda *_: pytest.fail("inspect must not read pixels"))
    assert store.inspect(grant.grant_id) == grant.public()
    assert grant.path.exists()


def test_consume_expected_rejects_metadata_drift_single_use_and_deletes_file(tmp_path):
    store = CompanionVisionGrantStore(session_id="session", root=tmp_path / "grants")
    data = jpeg(); grant = store.issue(media_type="image/jpeg", data=data, expected_sha256=hashlib.sha256(data).hexdigest())
    expected = store.inspect(grant.grant_id)
    expected["sha256"] = "0" * 64
    with pytest.raises(CompanionConflict, match="metadata changed"):
        store.consume_expected(grant.grant_id, expected)
    assert not grant.path.exists()
    with pytest.raises(CompanionConflict, match="already consumed"):
        store.consume_expected(grant.grant_id, grant.public())

    intact = store.issue(media_type="image/jpeg", data=data, expected_sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(CompanionConflict, match="vision grant changed"):
        intact.path.write_bytes(jpeg(65))
        store.consume_expected(intact.grant_id, store.inspect(intact.grant_id))
    assert not intact.path.exists()


def test_expired_changed_and_unknown_temp_files_fail_closed(tmp_path):
    clock = [100.0]; root = tmp_path / "grants"; root.mkdir(); (root / ("vision-grant-"+"a"*48+".jpg")).write_bytes(jpeg()); (root / "unrelated.jpg").write_bytes(jpeg())
    store = CompanionVisionGrantStore(session_id="session", root=root, now=lambda: clock[0])
    assert [item.name for item in root.iterdir()] == ["unrelated.jpg"]
    data = jpeg(); grant = store.issue(media_type="image/jpeg", data=data, expected_sha256=hashlib.sha256(data).hexdigest())
    clock[0] += 121
    with pytest.raises(CompanionConflict, match="expired"):
        store.consume(grant.grant_id)
    assert not grant.path.exists()


def test_startup_sweeps_owned_files_from_old_sessions_without_following_links(tmp_path, monkeypatch):
    monkeypatch.setattr(vision_module.tempfile, "gettempdir", lambda: str(tmp_path))
    parent=tmp_path/"chriptmas-companion-vision";old=parent/("a"*24);old.mkdir(parents=True)
    owned=old/("vision-grant-"+"b"*48+".jpg");owned.write_bytes(jpeg());unrelated=old/"keep.txt";unrelated.write_text("keep")
    link=old/("vision-grant-"+"c"*48+".jpg")
    try: link.symlink_to(unrelated)
    except OSError: link=None
    current=CompanionVisionGrantStore(session_id="new-session")
    assert not owned.exists() and unrelated.exists()
    if link is not None: assert link.is_symlink()
    assert current.root.name != old.name


def test_service_consumes_image_without_persisting_pixels_or_paths(tmp_path):
    repository = CompanionRepository.at_data_root(tmp_path / "vault"); router = Router(); store = CompanionVisionGrantStore(session_id="session", root=tmp_path / "grants")
    data=jpeg();grant=store.issue(media_type="image/jpeg",data=data,expected_sha256=hashlib.sha256(data).hexdigest())
    service=CompanionVisionService(repository,model_router=router,character_prompt_loader=lambda:("友善回答。",2),grant_store=store)
    result=service.analyze(request_id="vision:test",grant_id=grant.grant_id,question="这是什么？",confirm_egress=True)
    assert result["text"] == "屏幕里是测试内容。" and not grant.path.exists()
    call=router.calls[0];assert call["image_payload"] == {"media_type":"image/jpeg","bytes":data}
    digest=hashlib.sha256(data).hexdigest();assert str(tmp_path) not in str(result) and "image_payload" not in str(result)
    assert digest not in str(router.calls[0]["prompt"].messages) and digest[:12] not in str(result)
    database=repository.database_path.read_bytes();assert digest.encode() not in database and digest[:12].encode() not in database


def test_model_router_requires_matching_payload_and_never_traces_pixels():
    data=jpeg();digest=hashlib.sha256(data).hexdigest();captured=[]
    class Provider:
        def generate(self,request):captured.append(request);return{"text":"ok"}
    router=CompanionModelRouter(provider=Provider(),provider_capabilities=("vision",),egress_consented=True,enabled_routes={"companion.vision":True})
    prompt=compose_companion_prompt(route_key="companion.vision",master_profile={},character_prompt="角色",modifiers={},published_context=(),short_term_messages=(),user_payload={"question":"看"},context_epoch=1)
    grant={"grant_id":"vision-grant-"+"a"*48,"sha256":digest,"media_type":"image/jpeg","byte_length":len(data)}
    result=router.execute(route_key="companion.vision",prompt=prompt,request_id="vision:test",image_grant=grant,image_payload={"media_type":"image/jpeg","bytes":data})
    assert result["text"] == "ok" and "image" not in str(result["trace"]).lower()
    assert captured[0]["image_payload"]["bytes"] == data
    with pytest.raises(CompanionModelRouteError,match="does not match"):
        router.execute(route_key="companion.vision",prompt=prompt,request_id="vision:bad",image_grant=grant,image_payload={"media_type":"image/jpeg","bytes":data+b"x"})
