from __future__ import annotations


class CompanionRepositoryError(RuntimeError):
    """Base error for Companion persistence and recovery boundaries."""


class CompanionConflict(CompanionRepositoryError):
    """Raised when CAS or idempotency input conflicts with stored state."""


class CompanionSchemaTooNew(CompanionRepositoryError):
    """Raised when a database was created by a newer application schema."""


class CompanionIntegrityError(CompanionRepositoryError):
    """Raised when persisted state, a backup, or a ledger is inconsistent."""


class CompanionRestoreConflict(CompanionRepositoryError):
    """Raised when restore preflight or its bound fingerprint no longer matches."""
