"""Existing-lifespan startup composition for empty-Vault authority initialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.fresh_vault_shared_trust_audit_bootstrap import (
    bootstrap_fresh_vault_shared_trust_audit,
)


@dataclass(frozen=True, slots=True)
class FreshVaultSharedTrustAuditStartupReport:
    outcome: str
    object_count: int
    operation_id: str | None = None
    error_code: str | None = None


def bootstrap_fresh_vault_shared_trust_audit_on_startup(
    application: FastAPI,
    runtime_root: Path,
) -> FreshVaultSharedTrustAuditStartupReport:
    """Attempt one bounded bootstrap and expose a stable, path-free report."""

    _store, settings = build_rebuild_object_store(runtime_root)
    try:
        result = bootstrap_fresh_vault_shared_trust_audit(
            runtime_root,
            namespace_id=settings.namespace_id,
        )
        report = FreshVaultSharedTrustAuditStartupReport(
            outcome=result.outcome,
            object_count=result.object_count,
            operation_id=result.operation_id,
        )
    except Exception:
        report = FreshVaultSharedTrustAuditStartupReport(
            outcome="rejected",
            object_count=0,
            error_code="fresh_vault_bootstrap_rejected",
        )
    application.state.fresh_vault_shared_trust_audit_bootstrap = report
    return report
