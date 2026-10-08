"""Core Reaper adapter for one status-only MCP recovery observation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.api.mcp_runtime import MCPRemoteStatusRecoverySession
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.effect_log import Effect, EffectClass, EffectState


@dataclass(slots=True)
class _MCPStatusProbeControl:
    remaining_timeout_ms: int

    def checkpoint(self) -> None:
        if self.remaining_timeout_ms < 1:
            raise TimeoutError("MCP recovery probe deadline expired")


class MCPRemoteEffectRecoveryProbe:
    """Map reviewed remote status into Core states without target replay."""

    def __init__(
        self,
        turn_store: SQLiteAITurnStore,
        session: MCPRemoteStatusRecoverySession,
    ) -> None:
        if not isinstance(turn_store, SQLiteAITurnStore):
            raise ValueError("MCP recovery Turn store is invalid")
        if not isinstance(session, MCPRemoteStatusRecoverySession):
            raise ValueError("MCP recovery session is invalid")
        self._turn_store = turn_store
        self._session = session

    def __call__(self, effect: Effect) -> tuple[EffectState, str | None]:
        local = self._turn_store.verify_mcp_call_effect(effect)
        if local != (EffectState.UNKNOWN, "mcp.remote_status_required"):
            return local
        if effect.effect_class is not EffectClass.QUERYABLE:
            return EffectState.UNKNOWN, "mcp.effect_class_invalid"
        try:
            frozen = self._turn_store.get(effect.intent_ref)
        except KeyError:
            return EffectState.UNKNOWN, "mcp.effect_intent_missing"
        if not isinstance(frozen, Mapping):
            return EffectState.UNKNOWN, "mcp.effect_intent_invalid"
        timeout_ms = frozen.get("timeout_ms")
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 1
        ):
            return EffectState.UNKNOWN, "mcp.recovery_timeout_invalid"
        try:
            observed = self._session.probe(
                frozen,
                execution_control=_MCPStatusProbeControl(min(timeout_ms, 30_000)),
            )
        except Exception as error:
            return EffectState.UNKNOWN, f"mcp.remote_probe_failed:{type(error).__name__}"
        if observed is None:
            return EffectState.UNKNOWN, "mcp.remote_status_unknown"
        certainty = observed.get("effect_certainty")
        if certainty == "confirmed_applied":
            verified = self._turn_store.verify_mcp_call_effect(effect)
            if verified[0] is EffectState.SETTLED_OK:
                return verified
            return EffectState.UNKNOWN, "mcp.remote_receipt_missing_after_applied"
        if certainty == "confirmed_none":
            probe_ref = observed.get("probe_ref")
            return (
                EffectState.UNKNOWN,
                probe_ref if isinstance(probe_ref, str) else "mcp.remote_confirmed_none",
            )
        return EffectState.UNKNOWN, "mcp.remote_status_unknown"
