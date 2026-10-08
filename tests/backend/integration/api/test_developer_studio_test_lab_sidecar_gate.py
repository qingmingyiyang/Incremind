"""Authenticated production SidecarSupervisor vertical Gate for Developer Studio Test Lab."""
from __future__ import annotations

import json
import os
from pathlib import Path
from shutil import copyfile
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import psutil

from tests.openai_transport_testlib import OpenAITransportFixture


ROOT = Path(__file__).resolve().parents[4]
PYTHON = ROOT / "runtime" / "python.exe"
NODE = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe"
if not NODE.exists():
    NODE = Path("node.exe")
GATE = ROOT / "tests" / "fixtures" / "developer_studio_test_lab_sidecar_gate.cjs"


def _remember_process(session: dict[str, object]) -> tuple[int, float]:
    pid = int(session["child_pid"])
    return pid, psutil.Process(pid).create_time()


def _kill_if_same_process(identity: tuple[int, float]) -> None:
    pid, created_at = identity
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created_at) >= 0.001:
            return
        descendants = process.children(recursive=True)
        for child in reversed(descendants):
            child.kill()
        process.kill()
        psutil.wait_procs([*descendants, process], timeout=5)
    except psutil.NoSuchProcess:
        return


def _wait_json(path: Path, process: subprocess.Popen[bytes], *, generation: int) -> dict[str, object]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("generation") == generation:
                return payload
        if process.poll() is not None:
            detail = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            pytest.fail(f"Test Lab sidecar fixture exited early: {process.returncode}: {detail}")
        time.sleep(0.05)
    pytest.fail(f"Test Lab sidecar fixture did not reach generation {generation}")


def _request(origin: str, method: str, path: str, *, session: str | None, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if session is not None:
        headers["X-Chriptmas-Desktop-Session"] = session
    request = Request(f"{origin}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _write_command(path: Path, action: str) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps({"action": action}), encoding="utf-8")
    pending.replace(path)


def _prompt(content: str, version: int) -> dict[str, object]:
    return {
        "id": "pt-input-understanding", "stageId": "input-understanding",
        "name": "输入理解", "description": "authenticated sidecar fixture",
        "content": content, "variables": [], "outputSchema": "{}",
        "modelProfileId": "mp-default", "version": version,
        "isProtected": False, "updatedAt": f"2026-08-26T08:00:0{version}+00:00",
    }


def _save_prompt(origin: str, secret: str, expected_revision: int, content: str, version: int) -> dict[str, object]:
    status, payload = _request(origin, "PUT", "/api/rebuild/developer-studio/config", session=secret, body={
        "expected_revision": expected_revision, "model_profiles": [],
        "prompts": [_prompt(content, version)], "skills": [],
        "workflow_steps": [], "snapshots": [],
    })
    assert status == 200
    return payload


def _seed_authorities_over_authenticated_http(origin: str, secret: str, base_url: str) -> dict[str, int]:
    provider_status, provider = _request(origin, "POST", "/api/providers", session=secret, body={
        "provider_id": "test-lab-route", "name": "test-lab-route",
        "llm_provider": "openai", "base_url": base_url,
        "api_path": "/chat/completions", "model": "test-lab-model",
        "models": ["test-lab-model"], "enabled": True,
    })
    assert provider_status == 201
    route_status, _ = _request(origin, "PUT", "/api/model-routes/intake.classification", session=secret, body={
        "provider_id": provider["provider_id"], "model_name": "test-lab-model",
        "adapter_kind": "openai-compatible", "enabled": True,
        "reason": "authenticated Test Lab Gate", "expected_registry_revision": 0,
    })
    assert route_status == 200
    shadow_status, shadow = _request(origin, "POST", "/api/model-route-runtime/preview", session=secret, body={})
    assert shadow_status == 200
    runtime_status, runtime = _request(origin, "POST", "/api/model-route-runtime/activate", session=secret, body={
        "shadow_token": shadow["shadow_token"], "expected_runtime_revision": 0, "confirm": True,
    })
    assert runtime_status == 200
    profile_status, profile = _request(origin, "GET", "/api/ai/model-routing-profile", session=secret)
    assert profile_status == 200
    update_status, _ = _request(origin, "PUT", "/api/ai/model-routing-profile", session=secret, body={
        "expected_revision": profile["revision"], "rules_version": 1,
        "text_default_tier": "standard", "confirm": True,
        "tier_routes": {"fast": None, "standard": "intake.classification", "deep": None, "vision": None, "image_generation": None},
    })
    assert update_status == 200
    _save_prompt(origin, secret, 0, "ACTIVE PROMPT V1", 1)
    preview_status, preview = _request(origin, "POST", "/api/rebuild/developer-studio/prompt-activation/preview", session=secret, body={
        "unit_id": "intake.classification", "expected_config_revision": 1, "expected_activation_revision": 0,
    })
    assert preview_status == 200
    active_status, _ = _request(origin, "POST", "/api/rebuild/developer-studio/prompt-activation/activate", session=secret, body={
        "unit_id": "intake.classification", "expected_config_revision": 1,
        "expected_activation_revision": 0, "preview_token": preview["preview_token"],
        "confirm": True, "reason": "authenticated Test Lab Gate",
    })
    assert active_status == 200
    draft = _save_prompt(origin, secret, 2, "DRAFT PROMPT V2", 2)
    unit = next(item for item in draft["prompt_activation_status"]["units"] if item["unit_id"] == "intake.classification")
    return {
        "runtime": int(runtime["runtime_revision"]), "config": int(draft["revision"]),
        "activation": int(draft["prompt_activation_status"]["activation_revision"]),
        "unit": int(unit["unit_revision"]),
    }


def _prompt_request(revisions: dict[str, int]) -> dict[str, object]:
    return {
        "test_type": "prompt", "input": "测试输入", "provider_call_confirmed": True,
        "route_key": "intake.classification", "expected_runtime_revision": revisions["runtime"],
        "prompt_id": "pt-input-understanding", "prompt_source": "active",
        "expected_config_revision": revisions["config"],
        "expected_activation_revision": revisions["activation"],
        "expected_prompt_unit_revision": revisions["unit"],
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows production SidecarSupervisor Gate")
def test_test_lab_authenticated_sidecar_restarts_without_replaying_provider(tmp_path: Path) -> None:
    fixture = OpenAITransportFixture(expected_model="test-lab-model", fault=None)
    fixture.start()
    process: subprocess.Popen[bytes] | None = None
    sidecars: list[tuple[int, float]] = []
    command_path = tmp_path / "sidecar-command.json"
    ready_path = tmp_path / "sidecar-ready.json"
    try:
        (tmp_path / "config").mkdir()
        copyfile(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
        process = subprocess.Popen(
            [str(NODE), str(GATE), str(ROOT), str(tmp_path), str(ready_path), str(command_path)],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        first = _wait_json(ready_path, process, generation=1)
        session = first["session"]
        assert isinstance(session, dict)
        sidecars.append(_remember_process(session))
        origin, secret = str(session["origin"]), str(session["secret"])

        denied_status, denied = _request(origin, "POST", "/api/rebuild/developer-studio/test-lab", session=None, body={})
        assert denied_status == 403 and denied == {"detail": "desktop_session_unauthorized"}
        wrong_status, _ = _request(origin, "POST", "/api/rebuild/developer-studio/test-lab", session="wrong-session", body={})
        assert wrong_status == 403

        revisions = _seed_authorities_over_authenticated_http(origin, secret, fixture.base_url)

        status, payload = _request(origin, "POST", "/api/rebuild/developer-studio/test-lab", session=secret, body=_prompt_request(revisions))
        assert status == 200
        assert payload["raw"] == "" and payload["parsed"] is None
        assert payload["resolved"]["route"]["model_name"] == "test-lab-model"
        assert payload["resolved"]["prompt"]["source"] == "active"
        turn_id = str(payload["turn_id"])
        assert len(fixture.requests) == 1
        assert fixture.requests[0] == {"model": "test-lab-model", "path": "/v1/chat/completions", "ordinal": 1, "has_authorization": False}

        _write_command(command_path, "restart")
        second = _wait_json(ready_path, process, generation=2)
        restarted = second["session"]
        assert isinstance(restarted, dict)
        sidecars.append(_remember_process(restarted))
        stale_status, _ = _request(str(restarted["origin"]), "GET", f"/api/ai/turns/{turn_id}/events", session=secret)
        assert stale_status == 403
        events_status, events = _request(str(restarted["origin"]), "GET", f"/api/ai/turns/{turn_id}/events", session=str(restarted["secret"]))
        assert events_status == 200
        assert events["turn_id"] == turn_id
        event_types = [event["type"] for event in events["events"]]
        assert event_types.count("model.attempt.dispatched") == 1
        assert event_types.count("model.attempt.terminal") == 1
        assert any(event["type"] == "turn.completed" for event in events["events"])
        assert len(fixture.requests) == 1

        _write_command(command_path, "stop")
        process.wait(timeout=20)
        assert process.returncode == 0
    finally:
        fixture.close()
        if process is not None and process.poll() is None:
            try:
                _write_command(command_path, "stop")
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=10)
        for identity in sidecars:
            _kill_if_same_process(identity)
