"""Durable, non-authoritative checkpoint for Xiaohongshu binary staging.

The journal is deliberately an adapter-owned reconstruction aid: Source and Job
remain authoritative.  It stores only the minimum public binding and relative
file identities needed to prove that an interrupted media step may reuse its
already staged files.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from backend.api.xiaohongshu_asset_materializer import XiaohongshuStagedAsset
from core.job_runner.media_execution_receipt import media_job_uri_segment


SCHEMA_VERSION = "1.0.0"
_JOURNAL_FIELDS = frozenset({"schema_version", "job_segment", "manifest_ref", "manifest_revision", "assets", "state_hash"})
_ASSET_FIELDS = frozenset({"asset_id", "ordinal", "kind", "media_type", "byte_count", "content_sha256", "relative_path"})
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._:/-]{0,319}$")
_STATE_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_MEDIA_TYPES = {
    "image": frozenset({"image/jpeg", "image/png", "image/webp"}),
    "video": frozenset({"video/mp4"}),
    "text": frozenset({"text/plain"}),
}


class XiaohongshuStagingJournalError(ValueError):
    """Raised when a checkpoint cannot be safely committed or restored."""


@dataclass(frozen=True, slots=True)
class XiaohongshuStagingCheckpoint:
    """Public receipt-safe identity of one job-scoped staging checkpoint."""

    output_ref: str
    state_hash: str


class XiaohongshuStagingJournal:
    """Atomically persist and strictly restore adapter-owned staged assets."""

    def __init__(self, runtime_root: Path, *, namespace_id: str = "default") -> None:
        self._media_root = Path(runtime_root).resolve(strict=False) / ".rebuild-data" / "media-hands"
        self._journal_root = self._media_root / "xiaohongshu-journals"
        self._namespace = self._identity(namespace_id, "namespace")

    def commit(
        self,
        *,
        job_id: str,
        manifest_ref: str,
        manifest_revision: str,
        assets: tuple[XiaohongshuStagedAsset, ...],
    ) -> XiaohongshuStagingCheckpoint:
        """Record a frozen staging view, or prove a byte-for-byte replay matches."""

        segment = self._job_segment(job_id)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "job_segment": segment,
            "manifest_ref": self._manifest_ref(manifest_ref),
            "manifest_revision": self._identity(manifest_revision, "manifest revision"),
            "assets": self._assets_for_commit(assets),
        }
        payload["state_hash"] = _state_hash(payload)
        path = self._journal_path(segment)
        if path.exists():
            existing = self._read_payload(path, expected_segment=segment)
            if existing != payload:
                raise XiaohongshuStagingJournalError("xiaohongshu staging journal drifted")
            return self._checkpoint(segment, str(payload["state_hash"]))
        self._journal_root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, payload)
        return self._checkpoint(segment, str(payload["state_hash"]))

    def restore(
        self, *, job_id: str, manifest_ref: str, manifest_revision: str,
        receipt: Mapping[str, object]
    ) -> tuple[XiaohongshuStagedAsset, ...]:
        """Restore transient real paths only after receipt and journal proof agree."""

        segment = self._job_segment(job_id)
        clean_receipt = self._receipt(receipt)
        expected_ref = self._checkpoint_ref(segment)
        if clean_receipt.output_ref != expected_ref:
            raise XiaohongshuStagingJournalError("xiaohongshu staging receipt output is invalid")
        payload = self._read_payload(self._journal_path(segment), expected_segment=segment)
        if (
            payload.get("manifest_ref") != self._manifest_ref(manifest_ref)
            or payload.get("manifest_revision")
            != self._identity(manifest_revision, "manifest revision")
        ):
            raise XiaohongshuStagingJournalError("xiaohongshu staging manifest binding drifted")
        if clean_receipt.state_hash != payload["state_hash"]:
            raise XiaohongshuStagingJournalError("xiaohongshu staging receipt state drifted")
        restored: list[XiaohongshuStagedAsset] = []
        for record in payload["assets"]:
            assert isinstance(record, Mapping)  # established by _read_payload
            relative = record["relative_path"]
            kind = str(record["kind"])
            if relative is None:
                if kind != "text" or record["byte_count"] != 0:
                    raise XiaohongshuStagingJournalError("xiaohongshu staging journal asset is invalid")
                staged_path = None
            else:
                staged = self._resolve_staged_path(str(relative))
                if not staged.exists() or not staged.is_file() or staged.is_symlink():
                    raise XiaohongshuStagingJournalError("xiaohongshu staged asset is unavailable")
                if staged.stat().st_size != record["byte_count"]:
                    raise XiaohongshuStagingJournalError("xiaohongshu staged asset size drifted")
                if _sha256_file(staged) != record["content_sha256"]:
                    raise XiaohongshuStagingJournalError("xiaohongshu staged asset content drifted")
                staged_path = str(staged)
            restored.append(XiaohongshuStagedAsset(
                asset_id=str(record["asset_id"]), ordinal=int(record["ordinal"]), kind=kind,
                media_type=str(record["media_type"]), staged_path=staged_path,
                byte_count=int(record["byte_count"]),
            ))
        return tuple(restored)

    def checkpoint(self, *, job_id: str) -> XiaohongshuStagingCheckpoint:
        """Return the public CRP reference and state hash of an existing journal."""

        segment = self._job_segment(job_id)
        payload = self._read_payload(self._journal_path(segment), expected_segment=segment)
        return self._checkpoint(segment, str(payload["state_hash"]))

    def _assets_for_commit(self, assets: tuple[XiaohongshuStagedAsset, ...]) -> list[dict[str, object]]:
        if not isinstance(assets, tuple) or not assets:
            raise XiaohongshuStagingJournalError("xiaohongshu staged assets are invalid")
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for expected_ordinal, asset in enumerate(assets):
            if not isinstance(asset, XiaohongshuStagedAsset):
                raise XiaohongshuStagingJournalError("xiaohongshu staged assets are invalid")
            asset_id = self._identity(asset.asset_id, "asset id")
            if asset_id in seen or asset.ordinal != expected_ordinal:
                raise XiaohongshuStagingJournalError("xiaohongshu staged asset order is invalid")
            seen.add(asset_id)
            kind = asset.kind
            if kind not in _MEDIA_TYPES or asset.media_type not in _MEDIA_TYPES[kind]:
                raise XiaohongshuStagingJournalError("xiaohongshu staged asset media type is invalid")
            if not isinstance(asset.byte_count, int) or isinstance(asset.byte_count, bool) or asset.byte_count < 0:
                raise XiaohongshuStagingJournalError("xiaohongshu staged asset size is invalid")
            if kind == "text":
                if asset.staged_path is not None or asset.byte_count != 0:
                    raise XiaohongshuStagingJournalError("xiaohongshu text staged asset is invalid")
                relative: str | None = None
                content_sha256: str | None = None
            else:
                if asset.byte_count < 1 or not isinstance(asset.staged_path, str):
                    raise XiaohongshuStagingJournalError("xiaohongshu staged asset is invalid")
                staged = self._resolve_staged_path(asset.staged_path)
                if not staged.is_file() or staged.is_symlink() or staged.stat().st_size != asset.byte_count:
                    raise XiaohongshuStagingJournalError("xiaohongshu staged asset is unavailable")
                relative = staged.relative_to(self._media_root.resolve(strict=False)).as_posix()
                content_sha256 = _sha256_file(staged)
            result.append({"asset_id": asset_id, "ordinal": expected_ordinal, "kind": kind,
                           "media_type": asset.media_type, "byte_count": asset.byte_count,
                           "content_sha256": content_sha256,
                           "relative_path": relative})
        return result

    def _read_payload(self, path: Path, *, expected_segment: str) -> dict[str, object]:
        if not path.is_file() or path.is_symlink():
            raise XiaohongshuStagingJournalError("xiaohongshu staging journal is unavailable")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise XiaohongshuStagingJournalError("xiaohongshu staging journal is unreadable") from error
        if not isinstance(raw, Mapping) or set(raw) != _JOURNAL_FIELDS:
            raise XiaohongshuStagingJournalError("xiaohongshu staging journal fields are invalid")
        payload = dict(raw)
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("job_segment") != expected_segment:
            raise XiaohongshuStagingJournalError("xiaohongshu staging journal binding is invalid")
        self._manifest_ref(payload.get("manifest_ref"))
        self._identity(payload.get("manifest_revision"), "manifest revision")
        records = payload.get("assets")
        if not isinstance(records, list) or not records:
            raise XiaohongshuStagingJournalError("xiaohongshu staging journal assets are invalid")
        normalized = self._records(records)
        canonical = {key: payload[key] for key in _JOURNAL_FIELDS if key != "state_hash"}
        expected_hash = _state_hash(canonical)
        if payload.get("state_hash") != expected_hash:
            raise XiaohongshuStagingJournalError("xiaohongshu staging journal state drifted")
        payload["assets"] = normalized
        return payload

    def _records(self, records: list[object]) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        ids: set[str] = set()
        for ordinal, record in enumerate(records):
            if not isinstance(record, Mapping) or set(record) != _ASSET_FIELDS:
                raise XiaohongshuStagingJournalError("xiaohongshu staging journal asset fields are invalid")
            asset_id = self._identity(record.get("asset_id"), "asset id")
            kind, media_type = record.get("kind"), record.get("media_type")
            byte_count, relative = record.get("byte_count"), record.get("relative_path")
            content_sha256 = record.get("content_sha256")
            if asset_id in ids or record.get("ordinal") != ordinal or kind not in _MEDIA_TYPES or media_type not in _MEDIA_TYPES[kind]:
                raise XiaohongshuStagingJournalError("xiaohongshu staging journal asset is invalid")
            if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
                raise XiaohongshuStagingJournalError("xiaohongshu staging journal asset size is invalid")
            if kind == "text":
                if relative is not None or content_sha256 is not None or byte_count != 0:
                    raise XiaohongshuStagingJournalError("xiaohongshu staging journal asset is invalid")
            elif (
                not isinstance(relative, str)
                or byte_count < 1
                or not isinstance(content_sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", content_sha256) is None
            ):
                raise XiaohongshuStagingJournalError("xiaohongshu staging journal asset is invalid")
            elif self._relative_path(relative) != relative:
                raise XiaohongshuStagingJournalError("xiaohongshu staging journal path is invalid")
            ids.add(asset_id)
            normalized.append({"asset_id": asset_id, "ordinal": ordinal, "kind": kind,
                               "media_type": media_type, "byte_count": byte_count,
                               "content_sha256": content_sha256,
                               "relative_path": relative})
        return normalized

    def _resolve_staged_path(self, value: str) -> Path:
        raw = Path(value)
        if raw.is_absolute():
            candidate = raw
        else:
            candidate = self._media_root / self._relative_path(value)
        root = self._media_root.resolve(strict=False)
        try:
            relative = candidate.relative_to(root)
        except ValueError as error:
            raise XiaohongshuStagingJournalError("xiaohongshu staged path escaped its root") from error
        checked = root
        for part in relative.parts:
            checked = checked / part
            if checked.is_symlink():
                raise XiaohongshuStagingJournalError("xiaohongshu staged path is a symlink")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise XiaohongshuStagingJournalError("xiaohongshu staged path escaped its root")
        return resolved

    @staticmethod
    def _relative_path(value: object) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise XiaohongshuStagingJournalError("xiaohongshu staged path is invalid")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value.startswith("/"):
            raise XiaohongshuStagingJournalError("xiaohongshu staged path is invalid")
        return value

    @staticmethod
    def _identity(value: object, name: str) -> str:
        if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
            raise XiaohongshuStagingJournalError(f"xiaohongshu staging {name} is invalid")
        return value

    @staticmethod
    def _manifest_ref(value: object) -> str:
        if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None or "/source-manifests/" not in value:
            raise XiaohongshuStagingJournalError("xiaohongshu staging manifest reference is invalid")
        return value

    def _job_segment(self, job_id: str) -> str:
        return media_job_uri_segment(self._identity(job_id, "job id"))

    def _journal_path(self, segment: str) -> Path:
        return self._journal_root / f"{segment}.json"

    def _checkpoint_ref(self, segment: str) -> str:
        return f"crp://{self._namespace}/jobs/{segment}/staging/xiaohongshu"

    def _checkpoint(self, segment: str, state_hash: str) -> XiaohongshuStagingCheckpoint:
        if _STATE_HASH.fullmatch(state_hash) is None:
            raise XiaohongshuStagingJournalError("xiaohongshu staging state hash is invalid")
        return XiaohongshuStagingCheckpoint(self._checkpoint_ref(segment), state_hash)

    @staticmethod
    def _receipt(value: Mapping[str, object]) -> XiaohongshuStagingCheckpoint:
        if not isinstance(value, Mapping) or set(value) != {"output_ref", "state_hash"}:
            raise XiaohongshuStagingJournalError("xiaohongshu staging receipt fields are invalid")
        output_ref, state_hash = value.get("output_ref"), value.get("state_hash")
        if not isinstance(output_ref, str) or _CRP_REF.fullmatch(output_ref) is None or not isinstance(state_hash, str) or _STATE_HASH.fullmatch(state_hash) is None:
            raise XiaohongshuStagingJournalError("xiaohongshu staging receipt is invalid")
        return XiaohongshuStagingCheckpoint(output_ref, state_hash)

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _state_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
