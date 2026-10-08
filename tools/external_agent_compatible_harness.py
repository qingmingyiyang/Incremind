"""Minimal HTTP harness for validating external-Agent Bridge compatibility.

This is intentionally not a production client integration.  It consumes the
same generated, marker-owned instruction block that a compatible client would
receive, then uses only the public local HTTP Bridge contract.  Its tiny JSON
state file contains no template text or desktop secret, so a later process can
continue a durable change cursor without sharing a Python object or model
context.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


_BEGIN = re.compile(
    r"^<!-- chriptmas-os-external-agent-bridge:([a-z][a-z0-9._-]{0,63}):begin -->$",
    re.MULTILINE,
)
_FIELD = re.compile(r"^(adapter_revision|template_revision): ([^\r\n]+)$", re.MULTILINE)
_ACTIONS = frozenset({"start", "propose", "observe-ack"})


class HarnessError(RuntimeError):
    """The template, client state, or HTTP Bridge response was not admissible."""


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    try:
        result = _run_client(args)
    except (HarnessError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the local API for an isolated test root")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, required=True)

    client = subcommands.add_parser("client", help="execute one compatible external-Agent action")
    client.add_argument("--base-url", required=True)
    client.add_argument("--desktop-session", required=True)
    client.add_argument("--template", type=Path, required=True)
    client.add_argument("--state", type=Path, required=True)
    client.add_argument("--action", choices=sorted(_ACTIONS), required=True)
    client.add_argument("--operation-prefix", required=True)
    client.add_argument("--turn-id")
    client.add_argument("--project-id")
    client.add_argument("--context-ref")
    client.add_argument("--expect-change-type")
    return parser


def _serve(args: argparse.Namespace) -> int:
    import uvicorn
    from backend.api.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


def _run_client(args: argparse.Namespace) -> dict[str, object]:
    template = _read_template(args.template)
    state = _read_state(args.state)
    if args.action == "start":
        session = _start(args, template)
        _write_state(args.state, session)
        return {
            "status": "started",
            "adapter_id": template["adapter_id"],
            "session_id": session["session_id"],
            "event_cursor": session["event_cursor"],
        }
    if args.action == "propose":
        if not args.turn_id or not args.project_id or not args.context_ref:
            raise HarnessError("proposal requires turn, project, and context ref")
        session = _start(args, template)
        resolved = _post(
            args, f"/api/ai/external-agents/context-sessions/{session['session_id']}/resolve", {
                "operation_id": f"{args.operation_prefix}-resolve",
                "context_refs": [args.context_ref],
                "expected_context_manifest_revision": session["context_manifest_revision"],
                "purpose": "project_assistance",
            }, expected=200,
        )
        proposal = _post(
            args, f"/api/ai/external-agents/context-sessions/{session['session_id']}/memory-proposals", {
                "operation_id": f"{args.operation_prefix}-propose",
                "expected_context_manifest_revision": session["context_manifest_revision"],
                "purpose": "project_assistance",
                "confirm": True,
                "proposal": {
                    "proposal_type": "memory_candidate_proposal",
                    "summary": "独立兼容 Harness 提交的待审核项目记忆候选。",
                    "source_refs": [args.context_ref],
                    "evidence_refs": [args.context_ref],
                    "suggested_changes": {
                        "target_layer": "scenario",
                        "candidate_type": "external_agent_memory",
                        "proposed_content": "两个兼容外部 Agent 只通过受治理 Bridge 共享项目背景。",
                    },
                    "requires_user_review": True,
                },
            }, expected=201,
        )
        session["resolved_context_refs"] = [args.context_ref]
        _write_state(args.state, session)
        return {
            "status": "proposed",
            "adapter_id": template["adapter_id"],
            "session_id": session["session_id"],
            "resolved_slice_count": len(_required_list(resolved, "slices")),
            "memory_candidate_id": _required_str(proposal, "memory_candidate_id"),
            "memory_publication_state": _required_str(proposal, "memory_publication_state"),
        }
    if state is None:
        raise HarnessError("observe action requires durable prior client state")
    _assert_state_matches_template(state, template)
    changes = _get(args, f"/api/ai/external-agents/context-sessions/{state['session_id']}/changes", {
        "operation_id": f"{args.operation_prefix}-changes",
        "after_cursor": state["acknowledged_cursor"],
        "purpose": "project_assistance",
        "limit": 32,
    }, expected=200)
    change_types = [_required_str(item, "change_type") for item in _required_list(changes, "changes")]
    expected_type = args.expect_change_type
    if expected_type is not None and change_types != [expected_type]:
        raise HarnessError("unexpected change types")
    if expected_type is None and change_types:
        raise HarnessError("expected no unacknowledged changes")
    next_cursor = _required_int(changes, "next_cursor")
    acknowledgement = _post(
        args, f"/api/ai/external-agents/context-sessions/{state['session_id']}/acknowledgements", {
            "operation_id": f"{args.operation_prefix}-ack",
            "acknowledged_cursor": next_cursor,
            "purpose": "project_assistance",
        }, expected=200,
    )
    state["acknowledged_cursor"] = _required_int(acknowledgement, "acknowledged_cursor")
    _write_state(args.state, state)
    return {
        "status": "acknowledged",
        "adapter_id": template["adapter_id"],
        "session_id": state["session_id"],
        "change_types": change_types,
        "acknowledged_cursor": state["acknowledged_cursor"],
    }


def _start(args: argparse.Namespace, template: dict[str, object]) -> dict[str, object]:
    if not args.turn_id or not args.project_id:
        raise HarnessError("start requires turn and project")
    result = _post(args, "/api/ai/external-agents/context-sessions", {
        "operation_id": f"{args.operation_prefix}-start",
        "adapter_id": template["adapter_id"],
        "adapter_revision": template["adapter_revision"],
        "template_revision": template["template_revision"],
        "turn_id": args.turn_id,
        "project_id": args.project_id,
        "purpose": "project_assistance",
        "requested_context_bytes": 4096,
        "confirm": True,
    }, expected=201)
    return {
        "schema_version": "1.0.0",
        "adapter_id": template["adapter_id"],
        "adapter_revision": template["adapter_revision"],
        "template_revision": template["template_revision"],
        "session_id": _required_str(result, "session_id"),
        "project_id": args.project_id,
        "turn_id": args.turn_id,
        "context_manifest_revision": _required_str(result, "context_manifest_revision"),
        "event_cursor": _required_int(result, "event_cursor"),
        "acknowledged_cursor": _required_int(result, "acknowledged_cursor"),
    }


def _read_template(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HarnessError("template is unavailable") from error
    begin = _BEGIN.search(text)
    fields = dict(_FIELD.findall(text))
    if begin is None or text.count(begin.group(0)) != 1 or len(fields) != 2:
        raise HarnessError("template is not one exact generated bridge block")
    adapter_id = begin.group(1)
    end = f"<!-- chriptmas-os-external-agent-bridge:{adapter_id}:end -->"
    if text.count(end) != 1:
        raise HarnessError("template is not one exact generated bridge block")
    try:
        revision = int(fields["adapter_revision"])
    except ValueError as error:
        raise HarnessError("template adapter revision is invalid") from error
    if revision < 1 or not fields["template_revision"].strip():
        raise HarnessError("template metadata is invalid")
    return {
        "adapter_id": adapter_id,
        "adapter_revision": revision,
        "template_revision": fields["template_revision"],
    }


def _read_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise HarnessError("durable client state is unreadable") from error
    if not isinstance(value, dict):
        raise HarnessError("durable client state is invalid")
    return value


def _write_state(path: Path, state: dict[str, object]) -> None:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(path)


def _assert_state_matches_template(state: dict[str, object], template: dict[str, object]) -> None:
    for field in ("adapter_id", "adapter_revision", "template_revision", "session_id", "acknowledged_cursor"):
        if field not in state:
            raise HarnessError("durable client state is incomplete")
    if any(state[field] != template[field] for field in ("adapter_id", "adapter_revision", "template_revision")):
        raise HarnessError("durable client state does not match template")
    _required_str(state, "session_id")
    _required_int(state, "acknowledged_cursor")


def _headers(args: argparse.Namespace) -> dict[str, str]:
    return {"X-Chriptmas-Desktop-Session": args.desktop_session}


def _post(args: argparse.Namespace, path: str, body: dict[str, object], *, expected: int) -> dict[str, object]:
    import httpx

    response = httpx.post(f"{args.base_url.rstrip('/')}{path}", json=body, headers=_headers(args), timeout=10.0)
    return _response_json(response.status_code, response.text, expected)


def _get(args: argparse.Namespace, path: str, query: dict[str, object], *, expected: int) -> dict[str, object]:
    import httpx

    response = httpx.get(f"{args.base_url.rstrip('/')}{path}", params=query, headers=_headers(args), timeout=10.0)
    return _response_json(response.status_code, response.text, expected)


def _response_json(status: int, text: str, expected: int) -> dict[str, object]:
    if status != expected:
        raise HarnessError(f"Bridge returned HTTP {status}, expected {expected}")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise HarnessError("Bridge response was not JSON") from error
    if not isinstance(value, dict):
        raise HarnessError("Bridge response shape is invalid")
    return value


def _required_str(value: Any, field: str) -> str:
    result = value.get(field) if isinstance(value, dict) else None
    if not isinstance(result, str) or not result:
        raise HarnessError(f"Bridge response {field} is invalid")
    return result


def _required_int(value: Any, field: str) -> int:
    result = value.get(field) if isinstance(value, dict) else None
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise HarnessError(f"Bridge response {field} is invalid")
    return result


def _required_list(value: Any, field: str) -> list[dict[str, object]]:
    result = value.get(field) if isinstance(value, dict) else None
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise HarnessError(f"Bridge response {field} is invalid")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
