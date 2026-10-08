from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Callable, Mapping
from time import time
from typing import Protocol, TypeVar

from .core import (
    EffectClass,
    EffectIntent,
    EffectPurpose,
    EffectRunner,
    EffectState,
)


T = TypeVar("T")

WORKFLOW_EFFECT_CLASSES = {
    "audio_auto_transcribe": EffectClass.IDEMPOTENT,
    "video_auto_extract_audio": EffectClass.IDEMPOTENT,
    "video_auto_transcribe_audio": EffectClass.IDEMPOTENT,
    "video_auto_summarize_transcript": EffectClass.IDEMPOTENT,
    "video_auto_create_memory_candidate": EffectClass.IDEMPOTENT,
    "video_auto_publish_memory": EffectClass.IDEMPOTENT,
    "workbench_auto_extract_audio": EffectClass.IDEMPOTENT,
    "workbench_auto_transcribe_audio": EffectClass.IDEMPOTENT,
    "workbench_auto_summarize_transcript": EffectClass.IDEMPOTENT,
    "workbench_auto_create_memory_candidate": EffectClass.IDEMPOTENT,
    "workbench_auto_publish_memory": EffectClass.IDEMPOTENT,
    "long_audio_split": EffectClass.IDEMPOTENT,
    "workbench_auto_fetch_url": EffectClass.QUERYABLE,
    "workbench_auto_document_extract": EffectClass.IDEMPOTENT,
    "workbench_auto_image_ocr": EffectClass.IDEMPOTENT,
    "workbench_auto_prepare_file": EffectClass.IDEMPOTENT,
    "workbench_auto_prepare_video": EffectClass.IDEMPOTENT,
    "bilibili_authorized_download": EffectClass.AT_MOST_ONCE,
    "provider_model_discovery": EffectClass.QUERYABLE,
}
WORKFLOW_EFFECT_KINDS = tuple(WORKFLOW_EFFECT_CLASSES)


class WorkflowReceiptStore(Protocol):
    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None: ...
    def write(
        self, collection: str, object_id: str, value: Mapping[str, object],
        expected_revision: int | None,
    ) -> object: ...


class WorkflowEffectPending(RuntimeError):
    pass


class EffectWorkflowHandler:
    """Execute one workflow Handler under Core Effect ownership.

    Workflow code supplies only the next immutable step intent. This adapter
    owns Handler invocation and the durable Receipt that terminal Effect state
    references. It never retries; Core Reaper decides expired INFLIGHT state.
    """

    _COLLECTION = "workflow_effect_receipts"

    def __init__(
        self, runner: EffectRunner, receipts: WorkflowReceiptStore, *, namespace_id: str,
    ) -> None:
        self._runner = runner
        self._receipts = receipts
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        operation_id: str,
        session_id: str,
        root_id: str,
        step_key: str,
        kind: str,
        intent_ref: str,
        gate_decision_id: str,
        rev_set: Mapping[str, object],
        payload: Mapping[str, object],
        effect_class: EffectClass,
        invoke: Callable[[], T],
        encode: Callable[[T], Mapping[str, object]],
        decode: Callable[[Mapping[str, object]], T],
    ) -> T:
        intent = EffectIntent(
            session_id=session_id,
            root_id=root_id,
            step_key=step_key,
            kind=kind,
            effect_class=effect_class,
            purpose=EffectPurpose.PRIMARY,
            intent_ref=intent_ref,
            gate_decision_id=gate_decision_id,
            rev_set=rev_set,
            payload=payload,
            idem_key=operation_id,
            operation_id_override=operation_id,
        )
        effect, _ = self._runner.log.plan(intent, now=_now(payload))
        if effect.state is EffectState.SETTLED_OK:
            return self._load(operation_id, decode)
        captured: list[T] = []

        def handler(claimed_effect) -> str:
            value = invoke()
            captured.append(value)
            return self.record_result(claimed_effect, value, encode=encode)

        settled = self._runner.execute_planned(
            operation_id, handler, now=_now(payload),
            receipt_kind="workflow-step-receipt",
        )
        if settled.state is EffectState.SETTLED_OK:
            return captured[0] if captured else self._load(operation_id, decode)
        raise WorkflowEffectPending(
            f"workflow Effect {operation_id} is {settled.state.value}"
        )

    def record_result(
        self, effect, value: T, *, encode: Callable[[T], Mapping[str, object]],
    ) -> str:
        """Persist the immutable Receipt produced by a registered Handler."""

        encoded = dict(encode(value))
        receipt = {
            "schema_version": "1.0.0",
            "operation_id": effect.operation_id,
            "kind": effect.kind,
            "result": encoded,
        }
        receipt_id = _receipt_id(effect.operation_id)
        existing = self._receipts.read(self._COLLECTION, receipt_id)
        if existing is None:
            self._receipts.write(
                self._COLLECTION, receipt_id, receipt, expected_revision=None,
            )
        elif dict(existing) != receipt:
            raise ValueError("workflow Effect Receipt content drifted")
        return self._receipt_ref(effect.operation_id)

    def load_result(
        self, operation_id: str, decode: Callable[[Mapping[str, object]], T],
    ) -> T:
        return self._load(operation_id, decode)

    def verify(self, operation_id: str) -> tuple[EffectState, str | None]:
        receipt = self._receipts.read(self._COLLECTION, _receipt_id(operation_id))
        if receipt is None:
            return EffectState.PLANNED, None
        if receipt.get("operation_id") != operation_id or not isinstance(receipt.get("result"), Mapping):
            return EffectState.UNKNOWN, "workflow.receipt_invalid"
        return EffectState.SETTLED_OK, self._receipt_ref(operation_id)

    def verify_effect(self, effect) -> tuple[EffectState, str | None]:
        state, outcome_ref = self.verify(effect.operation_id)
        if state is EffectState.PLANNED and effect.effect_class is EffectClass.AT_MOST_ONCE:
            return EffectState.UNKNOWN, "workflow.at_most_once_receipt_missing"
        return state, outcome_ref

    def _load(
        self, operation_id: str, decode: Callable[[Mapping[str, object]], T],
    ) -> T:
        receipt = self._receipts.read(self._COLLECTION, _receipt_id(operation_id))
        result = receipt.get("result") if isinstance(receipt, Mapping) else None
        if not isinstance(result, Mapping):
            raise ValueError("workflow Effect Receipt is unavailable")
        return decode(result)

    def _receipt_ref(self, operation_id: str) -> str:
        return (
            f"crp://{self._namespace_id}/workflow-effect-receipts/"
            f"{operation_id}"
        )


def _now(payload: Mapping[str, object]) -> int:
    value = payload.get("recorded_at_epoch")
    return (
        int(value)
        if isinstance(value, int) and not isinstance(value, bool)
        else int(time())
    )


def _receipt_id(operation_id: str) -> str:
    encoded = urlsafe_b64encode(operation_id.encode("utf-8")).decode("ascii")
    return f"operation-{encoded.rstrip('=')}"
