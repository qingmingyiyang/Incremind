from __future__ import annotations

from types import SimpleNamespace

from backend.api.mcp_recovery_probe import MCPRemoteEffectRecoveryProbe
from backend.api.mcp_runtime import MCPRemoteStatusRecoverySession
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.effect_log import EffectClass, EffectState


class _Session(MCPRemoteStatusRecoverySession):
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[object, object]] = []

    def probe(self, frozen_intent, *, execution_control):
        self.calls.append((frozen_intent, execution_control))
        if self.error is not None:
            raise self.error
        return self.result


def _effect():
    return SimpleNamespace(
        effect_class=EffectClass.QUERYABLE,
        intent_ref="crp://session/turn-1/mcp-side-effect-intent/call-1",
    )


def _intent() -> dict[str, object]:
    return {
        "server_id": "calendar", "tool_id": "calendar.create",
        "tool_contract": {"frozen": "contract"}, "invocation_id": "call-1",
        "turn_id": "turn-1", "operation_id": "operation-1",
        "idempotency_key": "operation-1:call-1", "attempt": 1,
        "timeout_ms": 45_000,
    }


def _probe(tmp_path, monkeypatch, session, verification):
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    values = iter(verification)
    monkeypatch.setattr(store, "verify_mcp_call_effect", lambda _effect: next(values))
    monkeypatch.setattr(store, "get", lambda _ref: _intent())
    return MCPRemoteEffectRecoveryProbe(store, session)


def test_remote_applied_requires_new_local_receipt_before_settle(tmp_path, monkeypatch) -> None:
    session = _Session({"effect_certainty": "confirmed_applied"})
    probe = _probe(tmp_path, monkeypatch, session, (
        (EffectState.UNKNOWN, "mcp.remote_status_required"),
        (EffectState.SETTLED_OK, "crp://session/turn-1/mcp-receipt/call-1"),
    ))

    result = probe(_effect())

    assert result == (EffectState.SETTLED_OK, "crp://session/turn-1/mcp-receipt/call-1")
    assert session.calls[0][1].remaining_timeout_ms == 30_000


def test_remote_confirmed_none_stays_unknown_and_never_authorizes_replay(tmp_path, monkeypatch) -> None:
    session = _Session({
        "effect_certainty": "confirmed_none",
        "probe_ref": "crp://session/turn-1/mcp-probe/call-1",
    })
    probe = _probe(tmp_path, monkeypatch, session, (
        (EffectState.UNKNOWN, "mcp.remote_status_required"),
    ))

    assert probe(_effect()) == (
        EffectState.UNKNOWN, "crp://session/turn-1/mcp-probe/call-1",
    )


def test_local_receipt_short_circuits_remote_session(tmp_path, monkeypatch) -> None:
    session = _Session(error=AssertionError("remote session must not open"))
    probe = _probe(tmp_path, monkeypatch, session, (
        (EffectState.SETTLED_OK, "crp://session/turn-1/mcp-receipt/call-1"),
    ))

    assert probe(_effect())[0] is EffectState.SETTLED_OK
    assert session.calls == []


def test_remote_error_and_applied_without_receipt_remain_unknown(tmp_path, monkeypatch) -> None:
    failed = _probe(
        tmp_path, monkeypatch, _Session(error=TimeoutError("status timeout")),
        ((EffectState.UNKNOWN, "mcp.remote_status_required"),),
    )
    assert failed(_effect())[0] is EffectState.UNKNOWN

    missing = _probe(tmp_path, monkeypatch, _Session({"effect_certainty": "confirmed_applied"}), (
        (EffectState.UNKNOWN, "mcp.remote_status_required"),
        (EffectState.UNKNOWN, "mcp.remote_status_required"),
    ))
    assert missing(_effect()) == (
        EffectState.UNKNOWN, "mcp.remote_receipt_missing_after_applied",
    )
