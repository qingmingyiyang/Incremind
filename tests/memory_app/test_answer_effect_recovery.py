"""Durable result, outcome and Effect gaps for never-retried reads."""
from dataclasses import replace
import pytest
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime
from core.effect_log import EffectClass, EffectReaper, EffectRunner, EffectState
from tests.rebuild.test_ai_kernel_sqlite_store import _request, _registry, _Planner, _Provider, _CrashBeforeEffectSettleRunner


def registry(provider):
    from core.ai_tooling import tool_from_capability
    from core.ai_kernel import CapabilityDefinition, ScopedCapabilityRegistry
    definition = CapabilityDefinition("memory.recall", 1, "read", False, "read_only",
        "crp://default/contracts/in.schema.json", "crp://default/contracts/out.schema.json")
    tool = tool_from_capability(definition)
    tool = replace(tool, idempotency="never_retry",
        retry_policy=replace(tool.retry_policy, max_attempts=1, retryable_error_codes=()))
    registered = ScopedCapabilityRegistry()
    registered.register(replace(definition, tool_definition=tool), provider)
    return registered


@pytest.mark.parametrize("gap", ["result", "outcome", "effect"])
def test_read_only_never_retry_crash_evidence(tmp_path, gap):
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    class Provider(_Provider):
        def invoke(self, request):
            output = super().invoke(request)
            store.get_or_create_immutable_payload(request["turn_id"], "product-answer-result-v2", {"answer": "durable"})
            if gap == "result":
                raise SystemExit("result durable")
            return output
    provider = Provider(receipt=False)
    runner = (_CrashBeforeEffectSettleRunner(store.effect_runner.log, owner_id="crash", lease_seconds=1)
              if gap == "outcome" else EffectRunner(store.effect_runner.log, owner_id="crash", lease_seconds=1))
    class Runtime(SynchronousAIRuntime):
        def _append(self, turn_id, event_type, *args, **kwargs):
            if gap == "effect" and event_type == "tool.completed":
                raise SystemExit("effect durable")
            return super()._append(turn_id, event_type, *args, **kwargs)
    runtime = Runtime(planner=_Planner("memory.recall"), registry=registry(provider),
        events=store, payloads=store, state=store, effect_runner=runner)
    with pytest.raises(SystemExit):
        runtime.submit_turn(_request())
    events = store.events_after(_request()["turn_id"])
    intent = next(e for e in events if e["type"] == "tool.intent.recorded")
    identity = intent["correlation"]["tool_call_id"]
    effect = store.effect_runner.log.get(identity)
    assert effect.effect_class is EffectClass.AT_MOST_ONCE
    assert store.get_immutable_payload(_request()["turn_id"], "product-answer-result-v2") is not None
    if gap == "effect":
        assert effect.state is EffectState.SETTLED_OK
    else:
        EffectReaper(store.effect_runner.log).recover_expired(now=2_000_000_000,
            verifiers={effect.kind: store.verify_tool_call_effect})
        state = store.effect_runner.log.get(identity).state
        assert state is (EffectState.UNKNOWN if gap == "result" else EffectState.SETTLED_OK)
    assert provider.calls == 1
