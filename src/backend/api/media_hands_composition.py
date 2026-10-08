from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.api.job_lifecycle_runtime import get_or_create_rebuild_job_lifecycle
from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.media_hands_runtime import (
    MediaHandsRuntimeResolution,
    configure_media_hands_runtime,
)
from backend.api.mixed_media_e2e_fixture import install_mixed_media_e2e_fixture
from backend.api.xhs_controlled_credential_e2e_fixture import (
    install_xhs_controlled_credential_e2e_fixture,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.expert_media_job_wait_bridge import ExpertMediaJobTerminal, ExpertMediaJobWaitBridge
from core.job_runner import RoutedJobRepository, SQLiteJobRuntimeLifecycle


@dataclass(frozen=True, slots=True)
class MediaHandsComposition:
    resolution: MediaHandsRuntimeResolution
    lifecycle: SQLiteJobRuntimeLifecycle
    object_store: object
    repository: RoutedJobRepository
    expert_job_wait_bridge: ExpertMediaJobWaitBridge | None = None


def compose_media_hands_lifecycle(
    application: object,
    *,
    runtime_root: Path,
    object_store: object,
    repository: RoutedJobRepository,
    namespace_id: str,
    ai_runtime: object | None = None,
    session_store: object | None = None,
) -> MediaHandsComposition:
    """Freeze Hands before the app's sole Job lifecycle is constructed."""

    container = getattr(application.state, "container", None)
    if container is not None:
        install_mixed_media_e2e_fixture(
            container,
            runtime_root,
            object_store=object_store,
            namespace_id=namespace_id,
        )
        install_xhs_controlled_credential_e2e_fixture(container)
    resolution = configure_media_hands_runtime(
        application,
        runtime_root=runtime_root,
        object_store=object_store,
        repository=repository,
    )
    bridge = _compose_expert_media_job_wait_bridge(
        application, repository=repository, namespace_id=namespace_id,
        runtime=ai_runtime, session_store=session_store,
    )
    lifecycle = get_or_create_rebuild_job_lifecycle(
        application,
        repository,
        object_store,
        namespace_id=namespace_id,
    )
    if bridge is not None:
        application.state.expert_media_job_wait_bridge = bridge
        application.state.expert_media_job_wait_reconcile = (
            lambda: _reconcile_expert_media_job_waits(bridge, repository, namespace_id)
        )
    return MediaHandsComposition(resolution, lifecycle, object_store, repository, bridge)


def compose_application_media_hands(
    application: object,
    container: object,
) -> MediaHandsComposition:
    runtime_root = Path(getattr(container, "root_dir"))
    object_store, settings = build_rebuild_object_store(runtime_root)
    repository = build_rebuild_job_repository(runtime_root, object_store)
    return compose_media_hands_lifecycle(
        application,
        runtime_root=runtime_root,
        object_store=object_store,
        repository=repository,
        namespace_id=settings.namespace_id,
        ai_runtime=getattr(application.state, "ai_runtime", None),
        session_store=getattr(application.state, "ai_turn_store", None),
    )


def _compose_expert_media_job_wait_bridge(
    application: object, *, repository: RoutedJobRepository, namespace_id: str,
    runtime: object | None, session_store: object | None,
) -> ExpertMediaJobWaitBridge | None:
    """Compose only the complete trusted bridge; partial dependencies disable it."""
    existing = getattr(application.state, "expert_media_job_wait_bridge", None)
    if isinstance(existing, ExpertMediaJobWaitBridge):
        return existing
    if runtime is None or session_store is None:
        return None
    if not callable(getattr(session_store, "list_expert_job_waits", None)):
        return None
    if not callable(getattr(repository.sqlite, "read", None)):
        return None

    configure_verifier = getattr(runtime, "configure_expert_job_terminal_verifier", None)
    if not callable(configure_verifier):
        return None
    configure_verifier(lambda terminal: _verify_terminal_execution_receipt(repository, terminal))

    def continue_turn(
        turn_id: str, _terminal: ExpertMediaJobTerminal, lease: object,
    ) -> None:
        # The bridge's short strict lease is the fresh owner after the original
        # runner released its inert wait lease.  Runtime continuation only
        # consults the already-terminal Job; it does not submit an action.
        resume = getattr(runtime, "resume_expert_job_wait", None)
        if not callable(resume):
            raise RuntimeError("expert Media Job wake runtime is unavailable")
        resume(turn_id, lease)

    return ExpertMediaJobWaitBridge(
        session_store, runtime,
        notify_wake=lambda _turn_id, _terminal: None,
        continue_turn=continue_turn,
    )


def _observe_media_terminal(
    bridge: ExpertMediaJobWaitBridge, record: object, repository: RoutedJobRepository,
) -> None:
    """Project one terminal Media record into its frozen expert wait identity."""
    payload = getattr(record, "payload", None)
    revision = getattr(record, "revision", None)
    if not isinstance(payload, Mapping) or not isinstance(revision, int):
        return
    for snapshot in _terminal_snapshots_for_media_record(
        bridge, payload=payload, revision=revision, repository=repository,
    ):
        bridge.observe_terminal(snapshot)


def _terminal_snapshots_for_media_record(
    bridge: ExpertMediaJobWaitBridge, *, payload: Mapping[str, object],
    revision: int, repository: RoutedJobRepository,
) -> tuple[Mapping[str, object], ...]:
    if payload.get("job_type") != "media_hands" or payload.get("status") not in {"completed", "failed", "cancelled"}:
        return ()
    media = payload.get("media_hands")
    if not isinstance(media, Mapping):
        return ()
    manifest = media.get("manifest")
    source_id = payload.get("source_id")
    job_id = payload.get("id")
    if (
        not isinstance(manifest, Mapping) or not isinstance(source_id, str)
        or not isinstance(job_id, str) or not isinstance(manifest.get("ref"), str)
        or not isinstance(manifest.get("revision"), str)
    ):
        return ()
    import re
    if re.fullmatch(r"[a-z][a-z0-9-]{2,127}", source_id) is None:
        return ()
    waits = bridge.iter_pending_waits(batch_size=64, max_batches=256)
    job_ref = f"crp://jobs/{source_id}"
    matching = [item for item in waits if item.get("job_ref") == job_ref]
    if not matching:
        return ()
    status = str(payload["status"])
    receipt_ref: str | None = None
    terminal_evidence: Mapping[str, object]
    if status == "completed":
        receipt = _load_terminal_execution_receipt(
            repository, payload=payload, job_id=job_id, source_id=source_id,
            manifest_ref=manifest["ref"], manifest_revision=manifest["revision"],
        )
        if receipt is None:
            return ()
        receipt_ref = receipt["receipt_ref"]
        terminal_evidence = {
            "kind": "media_execution_receipt", "status": status, "job_id": job_id,
            "job_revision": revision, "execution_id": str(payload["idempotency_key"]),
        }
    else:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        if not isinstance(code, str) or not code:
            code = "job_cancelled" if status == "cancelled" else None
        if code is None:
            return ()
        terminal_evidence = {"kind": "media_job_terminal", "status": status, "job_id": job_id, "job_revision": revision, "code": code}
    return tuple({
        "schema_version": "1.0.0", "turn_id": item["turn_id"], "terminal_job_id": job_id,
        "canonical_job_ref": job_ref, "job_revision": revision, "status": status,
        "receipt_ref": receipt_ref, "terminal_evidence": dict(terminal_evidence), "source_manifest_ref": manifest["ref"],
        "source_manifest_revision": manifest["revision"],
    } for item in matching if isinstance(item.get("turn_id"), str))


def _load_terminal_execution_receipt(
    repository: RoutedJobRepository, *, payload: Mapping[str, object], job_id: str,
    source_id: str, manifest_ref: object, manifest_revision: object,
) -> Mapping[str, object] | None:
    execution_id = payload.get("idempotency_key")
    if not isinstance(execution_id, str) or not execution_id:
        return None
    try:
        receipt = repository.sqlite.get_media_execution_receipt(job_id, execution_id=execution_id)
    except (ValueError, KeyError):
        return None
    if not isinstance(receipt, Mapping):
        return None
    if (
        receipt.get("job_id") != job_id or receipt.get("source_id") != source_id
        or receipt.get("manifest_ref") != manifest_ref
        or receipt.get("manifest_revision") != manifest_revision
        or not isinstance(receipt.get("receipt_ref"), str)
        or not isinstance(receipt.get("published_outputs"), list)
        or not receipt["published_outputs"]
    ):
        return None
    return receipt


def _verify_terminal_execution_receipt(
    repository: RoutedJobRepository, terminal: Mapping[str, object],
) -> bool:
    """Re-read and exactly bind a completed terminal to the sole Job authority."""
    evidence = terminal.get("terminal_evidence")
    if not isinstance(evidence, Mapping):
        return False
    job_id = terminal.get("terminal_job_id")
    execution_id = evidence.get("execution_id")
    if not isinstance(job_id, str) or not isinstance(execution_id, str):
        return False
    try:
        receipt = repository.sqlite.get_media_execution_receipt(job_id, execution_id=execution_id)
    except (ValueError, KeyError):
        return False
    return bool(
        isinstance(receipt, Mapping)
        and receipt.get("job_id") == job_id
        and receipt.get("receipt_ref") == terminal.get("receipt_ref")
        and receipt.get("manifest_ref") == terminal.get("source_manifest_ref")
        and receipt.get("manifest_revision") == terminal.get("source_manifest_revision")
        and isinstance(receipt.get("published_outputs"), list)
        and bool(receipt["published_outputs"])
    )


def _reconcile_expert_media_job_waits(
    bridge: ExpertMediaJobWaitBridge, repository: RoutedJobRepository, namespace_id: str,
) -> int:
    snapshots = tuple(
        item for record in repository.sqlite.all()
        for item in _terminal_snapshots_for_media_record(
            bridge, payload=record.payload, revision=record.revision, repository=repository,
        )
    )
    return bridge.reconcile_startup(lambda: snapshots)
