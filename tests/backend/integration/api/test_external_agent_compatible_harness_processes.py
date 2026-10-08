from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx

from backend.api.external_agent_context_runtime import generate_external_agent_client_templates
from core.ai_kernel import (
    ContextEntry,
    ContextManifest,
    SQLiteAITurnStore,
    context_manifest_to_payload,
)
from core.ai_kernel.external_agent_client_adapters import install


ROOT = Path(__file__).resolve().parents[4]
PYTHON = ROOT / "runtime" / "python.exe"
HARNESS = ROOT / "tools" / "external_agent_compatible_harness.py"
PROJECT_ID = "project-compatible-harness"
NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc).isoformat()


def test_two_independent_compatible_harness_processes_share_bridge_and_restart_cursor(tmp_path) -> None:
    """Generated templates drive real HTTP processes; client B restarts from disk state."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copyfile(ROOT / "config" / "settings.toml", config_dir / "settings.toml")
    context_ref = _seed_turn(tmp_path)
    templates = generate_external_agent_client_templates(target_ids={
        "codex": "codex-test-target", "claude": "claude-test-target", "workbuddy": "workbuddy-test-target",
    })
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    codex_template = template_dir / "codex-instructions.md"
    claude_template = template_dir / "claude-instructions.md"
    # Use the exact production mutation path to produce the files that each
    # independent executable must consume.  The harness itself receives no
    # adapter profile object or template metadata through command-line flags.
    codex_template.write_text(
        install(templates["codex"], current_text="user codex rule\n", expected_current="user codex rule\n", confirm=True).next_text,
        encoding="utf-8",
    )
    claude_template.write_text(
        install(templates["claude"], current_text="user claude rule\n", expected_current="user claude rule\n", confirm=True).next_text,
        encoding="utf-8",
    )

    port = _unused_port()
    desktop_secret = "compatible-harness-desktop-secret-" + "x" * 48
    server_env = _server_environment(tmp_path, desktop_secret)
    server = subprocess.Popen(
        [str(PYTHON), str(HARNESS), "serve", "--port", str(port)],
        cwd=str(ROOT), env=server_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base_url, desktop_secret, server)
        codex_state = tmp_path / "states" / "codex.json"
        claude_state = tmp_path / "states" / "claude.json"

        # Process 1 starts Codex before the other client writes.  It exits
        # after persisting only the opaque Bridge session and cursor.
        codex_started = _client_process(
            base_url, desktop_secret, codex_template, codex_state, "start", "codex-initial",
            turn_id="turn-compatible-harness", project_id=PROJECT_ID,
        )
        assert codex_started["status"] == "started"

        # Process 2 independently reads the Claude template, starts, resolves
        # the map-scoped ref, and submits a proposal-only write over HTTP.
        claude_proposed = _client_process(
            base_url, desktop_secret, claude_template, claude_state, "propose", "claude-proposal",
            turn_id="turn-compatible-harness", project_id=PROJECT_ID, context_ref=context_ref,
        )
        assert claude_proposed["status"] == "proposed"
        assert claude_proposed["resolved_slice_count"] == 1
        assert claude_proposed["memory_publication_state"] == "not_published"

        # Process 3 is a fresh Codex executable.  It reads the state written
        # by process 1, observes the proposal cursor, and durable-acks it.
        first_resume = _client_process(
            base_url, desktop_secret, codex_template, codex_state, "observe-ack", "codex-resume",
            expected_change_type="memory.proposed",
        )
        assert first_resume["change_types"] == ["memory.proposed"]
        persisted = json.loads(codex_state.read_text(encoding="utf-8"))
        assert persisted["acknowledged_cursor"] == first_resume["acknowledged_cursor"]

        # Process 4 proves the cursor is client-durable, rather than process
        # memory: it reloads the same state and sees no duplicate event.
        second_resume = _client_process(
            base_url, desktop_secret, codex_template, codex_state, "observe-ack", "codex-restart",
        )
        assert second_resume["change_types"] == []
        assert second_resume["acknowledged_cursor"] == first_resume["acknowledged_cursor"]
    finally:
        _stop_server(server)


def _client_process(
    base_url: str, desktop_secret: str, template: Path, state: Path, action: str,
    operation_prefix: str, *, turn_id: str | None = None, project_id: str | None = None,
    context_ref: str | None = None, expected_change_type: str | None = None,
) -> dict[str, object]:
    command = [
        str(PYTHON), str(HARNESS), "client", "--base-url", base_url,
        "--desktop-session", desktop_secret, "--template", str(template),
        "--state", str(state), "--action", action, "--operation-prefix", operation_prefix,
    ]
    if turn_id is not None:
        command.extend(("--turn-id", turn_id))
    if project_id is not None:
        command.extend(("--project-id", project_id))
    if context_ref is not None:
        command.extend(("--context-ref", context_ref))
    if expected_change_type is not None:
        command.extend(("--expect-change-type", expected_change_type))
    completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = json.loads(completed.stdout)
    assert isinstance(result, dict)
    return result


def _server_environment(root: Path, desktop_secret: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "CHRIPTMAS_APP_ROOT": str(root),
        "CHRIPTMAS_DESKTOP_SESSION_MODE": "desktop_production",
        "CHRIPTMAS_DESKTOP_SESSION_SECRET": desktop_secret,
        "CHRIPTMAS_DESKTOP_INSTANCE_ID": "compatible-harness-instance",
        "CHRIPTMAS_DESKTOP_NONCE": "compatible-harness-nonce-" + "n" * 48,
        "CHRIPTMAS_DESKTOP_PROTOCOL_VERSION": "desktop-loopback/1",
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN": "http://127.0.0.1:8001",
    })
    return environment


def _wait_for_server(base_url: str, desktop_secret: str, process: subprocess.Popen[bytes], *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_response = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise AssertionError(f"harness server exited early: {stderr}")
        try:
            response = httpx.get(
                f"{base_url}/api/health",
                headers={"X-Chriptmas-Desktop-Session": desktop_secret}, timeout=1.0,
            )
            if response.status_code == 200:
                return
            last_response = f"HTTP {response.status_code}: {response.text[:300]}"
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"harness server did not become ready: {last_response}")


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _seed_turn(tmp_path: Path) -> str:
    store = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    turn_id = "turn-compatible-harness"
    request = {
        "turn_id": turn_id, "session_id": f"session-{turn_id}",
        "operation_id": f"operation-{turn_id}", "idempotency_key": f"key-{turn_id}",
        "scope": {"project_id": PROJECT_ID, "series_id": None},
        "context_policy": {"max_context_bytes": 4096},
    }
    store.claim_turn(request)
    store.append(_event(turn_id, 1, "turn.accepted"), expected_sequence=0)
    content = {
        "schema_version": "1.0.0", "skill_id": "compatible-harness-skill",
        "skill_fingerprint": "compatible-harness-skill-r1", "markdown": "已授权的兼容 Harness 项目背景。",
    }
    context_ref = store.put(turn_id, "application-skill-instructions-compatible-harness", content)
    capability_ref = store.put(turn_id, "capability-manifest", {"capability_ids": []})
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}", turn_id=turn_id,
        resolver_id="compatible-harness-fixture", project_id=PROJECT_ID, series_id=None,
        project_profile_id=f"project-capability-{PROJECT_ID}", project_profile_revision=1,
        boundary_profile_id=f"project-boundary-{PROJECT_ID}", boundary_profile_revision=1,
        capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="compatible-harness-skill-entry", kind="application_skill",
            source_ref=f"crp://skills/{PROJECT_ID}/compatible-harness-skill", payload_ref=context_ref,
            source_project_id=PROJECT_ID, revision_identity="compatible-harness-skill-r1",
            content_fingerprint=None, provenance_refs=(), disclosure="model", selection_reason="project_scope",
            content_bytes=len(content["markdown"].encode("utf-8")),
        ),),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=len(content["markdown"].encode("utf-8")),
    )
    manifest_ref = store.put(turn_id, "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(turn_id, 2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    return context_ref


def _event(turn_id: str, sequence: int, event_type: str, *, payload_ref: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{turn_id}-{sequence}",
        "turn_id": turn_id, "session_id": f"session-{turn_id}", "sequence": sequence,
        "type": event_type, "actor": "ai-kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": f"operation-{turn_id}"},
        "data": {"status": "running", "summary": "private", "capability_id": None, "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
        "occurred_at": NOW,
    }
