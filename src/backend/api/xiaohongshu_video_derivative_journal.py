"""Durable, non-authoritative checkpoint for Xiaohongshu staged video outputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from backend.api.governed_staged_video import GovernedStagedVideoDerivative, GovernedStagedVideoOutcome
from core.job_runner.media_execution_receipt import media_job_uri_segment


SCHEMA_VERSION = "1.0.0"
_FIELDS = frozenset({"schema_version", "job_segment", "asset_segment", "manifest_ref", "manifest_revision", "source_id", "source_asset_id", "wall_ms", "derivatives", "state_hash"})
_LEGACY_FIELDS = _FIELDS - {"asset_segment"}
_DERIVATIVE_FIELDS = frozenset({"kind", "ordinal", "media_type", "byte_count", "content_sha256", "relative_path"})
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._:/-]{0,319}$")
_STATE_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_TYPES = {"audio": "audio/wav", "frame": "image/jpeg"}


class XiaohongshuVideoDerivativeJournalError(ValueError):
    """Stable error when a video derivative checkpoint cannot be trusted."""


@dataclass(frozen=True, slots=True)
class XiaohongshuVideoDerivativeCheckpoint:
    output_ref: str
    state_hash: str


class XiaohongshuVideoDerivativeJournal:
    """Atomically commit and strictly restore Job-and-asset-scoped derivatives.

    A media Job can contain more than one video.  The individual derivative
    records therefore use their own CRP reference and file beneath the Job
    directory.  ``checkpoint(job_id=...)`` intentionally retains the old
    single-video convenience only when that Job has exactly one asset record.
    """

    def __init__(self, runtime_root: Path, *, namespace_id: str = "default") -> None:
        self._media_root = Path(runtime_root).resolve(strict=False) / ".rebuild-data" / "media-hands"
        self._journal_root = self._media_root / "xiaohongshu-video-derivative-journals"
        self._namespace = _identity(namespace_id, "namespace")

    def commit(
        self, *, job_id: str, manifest_ref: str, manifest_revision: str, source_id: str,
        source_asset_id: str, outcome: GovernedStagedVideoOutcome,
    ) -> XiaohongshuVideoDerivativeCheckpoint:
        segment = self._segment(job_id)
        asset_segment = self._asset_segment(source_asset_id)
        if not isinstance(outcome, GovernedStagedVideoOutcome) or outcome.wall_ms < 0:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative outcome is invalid")
        derivatives = self._derivatives_for_commit(outcome)
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION, "job_segment": segment, "asset_segment": asset_segment,
            "manifest_ref": _manifest_ref(manifest_ref), "manifest_revision": _identity(manifest_revision, "manifest revision"),
            "source_id": _identity(source_id, "source id"), "source_asset_id": _identity(source_asset_id, "source asset id"),
            "wall_ms": outcome.wall_ms, "derivatives": derivatives,
        }
        payload["state_hash"] = _state_hash(payload)
        path = self._path(segment, asset_segment)
        if path.exists():
            if self._read(path, segment, asset_segment) != payload:
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal drifted")
            return self._checkpoint(segment, asset_segment, str(payload["state_hash"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, payload)
        return self._checkpoint(segment, asset_segment, str(payload["state_hash"]))

    def restore(
        self, *, job_id: str, manifest_ref: str, manifest_revision: str, source_id: str,
        source_asset_id: str, receipt: Mapping[str, object],
    ) -> GovernedStagedVideoOutcome:
        segment = self._segment(job_id)
        asset_segment = self._asset_segment(source_asset_id)
        cleaned = self._receipt(receipt)
        if cleaned.output_ref == self._checkpoint_ref(segment, asset_segment):
            payload = self._read(self._path(segment, asset_segment), segment, asset_segment)
        elif cleaned.output_ref == self._legacy_checkpoint_ref(segment):
            payload = self._read(self._legacy_path(segment), segment, None)
        else:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative receipt output is invalid")
        expected = {
            "manifest_ref": _manifest_ref(manifest_ref), "manifest_revision": _identity(manifest_revision, "manifest revision"),
            "source_id": _identity(source_id, "source id"), "source_asset_id": _identity(source_asset_id, "source asset id"),
        }
        if any(payload[name] != value for name, value in expected.items()):
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative binding drifted")
        if cleaned.state_hash != payload["state_hash"]:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative receipt state drifted")
        rows = payload["derivatives"]
        assert isinstance(rows, list)
        values = tuple(self._restore_derivative(row) for row in rows)
        if not values or values[0].kind != "audio" or not any(item.kind == "frame" for item in values[1:]):
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is invalid")
        return GovernedStagedVideoOutcome(values[0], tuple(values[1:]), int(payload["wall_ms"]))

    def checkpoint(
        self, *, job_id: str, source_asset_id: str | None = None,
    ) -> XiaohongshuVideoDerivativeCheckpoint:
        """Return a checkpoint for one asset, or the legacy unique Job record."""

        segment = self._segment(job_id)
        if source_asset_id is None:
            records = tuple(self._journal_root.joinpath(segment).glob("*.json")) if self._journal_root.joinpath(segment).is_dir() else ()
            legacy = self._legacy_path(segment)
            if len(records) == 1 and not legacy.exists():
                asset_segment = records[0].stem
            elif not records and legacy.exists():
                payload = self._read(legacy, segment, None)
                return self._legacy_checkpoint(segment, str(payload["state_hash"]))
            else:
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative asset is ambiguous")
        else:
            asset_segment = self._asset_segment(source_asset_id)
        payload = self._read(self._path(segment, asset_segment), segment, asset_segment)
        return self._checkpoint(segment, asset_segment, str(payload["state_hash"]))

    def _derivatives_for_commit(self, outcome: GovernedStagedVideoOutcome) -> list[dict[str, object]]:
        values = (outcome.audio, *outcome.frames)
        if not outcome.frames:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative outcome is invalid")
        result: list[dict[str, object]] = []
        for index, item in enumerate(values):
            kind = "audio" if index == 0 else "frame"
            ordinal = 0 if index == 0 else index - 1
            if not isinstance(item, GovernedStagedVideoDerivative) or item.kind != kind or item.ordinal != ordinal or item.media_type != _TYPES[kind]:
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative outcome is invalid")
            path = self._resolve(item.staged_path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item.byte_count or item.byte_count < 1:
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative is unavailable")
            result.append({"kind": kind, "ordinal": ordinal, "media_type": item.media_type, "byte_count": item.byte_count,
                           "content_sha256": _sha256(path), "relative_path": path.relative_to(self._media_root).as_posix()})
        return result

    def _read(self, path: Path, segment: str, asset_segment: str | None) -> dict[str, object]:
        if not path.is_file() or path.is_symlink():
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is unavailable")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is unreadable") from error
        expected_fields = _LEGACY_FIELDS if asset_segment is None else _FIELDS
        if (not isinstance(raw, Mapping) or set(raw) != expected_fields
                or raw.get("schema_version") != SCHEMA_VERSION or raw.get("job_segment") != segment
                or (asset_segment is not None and raw.get("asset_segment") != asset_segment)):
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal fields are invalid")
        payload = dict(raw)
        _manifest_ref(payload.get("manifest_ref")); _identity(payload.get("manifest_revision"), "manifest revision")
        _identity(payload.get("source_id"), "source id"); _identity(payload.get("source_asset_id"), "source asset id")
        if not isinstance(payload.get("wall_ms"), int) or isinstance(payload.get("wall_ms"), bool) or payload["wall_ms"] < 0:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is invalid")
        records = payload.get("derivatives")
        if not isinstance(records, list) or len(records) < 2:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is invalid")
        normalized = self._records(records)
        canonical = {name: payload[name] for name in expected_fields if name != "state_hash"}
        if payload.get("state_hash") != _state_hash(canonical):
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal state drifted")
        payload["derivatives"] = normalized
        return payload

    def _records(self, rows: list[object]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping) or set(raw) != _DERIVATIVE_FIELDS:
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal fields are invalid")
            row = dict(raw)
            expected_kind, expected_ordinal = ("audio", 0) if index == 0 else ("frame", index - 1)
            if row.get("kind") != expected_kind or row.get("ordinal") != expected_ordinal or row.get("media_type") != _TYPES[expected_kind]:
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is invalid")
            size, digest, relative = row.get("byte_count"), row.get("content_sha256"), row.get("relative_path")
            if not isinstance(size, int) or isinstance(size, bool) or size < 1 or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None or not isinstance(relative, str):
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative journal is invalid")
            self._relative(relative)
            result.append(row)
        return result

    def _restore_derivative(self, row: Mapping[str, object]) -> GovernedStagedVideoDerivative:
        path = self._resolve(str(row["relative_path"]))
        if path.is_symlink() or not path.is_file():
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative is unavailable")
        if path.stat().st_size != row["byte_count"]:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative size drifted")
        if _sha256(path) != row["content_sha256"]:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative content drifted")
        return GovernedStagedVideoDerivative(str(row["kind"]), int(row["ordinal"]), str(row["media_type"]), str(path), int(row["byte_count"]))

    def _resolve(self, value: str) -> Path:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else self._media_root / self._relative(value)
        root = self._media_root.resolve(strict=False)
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative path escaped its root") from error
        checked = root
        for part in relative.parts:
            checked = checked / part
            if checked.is_symlink():
                raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative path is a symlink")
        path = candidate.resolve(strict=False)
        if not path.is_relative_to(root):
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative path escaped its root")
        return path

    @staticmethod
    def _relative(value: str) -> str:
        if not value or "\\" in value or Path(value).is_absolute() or ".." in Path(value).parts or value.startswith("/"):
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative path is invalid")
        return value

    def _segment(self, job_id: str) -> str:
        return media_job_uri_segment(_identity(job_id, "job id"))

    def _path(self, segment: str, asset_segment: str) -> Path:
        return self._journal_root / segment / f"{asset_segment}.json"

    def _legacy_path(self, segment: str) -> Path:
        return self._journal_root / f"{segment}.json"

    def _checkpoint_ref(self, segment: str, asset_segment: str) -> str:
        return f"crp://{self._namespace}/jobs/{segment}/video-derivatives/xiaohongshu/assets/{asset_segment}"

    def _legacy_checkpoint_ref(self, segment: str) -> str:
        return f"crp://{self._namespace}/jobs/{segment}/video-derivatives/xiaohongshu"

    def _checkpoint(self, segment: str, asset_segment: str, state_hash: str) -> XiaohongshuVideoDerivativeCheckpoint:
        if _STATE_HASH.fullmatch(state_hash) is None:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative state hash is invalid")
        return XiaohongshuVideoDerivativeCheckpoint(self._checkpoint_ref(segment, asset_segment), state_hash)

    def _legacy_checkpoint(self, segment: str, state_hash: str) -> XiaohongshuVideoDerivativeCheckpoint:
        if _STATE_HASH.fullmatch(state_hash) is None:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative state hash is invalid")
        return XiaohongshuVideoDerivativeCheckpoint(self._legacy_checkpoint_ref(segment), state_hash)

    @staticmethod
    def _asset_segment(source_asset_id: str) -> str:
        return _identity(source_asset_id, "source asset id")

    @staticmethod
    def _receipt(value: Mapping[str, object]) -> XiaohongshuVideoDerivativeCheckpoint:
        if not isinstance(value, Mapping) or set(value) != {"output_ref", "state_hash"}:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative receipt fields are invalid")
        output, state = value.get("output_ref"), value.get("state_hash")
        if not isinstance(output, str) or _CRP_REF.fullmatch(output) is None or not isinstance(state, str) or _STATE_HASH.fullmatch(state) is None:
            raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative receipt is invalid")
        return XiaohongshuVideoDerivativeCheckpoint(output, state)

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise XiaohongshuVideoDerivativeJournalError(f"xiaohongshu video derivative {name} is invalid")
    return value


def _manifest_ref(value: object) -> str:
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None or "/source-manifests/" not in value:
        raise XiaohongshuVideoDerivativeJournalError("xiaohongshu video derivative manifest reference is invalid")
    return value


def _state_hash(payload: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
