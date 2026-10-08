from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class LegacyLibraryDryRunError(ValueError):
    """Raised when a legacy library dry-run report cannot be produced safely."""


class LegacyLibraryDryRunReporterPort(Protocol):
    """Persists read-only legacy library dry-run reports."""

    def save_report(self, report: Mapping[str, object]) -> Mapping[str, object]:
        """Save one report and return the persisted payload."""


class LegacyLibraryDryRunStorePort(LegacyLibraryDryRunReporterPort, Protocol):
    """Reads and updates read-only legacy library dry-run reports."""

    def get(self, report_id: str) -> Mapping[str, object] | None:
        """Read one dry-run report."""

    def update_report(self, report: Mapping[str, object]) -> Mapping[str, object]:
        """Persist an updated dry-run report."""


@dataclass(frozen=True, slots=True)
class LegacyLibraryDryRunResult:
    report_id: str
    report: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LegacyLibraryDryRunReviewResult:
    report_id: str
    report: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CreateLegacyLibraryDryRunReport:
    """Scans an existing legacy library without mutating it and saves a review report."""

    reports: LegacyLibraryDryRunReporterPort
    namespace_id: str = "default"

    def execute(
        self,
        *,
        legacy_root: Path,
        requested_by: str,
        created_at: datetime | None = None,
        max_files: int = 50,
    ) -> LegacyLibraryDryRunResult:
        if not requested_by:
            raise LegacyLibraryDryRunError("requested_by is required")
        if max_files <= 0:
            raise LegacyLibraryDryRunError("max_files must be positive")
        root = legacy_root.expanduser()
        if not root.exists():
            raise LegacyLibraryDryRunError("legacy root must already exist")
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise LegacyLibraryDryRunError("legacy root must be a directory")

        timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
        legacy_segment = _safe_legacy_segment(root.name)
        candidates = _scan_candidates(root, legacy_segment=legacy_segment, max_files=max_files)
        report_id = _report_id(candidates, legacy_segment=legacy_segment, timestamp=timestamp)
        report: dict[str, object] = {
            "schema_version": "1.0.0",
            "id": report_id,
            "status": "review_ready",
            "legacy_root_uri": f"legacy-readonly://{legacy_segment}",
            "mode": "read_only",
            "namespace_id": self.namespace_id,
            "requested_by": requested_by,
            "created_at": timestamp,
            "file_count": len(candidates),
            "total_bytes": sum(_candidate_size(candidate) for candidate in candidates),
            "truncated": _count_files(root) > max_files,
            "candidate_refs": [candidate["source_ref"] for candidate in candidates],
            "candidates": list(candidates),
            "safety": {
                "legacy_write_allowed": False,
                "migration_executed": False,
                "published_outputs": [],
            },
            "review_decision": None,
        }
        persisted = self.reports.save_report(report)
        return LegacyLibraryDryRunResult(report_id=report_id, report=persisted)


@dataclass(frozen=True, slots=True)
class ReviewLegacyLibraryDryRunReport:
    """Accept or reject a dry-run report for planning without executing migration."""

    reports: LegacyLibraryDryRunStorePort

    def accept_for_planning(
        self,
        *,
        report_id: str,
        reviewed_by: str,
        reviewed_at: datetime | None = None,
        note: str | None = None,
    ) -> LegacyLibraryDryRunReviewResult:
        return self._review(
            report_id=report_id,
            decision="accepted_for_planning",
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            note=note,
        )

    def reject(
        self,
        *,
        report_id: str,
        reviewed_by: str,
        reviewed_at: datetime | None = None,
        note: str | None = None,
    ) -> LegacyLibraryDryRunReviewResult:
        return self._review(
            report_id=report_id,
            decision="rejected",
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            note=note,
        )

    def _review(
        self,
        *,
        report_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: datetime | None,
        note: str | None,
    ) -> LegacyLibraryDryRunReviewResult:
        if decision not in {"accepted_for_planning", "rejected"}:
            raise LegacyLibraryDryRunError("legacy dry-run review decision is not supported")
        if not reviewed_by:
            raise LegacyLibraryDryRunError("legacy dry-run review requires reviewed_by")
        report = self.reports.get(report_id)
        if report is None:
            raise LegacyLibraryDryRunError("legacy dry-run report was not found")
        if report.get("status") != "review_ready":
            raise LegacyLibraryDryRunError("legacy dry-run review requires review_ready status")
        _assert_report_still_read_only(report)
        timestamp = (reviewed_at or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
        updated = dict(report)
        updated["status"] = decision
        updated["review_decision"] = {
            "decision": decision,
            "reviewed_by": reviewed_by,
            "reviewed_at": timestamp,
            "note": note,
            "migration_approved": False,
            "migration_executed": False,
            "published_outputs": [],
        }
        saved = self.reports.update_report(updated)
        return LegacyLibraryDryRunReviewResult(report_id=report_id, report=saved)


def _scan_candidates(root: Path, *, legacy_segment: str, max_files: int) -> tuple[Mapping[str, object], ...]:
    candidates: list[Mapping[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if len(candidates) >= max_files:
            break
        relative_path = path.relative_to(root).as_posix()
        digest = _sha256_file(path)
        candidates.append(
            {
                "candidate_id": f"legacy-source-{digest[:12]}",
                "relative_path": relative_path,
                "kind": _candidate_kind(path),
                "size_bytes": path.stat().st_size,
                "content_sha256": f"sha256:{digest}",
                "source_ref": f"legacy-readonly://{legacy_segment}/{relative_path}#sha256:{digest[:12]}",
                "imported": False,
            }
        )
    return tuple(candidates)


def _count_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".txt":
        return "text"
    if suffix == ".json":
        return "json"
    return "other"


def _candidate_size(candidate: Mapping[str, object]) -> int:
    size = candidate.get("size_bytes")
    return size if isinstance(size, int) and not isinstance(size, bool) else 0


def _assert_report_still_read_only(report: Mapping[str, object]) -> None:
    safety = report.get("safety")
    if not isinstance(safety, Mapping):
        raise LegacyLibraryDryRunError("legacy dry-run report requires safety")
    if safety.get("legacy_write_allowed") is not False:
        raise LegacyLibraryDryRunError("legacy dry-run review requires read-only report")
    if safety.get("migration_executed") is not False:
        raise LegacyLibraryDryRunError("legacy dry-run review must not follow executed migration")
    published_outputs = safety.get("published_outputs")
    if not isinstance(published_outputs, list) or published_outputs:
        raise LegacyLibraryDryRunError("legacy dry-run review requires no published outputs")


def _report_id(candidates: tuple[Mapping[str, object], ...], *, legacy_segment: str, timestamp: str) -> str:
    digest = hashlib.sha256()
    digest.update(legacy_segment.encode("utf-8"))
    digest.update(timestamp.encode("utf-8"))
    for candidate in candidates:
        digest.update(str(candidate.get("source_ref", "")).encode("utf-8"))
    return f"legacy-dry-run-{digest.hexdigest()[:16]}"


def _safe_legacy_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9._~-]+", "-", value.strip()).strip("-._~")
    if not segment:
        return "library"
    if not re.match(r"^[A-Za-z0-9]", segment):
        segment = f"library-{segment}"
    return segment[:64]
