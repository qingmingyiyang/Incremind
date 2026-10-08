"""Durable, non-authoritative per-asset analysis checkpoints for Xiaohongshu.

This journal is intentionally only a recovery aid for a Media Hands recipe.
It never replaces Source, Job, or Document authority, and stores no locator,
filesystem path, credential, or download accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from core.job_runner.media_execution_receipt import media_job_uri_segment


SCHEMA_VERSION = "1.0.0"
_FIELDS = frozenset({
    "schema_version", "job_segment", "asset_segment", "manifest_ref", "manifest_revision",
    "source_id", "asset_id", "ordinal", "kind", "provider_revisions", "result", "consumed",
    "state_hash",
})
_CONSUMED_FIELDS = frozenset({"media_cpu_milliseconds", "audio_milliseconds", "vision_frames", "wall_milliseconds"})
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CRP_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._:/-]{0,319}$")
_STATE_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_TEXT_LIMIT = 200_000


class XiaohongshuAssetAnalysisJournalError(ValueError):
    """Stable error when a per-asset analysis checkpoint is untrustworthy."""


@dataclass(frozen=True, slots=True)
class XiaohongshuAssetAnalysisCheckpoint:
    output_ref: str
    state_hash: str


@dataclass(frozen=True, slots=True)
class XiaohongshuAssetAnalysisRecord:
    asset_id: str
    ordinal: int
    kind: str
    provider_revisions: dict[str, str]
    result: dict[str, object]
    consumed: dict[str, int]


class XiaohongshuAssetAnalysisJournal:
    """Atomically commit and restore one typed analysis result per Job asset."""

    def __init__(self, runtime_root: Path, *, namespace_id: str = "default") -> None:
        self._root = Path(runtime_root).resolve(strict=False) / ".rebuild-data" / "media-hands"
        self._journal_root = self._root / "xiaohongshu-asset-analysis-journals"
        self._namespace = _identity(namespace_id, "namespace")

    def commit(
        self, *, job_id: str, manifest_ref: str, manifest_revision: str, source_id: str,
        asset_id: str, ordinal: int, kind: str, provider_revisions: Mapping[str, str],
        result: Mapping[str, object], consumed: Mapping[str, int],
    ) -> XiaohongshuAssetAnalysisCheckpoint:
        segment, asset_segment = self._segments(job_id, asset_id)
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION, "job_segment": segment, "asset_segment": asset_segment,
            "manifest_ref": _manifest_ref(manifest_ref), "manifest_revision": _identity(manifest_revision, "manifest revision"),
            "source_id": _identity(source_id, "source id"), "asset_id": _identity(asset_id, "asset id"),
            "ordinal": _ordinal(ordinal), "kind": _kind(kind),
        }
        payload["provider_revisions"] = _providers(provider_revisions, str(payload["kind"]))
        payload["result"] = _result(result, str(payload["kind"]))
        payload["consumed"] = _consumed(consumed)
        payload["state_hash"] = _state_hash(payload)
        path = self._path(segment, asset_segment)
        if path.exists():
            if self._read(path, segment, asset_segment) != payload:
                raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal drifted")
            return self._checkpoint(segment, asset_segment, str(payload["state_hash"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, payload)
        return self._checkpoint(segment, asset_segment, str(payload["state_hash"]))

    def restore(
        self, *, job_id: str, manifest_ref: str, manifest_revision: str, source_id: str,
        asset_id: str, ordinal: int, kind: str, provider_revisions: Mapping[str, str],
        receipt: Mapping[str, object],
    ) -> XiaohongshuAssetAnalysisRecord:
        segment, asset_segment = self._segments(job_id, asset_id)
        cleaned = self._receipt(receipt)
        if cleaned.output_ref != self._checkpoint_ref(segment, asset_segment):
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis receipt output is invalid")
        payload = self._read(self._path(segment, asset_segment), segment, asset_segment)
        requested_kind = _kind(kind)
        expected = {
            "manifest_ref": _manifest_ref(manifest_ref), "manifest_revision": _identity(manifest_revision, "manifest revision"),
            "source_id": _identity(source_id, "source id"), "asset_id": _identity(asset_id, "asset id"),
            "ordinal": _ordinal(ordinal), "kind": requested_kind,
            "provider_revisions": _providers(provider_revisions, requested_kind),
        }
        if any(payload[name] != value for name, value in expected.items()):
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis binding drifted")
        if cleaned.state_hash != payload["state_hash"]:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis receipt state drifted")
        return XiaohongshuAssetAnalysisRecord(
            asset_id=str(payload["asset_id"]), ordinal=int(payload["ordinal"]), kind=str(payload["kind"]),
            provider_revisions=dict(payload["provider_revisions"]), result=dict(payload["result"]),
            consumed=dict(payload["consumed"]),
        )

    def checkpoint(self, *, job_id: str, asset_id: str) -> XiaohongshuAssetAnalysisCheckpoint:
        segment, asset_segment = self._segments(job_id, asset_id)
        payload = self._read(self._path(segment, asset_segment), segment, asset_segment)
        return self._checkpoint(segment, asset_segment, str(payload["state_hash"]))

    def _segments(self, job_id: str, asset_id: str) -> tuple[str, str]:
        return media_job_uri_segment(_identity(job_id, "job id")), _identity(asset_id, "asset id")

    def _path(self, segment: str, asset_segment: str) -> Path:
        return self._journal_root / segment / f"{asset_segment}.json"

    def _checkpoint_ref(self, segment: str, asset_segment: str) -> str:
        return f"crp://{self._namespace}/jobs/{segment}/asset-analysis/xiaohongshu/assets/{asset_segment}"

    def _checkpoint(self, segment: str, asset_segment: str, state_hash: str) -> XiaohongshuAssetAnalysisCheckpoint:
        if _STATE_HASH.fullmatch(state_hash) is None:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis state hash is invalid")
        return XiaohongshuAssetAnalysisCheckpoint(self._checkpoint_ref(segment, asset_segment), state_hash)

    def _read(self, path: Path, segment: str, asset_segment: str) -> dict[str, object]:
        if not path.is_file() or path.is_symlink():
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal is unavailable")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal is unreadable") from error
        if not isinstance(raw, Mapping) or set(raw) != _FIELDS:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal fields are invalid")
        payload = dict(raw)
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("job_segment") != segment or payload.get("asset_segment") != asset_segment:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal binding is invalid")
        _manifest_ref(payload.get("manifest_ref")); _identity(payload.get("manifest_revision"), "manifest revision")
        _identity(payload.get("source_id"), "source id"); asset_id = _identity(payload.get("asset_id"), "asset id")
        if asset_id != asset_segment or _ordinal(payload.get("ordinal")) != payload.get("ordinal"):
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal binding is invalid")
        kind = _kind(payload.get("kind"))
        payload["provider_revisions"] = _providers(payload.get("provider_revisions"), kind)
        payload["result"] = _result(payload.get("result"), kind)
        payload["consumed"] = _consumed(payload.get("consumed"))
        canonical = {name: payload[name] for name in _FIELDS if name != "state_hash"}
        if payload.get("state_hash") != _state_hash(canonical):
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis journal state drifted")
        return payload

    @staticmethod
    def _receipt(value: Mapping[str, object]) -> XiaohongshuAssetAnalysisCheckpoint:
        if not isinstance(value, Mapping) or set(value) != {"output_ref", "state_hash"}:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis receipt fields are invalid")
        output, state = value.get("output_ref"), value.get("state_hash")
        if not isinstance(output, str) or _CRP_REF.fullmatch(output) is None or not isinstance(state, str) or _STATE_HASH.fullmatch(state) is None:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis receipt is invalid")
        return XiaohongshuAssetAnalysisCheckpoint(output, state)

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
        raise XiaohongshuAssetAnalysisJournalError(f"xiaohongshu asset analysis {name} is invalid")
    return value


def _manifest_ref(value: object) -> str:
    if not isinstance(value, str) or _CRP_REF.fullmatch(value) is None or "/source-manifests/" not in value:
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis manifest reference is invalid")
    return value


def _ordinal(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis ordinal is invalid")
    return value


def _kind(value: object) -> str:
    if value not in {"image", "video", "text"}:
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis kind is invalid")
    return str(value)


def _providers(value: object, kind: str) -> dict[str, str]:
    expected = {"image": {"ocr"}, "video": {"asr", "ocr"}, "text": set()}[kind]
    if not isinstance(value, Mapping) or set(value) != expected:
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis providers are invalid")
    result: dict[str, str] = {}
    for name in sorted(expected):
        result[name] = _identity(value.get(name), f"{name} provider revision")
    return result


def _safe_text(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > _TEXT_LIMIT:
        raise XiaohongshuAssetAnalysisJournalError(f"xiaohongshu asset analysis {field} is invalid")
    return value


def _result(value: object, kind: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis result is invalid")
    if kind == "image":
        if set(value) != {"ocr_text"}:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis result is invalid")
        return {"ocr_text": _safe_text(value.get("ocr_text"), "OCR text")}
    if kind == "text":
        if set(value) != {"body"}:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis result is invalid")
        return {"body": _safe_text(value.get("body"), "text body")}
    if set(value) != {"transcript_segments", "frame_ocr"}:
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis result is invalid")
    segments, frames = value.get("transcript_segments"), value.get("frame_ocr")
    if not isinstance(segments, list) or not isinstance(frames, list):
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis result is invalid")
    restored_segments: list[dict[str, object]] = []
    previous_end = 0
    for row in segments:
        if not isinstance(row, Mapping) or set(row) != {"start_ms", "end_ms", "text"}:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis transcript is invalid")
        start, end = _ordinal(row.get("start_ms")), _ordinal(row.get("end_ms"))
        if end <= start or start < previous_end:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis transcript is invalid")
        restored_segments.append({"start_ms": start, "end_ms": end, "text": _safe_text(row.get("text"), "transcript text")})
        previous_end = end
    restored_frames: list[dict[str, object]] = []
    for ordinal, row in enumerate(frames):
        if not isinstance(row, Mapping) or set(row) != {"ordinal", "text"} or _ordinal(row.get("ordinal")) != ordinal:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis frame OCR is invalid")
        restored_frames.append({"ordinal": ordinal, "text": _safe_text(row.get("text"), "frame OCR text")})
    return {"transcript_segments": restored_segments, "frame_ocr": restored_frames}


def _consumed(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _CONSUMED_FIELDS:
        raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis consumption is invalid")
    result: dict[str, int] = {}
    for name in sorted(_CONSUMED_FIELDS):
        amount = value.get(name)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise XiaohongshuAssetAnalysisJournalError("xiaohongshu asset analysis consumption is invalid")
        result[name] = amount
    return result


def _state_hash(payload: Mapping[str, object]) -> str:
    value = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()
