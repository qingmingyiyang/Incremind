from __future__ import annotations

import sqlite3
from dataclasses import replace
from threading import Event, Lock, Thread

import pytest

from core.effect_log import EffectLog, EffectReaper, EffectRunner, EffectState
from core.product_core.index_rebuild_effect_admission import (
    IndexRebuildEffectAdmissionFactory, SQLiteIndexRebuildEffectAdmission,
)
from core.product_core.index_rebuild_effect_execution import (
    IndexRebuildEffectExecutionHandler, IndexRebuildEffectExecutionProbe, IndexRebuildQueryOutcome,
)


def _admitted():
    return IndexRebuildEffectAdmissionFactory(admitted_at=100).build(
        request={"id": "request-1", "backend_kind": "sqlite_fts5", "reason": "freshness", "source_refs": ["source-a#rev:1"]},
        manifest={"id": "manifest-1", "backend_kind": "sqlite_fts5", "source_fingerprint": "fp-1"},
        ledger={"id": "ledger-1", "source_fingerprint": "fp-1", "entry_count": 2},
    )


def _effect(tmp_path):
    admitted = _admitted()
    log = EffectLog(tmp_path / "effects.sqlite")
    with sqlite3.connect(log.database, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        effect, _ = SQLiteIndexRebuildEffectAdmission(log).admit_in_connection(connection, admitted, now=100)
        connection.commit()
    manifest = dict(admitted.request["manifest"])
    ledger = dict(admitted.request["ledger"])
    return log, effect, admitted, manifest, ledger


def _not_completed(_domain):
    return IndexRebuildQueryOutcome("not_completed")


def _handler(
    log, manifest, ledger, *, after_reservation_write=None, after_domain_write=None,
    query_completion=_not_completed, execute=None,
):
    calls = []
    if execute is None:
        execute = lambda operation_id, request, _manifest, _ledger: calls.append((operation_id, request)) or {
            "status": "ready", "artifact_ref": "facts:index-rebuild-artifact-1",
            "artifact_revision": "rev-1", "entry_count": 2,
        }
    handler = IndexRebuildEffectExecutionHandler(
        log.database,
        lambda _ref: manifest,
        lambda _ref: ledger,
        execute,
        query_completion=query_completion,
        after_reservation_write=after_reservation_write,
        after_domain_write=after_domain_write,
    )
    return handler, calls


def test_handler_and_probe_construction_do_not_bootstrap_domain_schema(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite")
    manifest = {"id": "manifest-1", "backend_kind": "sqlite_fts5", "source_fingerprint": "fp-1"}
    ledger = {"id": "ledger-1", "source_fingerprint": "fp-1", "entry_count": 2}
    _handler(log, manifest, ledger)
    IndexRebuildEffectExecutionProbe(
        log.database, lambda _ref: manifest, lambda _ref: ledger, _not_completed,
    )
    with sqlite3.connect(log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'index_rebuild_effect_%'"
        ).fetchone()[0] == 0


def test_receipt_is_immutable_domain_evidence_and_core_remains_unsettled(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    handler, calls = _handler(log, manifest, ledger)
    receipt = handler.handle(effect)
    assert receipt.receipt_ref.startswith("receipt:index-rebuild/")
    assert log.get(effect.operation_id).state is EffectState.PLANNED
    assert calls == [(effect.operation_id, dict(_admission.request["request"]))]
    probe = IndexRebuildEffectExecutionProbe(log.database, lambda _ref: manifest, lambda _ref: ledger, _not_completed)
    assert probe.probe(effect) == (EffectState.SETTLED_OK, receipt.receipt_ref)
    assert handler.handle(effect) == receipt
    assert len(calls) == 1


def test_reservation_without_receipt_retries_only_with_the_same_operation_key(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    handler, calls = _handler(log, manifest, ledger, after_reservation_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")))
    try:
        handler.handle(effect)
    except RuntimeError:
        pass
    probe = IndexRebuildEffectExecutionProbe(log.database, lambda _ref: manifest, lambda _ref: ledger, _not_completed)
    assert probe.probe(effect) == (EffectState.PLANNED, f"facts:index-rebuild-not-completed/{effect.operation_id}")
    assert calls == []
    recovery, recovery_calls = _handler(log, manifest, ledger)
    recovery.handle(effect)
    assert recovery_calls == [(effect.operation_id, dict(_admission.request["request"]))]


def test_probe_fails_closed_for_input_manifest_and_ledger_drift(tmp_path) -> None:
    log, effect, admission, manifest, ledger = _effect(tmp_path)
    probe = IndexRebuildEffectExecutionProbe(log.database, lambda _ref: manifest, lambda _ref: ledger, _not_completed)
    assert probe.probe(effect)[0] is EffectState.PLANNED
    manifest["source_fingerprint"] = "changed"
    assert probe.probe(effect) == (EffectState.UNKNOWN, "error:index-rebuild-manifest-drift")
    manifest.update(admission.request["manifest"])
    ledger["entry_count"] = 3
    assert probe.probe(effect) == (EffectState.UNKNOWN, "error:index-rebuild-ledger-drift")
    ledger.update(admission.request["ledger"])
    with sqlite3.connect(log.database) as connection:
        connection.execute(
            "UPDATE effect_intent_fact SET payload_json='{}' WHERE operation_id=?",
            (effect.operation_id,),
        )
    assert probe.probe(effect) == (EffectState.UNKNOWN, "error:index-rebuild-input-drift")


def test_handler_and_probe_require_exact_gate_manifest_and_budget_bindings(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    probe = IndexRebuildEffectExecutionProbe(log.database, lambda _ref: manifest, lambda _ref: ledger, _not_completed)
    drifted_revisions = dict(effect.rev_set)
    drifted_revisions["context_manifest"] = "index-rebuild-manifest:sha256:drift"
    assert probe.probe(replace(effect, rev_set=drifted_revisions)) == (
        EffectState.UNKNOWN, "error:index-rebuild-input-drift",
    )
    with sqlite3.connect(log.database) as connection:
        connection.execute(
            "UPDATE effect_gate_fact SET budget_after='{}' WHERE decision_id=?",
            (effect.gate_decision_id,),
        )
    assert probe.probe(effect) == (EffectState.UNKNOWN, "error:index-rebuild-input-drift")


def test_module_never_imports_legacy_index_job_runtime() -> None:
    import ast
    from pathlib import Path
    source = Path(__file__).parents[2] / "src" / "core" / "product_core" / "index_rebuild_effect_execution.py"
    imports = {alias.name for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not any("index_rebuild_runtime" in name or "index_rebuild_job" in name for name in imports)


def test_probe_distinguishes_completed_not_completed_and_unknown_query_evidence(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    unknown = IndexRebuildEffectExecutionProbe(
        log.database, lambda _ref: manifest, lambda _ref: ledger,
        lambda _domain: IndexRebuildQueryOutcome("unknown"),
    )
    assert unknown(effect) == (EffectState.UNKNOWN, f"error:index-rebuild-query-unknown/{effect.operation_id}")
    completed = IndexRebuildEffectExecutionProbe(
        log.database, lambda _ref: manifest, lambda _ref: ledger,
        lambda _domain: IndexRebuildQueryOutcome("completed", {
            "status": "ready", "artifact_ref": "facts:index-rebuild-artifact-1",
            "artifact_revision": "rev-1", "entry_count": 2,
        }),
    )
    assert completed(effect) == (EffectState.UNKNOWN, f"error:index-rebuild-receipt-missing/{effect.operation_id}")


def test_completed_query_never_materializes_receipt_from_an_expired_probe(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    crashing, calls = _handler(
        log, manifest, ledger, after_domain_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError):
        EffectRunner(log, owner_id="index-a", lease_seconds=1).execute_planned(
            effect.operation_id, crashing, now=100,
        )
    completed = lambda _domain: IndexRebuildQueryOutcome("completed", {
        "status": "ready", "artifact_ref": "facts:index-rebuild-artifact-1",
        "artifact_revision": "rev-1", "entry_count": 2,
    })
    probe = IndexRebuildEffectExecutionProbe(
        log.database, lambda _ref: manifest, lambda _ref: ledger, completed,
    )
    outcome = EffectReaper(log).recover_expired(
        now=102, probes={("index_rebuild", "effect-v2"): probe},
    )
    assert calls == [(effect.operation_id, dict(_admission.request["request"]))]
    assert outcome[0].state is EffectState.UNKNOWN
    assert log.get(effect.operation_id).result_ref is None


def test_core_runner_and_reaper_bind_domain_receipt_after_settle_crash_and_fence_second_worker(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    handler, calls = _handler(log, manifest, ledger)
    runner_a = EffectRunner(log, owner_id="index-a", lease_seconds=1)
    runner_b = EffectRunner(log, owner_id="index-b", lease_seconds=1)
    inflight, claimed = runner_a.claim_planned(effect.operation_id, now=100)
    assert claimed
    assert runner_b.execute_planned(effect.operation_id, handler, now=100).state is EffectState.INFLIGHT
    assert calls == []
    receipt = handler(inflight)
    assert calls == [(effect.operation_id, dict(_admission.request["request"]))]
    assert log.get(effect.operation_id).state is EffectState.INFLIGHT
    probe = IndexRebuildEffectExecutionProbe(log.database, lambda _ref: manifest, lambda _ref: ledger, _not_completed)
    outcome = EffectReaper(log).recover_expired(
        now=102, probes={("index_rebuild", "effect-v2"): probe},
    )
    assert outcome[0].state is EffectState.SETTLED_OK
    assert log.get(effect.operation_id).result_ref == receipt.receipt_ref


def test_reaper_marks_unknown_when_lease_expires_after_unqueryable_reservation(tmp_path) -> None:
    log, effect, _admission, manifest, ledger = _effect(tmp_path)
    handler, calls = _handler(
        log, manifest, ledger, after_reservation_write=lambda: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError):
        EffectRunner(log, owner_id="index-a", lease_seconds=1).execute_planned(effect.operation_id, handler, now=100)
    probe = IndexRebuildEffectExecutionProbe(
        log.database, lambda _ref: manifest, lambda _ref: ledger,
        lambda _domain: IndexRebuildQueryOutcome("unknown"),
    )
    outcome = EffectReaper(log).recover_expired(now=102, probes={("index_rebuild", "effect-v2"): probe})
    assert outcome[0].state is EffectState.UNKNOWN
    assert calls == []


def test_expired_lease_overlap_reuses_operation_key_and_commits_external_work_once(tmp_path) -> None:
    log, effect, admission, manifest, ledger = _effect(tmp_path)
    started = Event()
    second_started = Event()
    release = Event()
    completed = Event()
    lock = Lock()
    in_progress: set[str] = set()
    results: dict[str, dict[str, object]] = {}
    invocations: list[str] = []
    side_effect_count = 0
    result = {
        "status": "ready", "artifact_ref": "facts:index-rebuild-artifact-1",
        "artifact_revision": "rev-1", "entry_count": 2,
    }

    def execute_once(operation_id, request, _manifest, _ledger):
        nonlocal side_effect_count
        assert request == admission.request["request"]
        with lock:
            invocations.append(operation_id)
            cached = results.get(operation_id)
            if cached is not None:
                return dict(cached)
            leader = operation_id not in in_progress
            if leader:
                in_progress.add(operation_id)
        if leader:
            started.set()
            assert release.wait(3)
            with lock:
                side_effect_count += 1
                results[operation_id] = dict(result)
                in_progress.remove(operation_id)
            completed.set()
            return dict(result)
        second_started.set()
        assert completed.wait(3)
        with lock:
            return dict(results[operation_id])

    def query_completion(_domain):
        with lock:
            stored = results.get(effect.operation_id)
        return (
            IndexRebuildQueryOutcome("completed", stored)
            if stored is not None
            else IndexRebuildQueryOutcome("not_completed")
        )

    handler, _calls = _handler(
        log, manifest, ledger, query_completion=query_completion, execute=execute_once,
    )
    errors: list[BaseException] = []

    def run(runner, now):
        try:
            runner.execute_planned(effect.operation_id, handler, now=now)
        except BaseException as error:  # the stale fence loser must fail closed
            errors.append(error)

    first = Thread(target=run, args=(EffectRunner(log, owner_id="index-a", lease_seconds=1), 100))
    first.start()
    assert started.wait(3)
    probe = IndexRebuildEffectExecutionProbe(
        log.database, lambda _ref: manifest, lambda _ref: ledger, query_completion,
    )
    recovered = EffectReaper(log).recover_expired(
        now=102, probes={("index_rebuild", "effect-v2"): probe},
    )
    assert recovered[0].state is EffectState.PLANNED
    second = Thread(target=run, args=(EffectRunner(log, owner_id="index-b", lease_seconds=5), 102))
    second.start()
    assert second_started.wait(3)
    release.set()
    first.join(3)
    second.join(3)

    assert not first.is_alive() and not second.is_alive()
    assert invocations == [effect.operation_id, effect.operation_id]
    assert side_effect_count == 1
    assert log.get(effect.operation_id).state is EffectState.SETTLED_OK
    assert len(errors) == 1
