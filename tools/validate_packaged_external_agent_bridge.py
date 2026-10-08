"""Run the external-Agent Bridge lifecycle from a staged Windows sidecar only.

This is a release-validation utility, not a production launcher.  It creates a
temporary AppRoot, starts the staged Python runtime on loopback, and starts
fresh compatible-client processes for each action.  The child commands use
only the staged ``backend`` and ``rebuild`` module roots; each writes an import
proof that this driver verifies before reporting success.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STAGE = ROOT / "apps" / "desktop-electron" / ".sidecar-stage"
PROJECT_ID = "packaged-external-agent-bridge"
TURN_ID = "turn-packaged-external-agent-bridge"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-root", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--keep-root", action="store_true", help="retain the temporary AppRoot after a failure")
    args = parser.parse_args(argv)
    try:
        result = validate(args.stage_root.resolve(), keep_root=args.keep_root)
    except (AssertionError, OSError, RuntimeError, ValueError, subprocess.SubprocessError, URLError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def validate(stage_root: Path, *, keep_root: bool = False) -> dict[str, object]:
    runtime = stage_root / "runtime" / "python.exe"
    for required in (runtime, stage_root / "backend", stage_root / "rebuild", stage_root / "config" / "settings.toml"):
        if not required.exists():
            raise RuntimeError(f"staged sidecar prerequisite is missing: {required}")

    root = Path(tempfile.mkdtemp(prefix="chriptmas-packaged-external-agent-"))
    try:
        config_root = root / "config"
        config_root.mkdir()
        shutil.copyfile(stage_root / "config" / "settings.toml", config_root / "settings.toml")
        context_ref = _run_seed(runtime, stage_root, root)
        templates = _write_templates(root)
        secret = "packaged-external-agent-secret-" + "x" * 48
        port = _unused_port()
        first = _start_server(runtime, stage_root, root, secret, port)
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_server(base_url, secret, first)
            codex_state = root / "clients" / "codex.json"
            claude_state = root / "clients" / "claude.json"
            started = _run_client(runtime, stage_root, root, base_url, secret, templates["codex"], codex_state, "start", "codex-start")
            proposed = _run_client(runtime, stage_root, root, base_url, secret, templates["claude"], claude_state, "propose", "claude-propose", context_ref=context_ref)
            observed = _run_client(runtime, stage_root, root, base_url, secret, templates["codex"], codex_state, "observe-ack", "codex-proposed", expected_change="memory.proposed")
            candidate_id = _required_str(proposed, "memory_candidate_id")
            _publish_candidate(base_url, secret, candidate_id)
        finally:
            _stop_server(first)

        second = _start_server(runtime, stage_root, root, secret, port)
        try:
            _wait_for_server(base_url, secret, second)
            published = _run_client(runtime, stage_root, root, base_url, secret, templates["codex"], codex_state, "observe-ack", "codex-restart", expected_change="memory.published")
            duplicate = _run_client(runtime, stage_root, root, base_url, secret, templates["codex"], codex_state, "observe-ack", "codex-no-duplicate")
        finally:
            _stop_server(second)

        proofs = _read_proofs(root, stage_root)
        return {
            "status": "passed",
            "stage_root": str(stage_root),
            "server_restarts": 1,
            "import_proofs": len(proofs),
            "actions": [started["status"], proposed["status"], observed["status"], published["status"], duplicate["status"]],
            "published_changes": published["change_types"],
            "duplicate_changes": duplicate["change_types"],
        }
    except BaseException:
        if keep_root:
            print(json.dumps({"retained_app_root": str(root)}, ensure_ascii=False), file=sys.stderr)
            root = None  # type: ignore[assignment]
        raise
    finally:
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def _environment(stage_root: Path, root: Path, secret: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "PYTHONPATH": str(stage_root), "PYTHONNOUSERSITE": "1", "CHRIPTMAS_APP_ROOT": str(root),
        "CHRIPTMAS_DESKTOP_SESSION_MODE": "desktop_production",
        "CHRIPTMAS_DESKTOP_INSTANCE_ID": "packaged-external-agent-validation",
        "CHRIPTMAS_DESKTOP_NONCE": "packaged-external-agent-nonce-" + "n" * 48,
        "CHRIPTMAS_DESKTOP_PROTOCOL_VERSION": "desktop-loopback/1",
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN": "http://127.0.0.1:8001",
    })
    if secret is not None:
        environment["CHRIPTMAS_DESKTOP_SESSION_SECRET"] = secret
    return environment


def _run_seed(runtime: Path, stage_root: Path, root: Path) -> str:
    code = _SEED_CODE
    completed = subprocess.run([str(runtime), "-c", code], cwd=str(root), env=_environment(stage_root, root), capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode:
        raise RuntimeError(f"staged seed failed: {completed.stderr[-1000:]}")
    result = _json_line(completed.stdout)
    return _required_str(result, "context_ref")


def _write_templates(root: Path) -> dict[str, Path]:
    directory = root / "templates"
    directory.mkdir()
    result: dict[str, Path] = {}
    for adapter_id, revision in (("codex", "openai-skill-map-v1"), ("claude", "claude-project-map-v1")):
        target = directory / f"{adapter_id}.md"
        target.write_text(
            f"<!-- chriptmas-os-external-agent-bridge:{adapter_id}:begin -->\n"
            f"adapter_revision: 1\ntemplate_revision: {revision}\n"
            f"<!-- chriptmas-os-external-agent-bridge:{adapter_id}:end -->\n", encoding="utf-8",
        )
        result[adapter_id] = target
    return result


def _start_server(runtime: Path, stage_root: Path, root: Path, secret: str, port: int) -> subprocess.Popen[str]:
    code = textwrap.dedent("""
        import json, os
        from pathlib import Path
        import backend, rebuild, uvicorn
        from backend.api.app import create_app
        stage = Path(os.environ['PYTHONPATH']).resolve()
        modules = [Path(backend.__file__).resolve(), Path(rebuild.__file__).resolve(), Path(create_app.__code__.co_filename).resolve()]
        if any(path != stage and stage not in path.parents for path in modules): raise RuntimeError('non-staged import')
        Path(os.environ['CHRIPTMAS_APP_ROOT'], 'server-import-proof.json').write_text(json.dumps([str(path) for path in modules]), encoding='utf-8')
        uvicorn.run(create_app(), host='127.0.0.1', port=int(os.environ['CHRIPTMAS_GATE_PORT']), log_level='warning')
    """)
    environment = _environment(stage_root, root, secret)
    environment["CHRIPTMAS_GATE_PORT"] = str(port)
    return subprocess.Popen([str(runtime), "-c", code], cwd=str(root), env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _run_client(runtime: Path, stage_root: Path, root: Path, base_url: str, secret: str, template: Path, state: Path, action: str, operation: str, *, context_ref: str | None = None, expected_change: str | None = None) -> dict[str, object]:
    arguments = [str(runtime), str(Path(__file__).resolve()), "--stage-root", str(stage_root), "--client", action, "--base-url", base_url, "--desktop-session", secret, "--template", str(template), "--state", str(state), "--operation", operation]
    if context_ref:
        arguments.extend(("--context-ref", context_ref))
    if expected_change:
        arguments.extend(("--expected-change", expected_change))
    completed = subprocess.run(arguments, cwd=str(root), env=_environment(stage_root, root, secret), capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode:
        raise RuntimeError(f"staged compatible client failed: {completed.stderr[-800:]} {completed.stdout[-800:]}")
    return _json_line(completed.stdout)


def _client_main(args: argparse.Namespace) -> int:
    template = _template(args.template)
    state = _read_state(args.state)
    if args.client == "start":
        state = _start(args, template)
        _write_state(args.state, state)
        return _emit({"status": "started", "session_id": state["session_id"]})
    if args.client == "propose":
        session = _start(args, template)
        _request(args, "POST", f"/api/ai/external-agents/context-sessions/{session['session_id']}/resolve", {"operation_id": args.operation + "-resolve", "context_refs": [args.context_ref], "expected_context_manifest_revision": session["context_manifest_revision"], "purpose": "project_assistance"}, 200)
        proposal = _request(args, "POST", f"/api/ai/external-agents/context-sessions/{session['session_id']}/memory-proposals", {"operation_id": args.operation + "-propose", "expected_context_manifest_revision": session["context_manifest_revision"], "purpose": "project_assistance", "confirm": True, "proposal": {"proposal_type": "memory_candidate_proposal", "summary": "打包 sidecar 独立 Harness 候选。", "source_refs": [args.context_ref], "evidence_refs": [args.context_ref], "suggested_changes": {"target_layer": "scenario", "candidate_type": "external_agent_memory", "proposed_content": "打包 sidecar 的外部 Agent 通过 Bridge 共享已审核项目背景。"}, "requires_user_review": True}}, 201)
        _write_state(args.state, session)
        return _emit({"status": "proposed", "memory_candidate_id": _required_str(proposal, "memory_candidate_id")})
    if state is None:
        raise RuntimeError("client restart state is absent")
    changes = _request(args, "GET", f"/api/ai/external-agents/context-sessions/{state['session_id']}/changes", {"operation_id": args.operation + "-changes", "after_cursor": state["acknowledged_cursor"], "purpose": "project_assistance", "limit": 32}, 200)
    types = [item["change_type"] for item in changes["changes"]]
    if args.expected_change is not None and types != [args.expected_change]:
        raise RuntimeError("unexpected change sequence")
    if args.expected_change is None and types:
        raise RuntimeError("duplicate changes after durable acknowledgement")
    acknowledgement = _request(args, "POST", f"/api/ai/external-agents/context-sessions/{state['session_id']}/acknowledgements", {"operation_id": args.operation + "-ack", "acknowledged_cursor": changes["next_cursor"], "purpose": "project_assistance"}, 200)
    state["acknowledged_cursor"] = acknowledgement["acknowledged_cursor"]
    _write_state(args.state, state)
    return _emit({"status": "acknowledged", "change_types": types})


def _start(args: argparse.Namespace, template: dict[str, object]) -> dict[str, object]:
    result = _request(args, "POST", "/api/ai/external-agents/context-sessions", {"operation_id": args.operation + "-start", "adapter_id": template["adapter_id"], "adapter_revision": template["adapter_revision"], "template_revision": template["template_revision"], "turn_id": TURN_ID, "project_id": PROJECT_ID, "purpose": "project_assistance", "requested_context_bytes": 4096, "confirm": True}, 201)
    return {"adapter_id": template["adapter_id"], "adapter_revision": template["adapter_revision"], "template_revision": template["template_revision"], "session_id": _required_str(result, "session_id"), "context_manifest_revision": _required_str(result, "context_manifest_revision"), "acknowledged_cursor": result["acknowledged_cursor"]}


def _template(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    import re
    match = re.search(r"^<!-- chriptmas-os-external-agent-bridge:([a-z][a-z0-9._-]{0,63}):begin -->$", text, re.MULTILINE)
    fields = dict(re.findall(r"^(adapter_revision|template_revision): ([^\r\n]+)$", text, re.MULTILINE))
    if match is None or len(fields) != 2: raise RuntimeError("generated template is invalid")
    return {"adapter_id": match.group(1), "adapter_revision": int(fields["adapter_revision"]), "template_revision": fields["template_revision"]}


def _request(args: argparse.Namespace, method: str, path: str, payload: dict[str, object], expected: int) -> dict[str, object]:
    url = args.base_url + path
    headers = {"X-Chriptmas-Desktop-Session": args.desktop_session}
    data: bytes | None = None
    if method == "GET": url += "?" + urlencode(payload)
    else: data = json.dumps(payload).encode(); headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=10) as response:
        if response.status != expected: raise RuntimeError(f"Bridge HTTP {response.status}")
        value = json.loads(response.read())
    if not isinstance(value, dict): raise RuntimeError("Bridge response is invalid")
    return value


def _publish_candidate(base_url: str, secret: str, candidate_id: str) -> None:
    review = _http(base_url, secret, "POST", f"/api/rebuild/memory-candidates/{candidate_id}/review", {"action": "promote_to_scenario", "reason": "打包 Gate 用户审核。", "series_id": PROJECT_ID, "atom_ids": []})
    staged_id = _required_str(review, "promoted_object_id")
    _http(base_url, secret, "POST", f"/api/rebuild/staging-scenarios/{staged_id}/publication", {"confirm": True, "reason": "打包 Gate 用户发布。"})


def _http(base_url: str, secret: str, method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    data = json.dumps(payload).encode()
    with urlopen(Request(base_url + path, data=data, headers={"Content-Type": "application/json", "X-Chriptmas-Desktop-Session": secret}, method=method), timeout=10) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict): raise RuntimeError("publication response is invalid")
    return value


def _wait_for_server(base_url: str, secret: str, process: subprocess.Popen[str]) -> None:
    end = time.monotonic() + 25
    while time.monotonic() < end:
        if process.poll() is not None: raise RuntimeError("staged server exited: " + (process.stderr.read() if process.stderr else ""))
        try:
            with urlopen(Request(base_url + "/api/health", headers={"X-Chriptmas-Desktop-Session": secret}), timeout=1) as response:
                if response.status == 200: return
        except URLError: pass
        time.sleep(.1)
    raise RuntimeError("staged server did not become ready")


def _stop_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)


def _read_proofs(root: Path, stage_root: Path) -> list[str]:
    proof = root / "server-import-proof.json"
    if not proof.is_file(): raise RuntimeError("staged server import proof is missing")
    paths = json.loads(proof.read_text(encoding="utf-8"))
    if not isinstance(paths, list) or not paths: raise RuntimeError("staged server import proof is invalid")
    stage = stage_root.resolve()
    if any(not isinstance(item, str) or (Path(item).resolve() != stage and stage not in Path(item).resolve().parents) for item in paths): raise RuntimeError("repo source fallback detected")
    return paths


def _read_state(path: Path) -> dict[str, object] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8"); temporary.replace(path)


def _json_line(text: str) -> dict[str, object]:
    value = json.loads(text.strip().splitlines()[-1])
    if not isinstance(value, dict): raise RuntimeError("child result is invalid")
    return value


def _emit(value: dict[str, object]) -> int: print(json.dumps(value, sort_keys=True)); return 0
def _required_str(value: dict[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result: raise RuntimeError(f"missing {field}")
    return result
def _unused_port() -> int:
    with socket.socket() as listener: listener.bind(("127.0.0.1", 0)); return int(listener.getsockname()[1])


_SEED_CODE = r'''
import json, os
from datetime import datetime, timezone
from pathlib import Path
from rebuild.ai_kernel import ContextEntry, ContextManifest, SQLiteAITurnStore, context_manifest_to_payload
root = Path(os.environ['CHRIPTMAS_APP_ROOT']); project = 'packaged-external-agent-bridge'; turn = 'turn-packaged-external-agent-bridge'
store = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
request = {'turn_id': turn, 'session_id': 'session-' + turn, 'operation_id': 'operation-' + turn, 'idempotency_key': 'key-' + turn, 'scope': {'project_id': project, 'series_id': None}, 'context_policy': {'max_context_bytes': 4096}}
store.claim_turn(request)
def event(seq, kind, ref=None): return {'schema_version':'1.0.0','event_id':f'event-{turn}-{seq}','turn_id':turn,'session_id':'session-'+turn,'sequence':seq,'type':kind,'actor':'ai-kernel','correlation':{'step_id':None,'tool_call_id':None,'model_request_id':None,'operation_id':'operation-'+turn},'data':{'status':'running','summary':'private','capability_id':None,'payload_ref':ref,'receipt_ref':None,'evidence_refs':[],'error_code':None,'retryable':False},'occurred_at':datetime(2026,8,29,8,0,tzinfo=timezone.utc).isoformat()}
store.append(event(1, 'turn.accepted'), expected_sequence=0)
payload = store.put(turn, 'application-skill-instructions-packaged', {'schema_version':'1.0.0','skill_id':'packaged-bridge','skill_fingerprint':'packaged-bridge-r1','markdown':'打包 sidecar 已授权项目背景。'})
caps = store.put(turn, 'capability-manifest', {'capability_ids': []})
entry = ContextEntry(entry_id='packaged-entry',kind='application_skill',source_ref=f'crp://skills/{project}/packaged-bridge',payload_ref=payload,source_project_id=project,revision_identity='packaged-bridge-r1',content_fingerprint=None,provenance_refs=(),disclosure='model',selection_reason='project_scope',content_bytes=len('打包 sidecar 已授权项目背景。'.encode()))
manifest = ContextManifest(manifest_id='context-manifest-'+turn,turn_id=turn,resolver_id='packaged-gate',project_id=project,series_id=None,project_profile_id='project-capability-'+project,project_profile_revision=1,boundary_profile_id='project-boundary-'+project,boundary_profile_revision=1,capability_manifest_ref=caps,entries=(entry,),compactions=(),excluded_reason_counts=(),max_context_bytes=4096,selected_context_bytes=entry.content_bytes)
ref = store.put(turn, 'context-manifest', context_manifest_to_payload(manifest)); store.append(event(2, 'context.resolved', ref), expected_sequence=1)
print(json.dumps({'context_ref': payload}))
'''


if __name__ == "__main__":
    # The recursive client mode intentionally imports only Python stdlib.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--stage-root", type=Path); parser.add_argument("--client", choices=("start", "propose", "observe-ack")); parser.add_argument("--base-url"); parser.add_argument("--desktop-session"); parser.add_argument("--template", type=Path); parser.add_argument("--state", type=Path); parser.add_argument("--operation"); parser.add_argument("--context-ref"); parser.add_argument("--expected-change")
    known, remaining = parser.parse_known_args()
    if known.client:
        raise SystemExit(_client_main(known))
    raise SystemExit(main(remaining))
