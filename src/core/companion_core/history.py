from __future__ import annotations

from collections.abc import Callable, Mapping

from .errors import CompanionRepositoryError
from .models import CompanionForgetReceipt, CompanionMessageDependency
from .repository import CompanionRepository


DependencyEraser = Callable[[str], None]


class CompanionHardForgetError(CompanionRepositoryError):
    def __init__(self, message: str, *, receipt: CompanionForgetReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class CompanionHistoryService:
    """Coordinates durable local deletion without retaining message content in receipts."""

    def __init__(
        self,
        repository: CompanionRepository,
        *,
        dependency_erasers: Mapping[str, DependencyEraser] | None = None,
    ) -> None:
        self.repository = repository
        self.dependency_erasers = dict(dependency_erasers or {})

    def forget(self, message_id: str) -> CompanionForgetReceipt:
        receipt, dependencies = self.repository.begin_forget(message_id)
        if receipt.status == "completed":
            return receipt
        affected = dict(receipt.affected)
        affected.setdefault("message", 1)
        for dependency in dependencies:
            if dependency.state in {"deleted", "withdrawn"}:
                affected[dependency.dependent_kind] = max(1, affected.get(dependency.dependent_kind, 0))
                continue
            step = f"erase:{dependency.dependent_kind}"
            try:
                self._erase(dependency)
                terminal = "withdrawn" if dependency.dependent_kind == "published_memory" else "deleted"
                self.repository.mark_forget_dependency(dependency, state=terminal)
                affected[dependency.dependent_kind] = affected.get(dependency.dependent_kind, 0) + 1
            except Exception as exc:
                try:
                    self.repository.mark_forget_dependency(dependency, state="failed")
                except CompanionRepositoryError:
                    pass
                failed = self.repository.fail_forget(message_id, failed_step=step, affected=affected)
                raise CompanionHardForgetError("hard forget dependency cleanup failed", receipt=failed) from exc
        try:
            return self.repository.finalize_forget(message_id, affected=affected)
        except CompanionRepositoryError as exc:
            existing = self.repository.get_forget_receipt(message_id)
            failed = existing if existing is not None and existing.status == "failed" else self.repository.fail_forget(
                message_id, failed_step="commit:message", affected=affected,
            )
            raise CompanionHardForgetError("hard forget commit failed", receipt=failed) from exc

    def _erase(self, dependency: CompanionMessageDependency) -> None:
        eraser = self.dependency_erasers.get(dependency.dependent_kind)
        if eraser is None:
            raise CompanionHardForgetError("hard forget dependency eraser is unavailable")
        eraser(dependency.dependent_id)
