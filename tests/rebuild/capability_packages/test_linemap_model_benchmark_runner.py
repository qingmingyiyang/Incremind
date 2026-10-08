from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.capability_packages.thought_graph_context.model_benchmark_runner import (
    ModelBenchmarkRunner,
    ModelBenchmarkRunnerError,
)
from core.context_graph import FrozenContextRevisions, context_binding_to_payload


class _Bindings:
    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.create_calls: list[dict[str, object]] = []
        self.create_revision_drift = False

    def create(self, **values: object):
        self.create_calls.append(dict(values))
        binding_id = str(values["binding_id"])
        if binding_id in self.records:
            raise ValueError("ContextBinding identity already exists")
        binding = values["binding"]
        record = SimpleNamespace(
            binding_id=binding_id,
            project_id=values["project_id"],
            capability_id=values["capability_id"],
            capability_revision=("drift" if self.create_revision_drift else values["capability_revision"]),
            binding=binding,
        )
        self.records[binding_id] = record
        return {
            "binding_id": record.binding_id,
            "project_id": record.project_id,
            "capability_id": record.capability_id,
            "capability_revision": record.capability_revision,
            "binding": context_binding_to_payload(binding),
        }

    def resolve(self, binding_ref: str, *, project_id: str):
        binding_id = binding_ref.rsplit("/", 1)[-1]
        result = self.records[binding_id]
        assert result.project_id == project_id
        return result


class _Turns:
    def __init__(self, *, failed_turn_id: str | None = None) -> None:
        self.requests: list[dict[str, object]] = []
        self._failed_turn_id = failed_turn_id

    def __call__(self, request: dict[str, object]):
        self.requests.append(dict(request))
        turn_id = str(request["turn_id"])
        return SimpleNamespace(
            turn_id=turn_id,
            status="failed" if turn_id == self._failed_turn_id else "completed",
            replayed=any(item["turn_id"] == turn_id for item in self.requests[:-1]),
        )


class _Finalizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **values: object):
        self.calls.append(dict(values))
        return {"artifact_ref": "crp://session/turn/final-artifact"}


def _run(
    *,
    bindings: _Bindings | None = None,
    turns: _Turns | None = None,
    finalizer: _Finalizer | None = None,
):
    bindings = bindings or _Bindings()
    turns = turns or _Turns()
    finalizer = finalizer or _Finalizer()
    runner = ModelBenchmarkRunner(
        bindings=bindings,
        submit_turn=turns,
        finalize=finalizer,
    )
    result = runner.run(
        suite_run_id="suite-real-a",
        project_id="project-alpha",
        session_id="session-benchmark",
        revisions=FrozenContextRevisions("4.0.0", "7", "provider-r2", "route-r4", "2.0.0"),
        consent_refs=("crp://consents/project-alpha/benchmark-r1",),
        created_at="2026-08-30T00:00:00Z",
    )
    return result, bindings, turns, finalizer


def test_runner_submits_three_frozen_bindings_and_six_regular_turns() -> None:
    result, bindings, turns, finalizer = _run()

    assert len(bindings.create_calls) == 3
    assert len(turns.requests) == 6
    assert len(finalizer.calls) == 1
    assert result.turn_ids == tuple(request["turn_id"] for request in turns.requests)
    assert finalizer.calls[0]["turn_ids"] == result.turn_ids
    assert finalizer.calls[0]["coordinator_turn_id"] == result.turn_ids[0]
    assert {request["desired_outcome"] for request in turns.requests} == {"context.evaluate"}
    assert {request["operation_id"] for request in turns.requests} == {
        f"op-lm-suite-real-a-{case}-r0-{variant}"
        for case in ("project_skill", "document", "research_turn")
        for variant in ("linear", "linemap")
    }

    pairs = {
        request["operation_id"].rsplit("-", 1)[0]: request
        for request in turns.requests
        if str(request["operation_id"]).endswith("-linear")
    }
    for prefix, linear in pairs.items():
        linemap = next(
            request for request in turns.requests
            if request["operation_id"] == prefix + "-linemap"
        )
        assert linear["input"]["refs"] == []
        assert linemap["input"]["refs"] and linemap["input"]["text"] != linear["input"]["text"]
        for key in ("scope", "privacy", "capability_policy", "context_policy", "approval_policy", "created_at"):
            assert linear[key] == linemap[key]


def test_runner_rerun_reuses_identical_binding_and_turn_identities() -> None:
    bindings, turns, finalizer = _Bindings(), _Turns(), _Finalizer()
    first, *_ = _run(bindings=bindings, turns=turns, finalizer=finalizer)
    second, *_ = _run(bindings=bindings, turns=turns, finalizer=finalizer)

    assert first.binding_ids == second.binding_ids
    assert first.turn_ids == second.turn_ids
    assert len(bindings.records) == 3
    assert len(turns.requests) == 12
    assert len(finalizer.calls) == 2


def test_runner_does_not_finalize_when_one_turn_is_not_completed() -> None:
    failed = "turn-lm-suite-real-a-document-r0-linemap"
    turns, finalizer = _Turns(failed_turn_id=failed), _Finalizer()

    with pytest.raises(ModelBenchmarkRunnerError, match="did not complete"):
        _run(turns=turns, finalizer=finalizer)

    assert len(turns.requests) == 4
    assert finalizer.calls == []


@pytest.mark.parametrize("mode", ("created_revision", "reused_binding"))
def test_runner_fails_closed_on_binding_or_revision_drift(mode: str) -> None:
    bindings = _Bindings()
    if mode == "created_revision":
        bindings.create_revision_drift = True
    else:
        _run(bindings=bindings)
        existing = bindings.records["binding-lm-suite-real-a-project_skill-r0"]
        bindings.records[existing.binding_id] = SimpleNamespace(
            binding_id=existing.binding_id,
            project_id=existing.project_id,
            capability_id=existing.capability_id,
            capability_revision=existing.capability_revision,
            binding=replace(existing.binding, graph_revision="drift"),
        )

    with pytest.raises(ModelBenchmarkRunnerError, match="drifted"):
        _run(bindings=bindings)
