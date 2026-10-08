"""The first real, subtitle-first Media Hands provider vertical.

The provider deliberately only understands the frozen, project-scoped Source
Manifest and writes its result through the existing Document authority.  It
does not accept a URL, Cookie, local path, yt-dlp option or arbitrary output
destination. Missing official subtitles use the governed frozen-Manifest audio
staging and local ASR recipe when both production adapters are available.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time

from backend.api.bilibili_audio_fetcher import (
    BilibiliAnonymousAudioFetcher,
    BinaryDownloadPort,
)
from backend.api.governed_local_asr import GovernedLocalAsrRunner
from backend.api.bilibili_subtitle_provider import (
    BilibiliOfficialSubtitleProvider,
    TextNetworkPort,
)
from core.document_engine import DocumentDraft, DocumentRepositoryPort
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.media_hands import MediaOperationReceipt, MediaOperationRequest
from core.source_processing import SourceManifestArtifactRepository


_REQUIRED_BUDGETS = frozenset(
    {
        "max_download_bytes",
        "max_media_cpu_ms",
        "max_asr_audio_ms",
        "max_vision_frames",
        "max_model_input_tokens",
        "max_model_output_tokens",
        "max_wall_ms",
    }
)


class BilibiliSubtitleMediaOperationError(ValueError):
    """Stable provider failure, suitable for a Job receipt/error projection."""


@dataclass(frozen=True, slots=True)
class _BudgetedTextNetwork:
    """Builds each request with the Job's remaining byte and wall budget."""

    factory: Callable[[int, float, Callable[[], None] | None], TextNetworkPort]
    max_download_bytes: int
    deadline: float
    monotonic: Callable[[], float]
    control_check: Callable[[], None] | None
    _used: list[int]
    _control_error: list[BaseException | None]

    def fetch_text(self, url: str) -> str:
        self._checkpoint()
        remaining_bytes = self.max_download_bytes - self._used[0]
        remaining_seconds = self.deadline - self.monotonic()
        if remaining_bytes <= 0:
            raise BilibiliSubtitleMediaOperationError("subtitle_download_budget_exceeded")
        if remaining_seconds <= 0:
            raise BilibiliSubtitleMediaOperationError("subtitle_wall_budget_exceeded")
        result = self.factory(
            remaining_bytes,
            remaining_seconds,
            self._guarded_checkpoint if self.control_check is not None else None,
        ).fetch_text(url)
        self._checkpoint()
        try:
            size = len(result.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as error:  # pragma: no cover - defensive boundary
            raise BilibiliSubtitleMediaOperationError("subtitle_payload_invalid") from error
        self._used[0] += size
        if self._used[0] > self.max_download_bytes:
            raise BilibiliSubtitleMediaOperationError("subtitle_download_budget_exceeded")
        return result

    def _guarded_checkpoint(self) -> None:
        try:
            self._checkpoint()
        except BaseException as error:
            self._control_error[0] = error
            raise

    def _checkpoint(self) -> None:
        if self.control_check is not None:
            self.control_check()


@dataclass(frozen=True, slots=True)
class BilibiliSubtitleMediaOperationProvider:
    """Persist one official Bilibili transcript as the canonical Document output."""

    artifacts: SourceManifestArtifactRepository
    documents: DocumentRepositoryPort
    network_factory: Callable[[int, float, Callable[[], None] | None], TextNetworkPort]
    now: Callable[[], str]
    monotonic: Callable[[], float] = time.monotonic
    binary_network: BinaryDownloadPort | None = None
    local_asr: GovernedLocalAsrRunner | None = None

    provider_id = "bilibili-official-subtitle"
    provider_revision = "bilibili-official-subtitle-r1"
    supported_platforms = frozenset({"bilibili"})
    supports_durable_recipe_resume = True

    def execute(self, request: MediaOperationRequest) -> MediaOperationReceipt:
        self._validate_request(request)
        artifact = self.artifacts.resolve_source_ref(
            source_ref=request.manifest_ref,
            project_id=request.permission_snapshot.project_id,
        )
        if (
            artifact.revision != request.manifest_revision
            or artifact.manifest.source_id != request.source_id
            or artifact.public_ref != request.manifest_ref
        ):
            raise BilibiliSubtitleMediaOperationError("frozen_manifest_drift")
        if artifact.manifest.permission.decision != "granted":
            raise BilibiliSubtitleMediaOperationError("frozen_manifest_permission_denied")

        start = self.monotonic()
        used = [0]
        control_error: list[BaseException | None] = [None]
        wall_seconds = request.budget["max_wall_ms"] / 1000
        network = _BudgetedTextNetwork(
            self.network_factory,
            max_download_bytes=request.budget["max_download_bytes"],
            deadline=start + wall_seconds,
            monotonic=self.monotonic,
            control_check=(request.control.checkpoint if request.control is not None else None),
            _used=used,
            _control_error=control_error,
        )
        outcome = BilibiliOfficialSubtitleProvider(network).resolve(artifact.manifest)
        if control_error[0] is not None:
            raise control_error[0]
        if used[0] > request.budget["max_download_bytes"]:
            raise BilibiliSubtitleMediaOperationError("subtitle_download_budget_exceeded")
        if _elapsed_ms(start, self.monotonic()) > request.budget["max_wall_ms"]:
            raise BilibiliSubtitleMediaOperationError("subtitle_wall_budget_exceeded")
        asr_audio_ms = 0
        transcript = outcome.transcript
        chunks = outcome.chunks
        document: dict[str, object] | None = None
        if not outcome.available:
            if self.binary_network is None or self.local_asr is None:
                reason = outcome.unavailable_reason or "official_subtitle_unavailable"
                raise BilibiliSubtitleMediaOperationError(
                    f"asr_fallback_unavailable:{reason}"
                )
            self.local_asr.assert_ready()
            remaining_download = request.budget["max_download_bytes"] - used[0]
            remaining_wall_before_download = request.budget["max_wall_ms"] - _elapsed_ms(
                start, self.monotonic()
            )
            fetcher = BilibiliAnonymousAudioFetcher(network, self.binary_network)
            audio_ref = (
                f"crp://{self.artifacts.namespace_id}/jobs/"
                f"{media_job_uri_segment(request.job_id)}/staging/source-audio"
            )
            fetch_input_hash = _state_hash({
                "step": "fetch_audio",
                "manifest_ref": request.manifest_ref,
                "manifest_revision": request.manifest_revision,
                "max_download_bytes": remaining_download,
            })
            recovered = (
                request.control.begin_recipe_step("fetch_audio", fetch_input_hash)
                if request.control is not None else None
            )
            if recovered is not None:
                audio = fetcher.restore(
                    job_id=request.job_id,
                    namespace_id=self.artifacts.namespace_id,
                    receipt=recovered,
                )
            else:
                try:
                    audio = fetcher.fetch(
                        artifact.manifest,
                        job_id=request.job_id,
                        max_download_bytes=remaining_download,
                        timeout_seconds=remaining_wall_before_download / 1000,
                        control_check=(request.control.checkpoint if request.control is not None else None),
                    )
                    if request.control is not None:
                        request.control.complete_recipe_step(
                            "fetch_audio", output_ref=audio_ref,
                            output_state_hash=_state_hash({
                                "output_ref": audio_ref,
                                "byte_count": audio.byte_count,
                                "media_type": audio.media_type,
                            }),
                            consumed={"download_octets": audio.byte_count},
                        )
                except Exception:
                    if request.control is not None:
                        request.control.mark_recipe_step_unknown("fetch_audio")
                    raise
            used[0] += audio.byte_count
            probe_input_hash = _state_hash({
                "step": "probe_audio", "audio_ref": audio_ref,
                "byte_count": audio.byte_count,
                "max_asr_audio_ms": request.budget["max_asr_audio_ms"],
            })
            recovered_probe = (
                request.control.begin_recipe_step("probe_audio", probe_input_hash)
                if request.control is not None else None
            )
            if recovered_probe is not None:
                probe_consumed = recovered_probe.get("consumed")
                duration_ms = (
                    probe_consumed.get("audio_milliseconds")
                    if isinstance(probe_consumed, Mapping) else None
                )
                if (
                    not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
                    or duration_ms < 1
                    or recovered_probe.get("output_ref") != audio_ref
                    or recovered_probe.get("output_state_hash") != _state_hash({
                        "audio_ref": audio_ref, "duration_ms": duration_ms,
                    })
                ):
                    raise BilibiliSubtitleMediaOperationError("probe_step_receipt_invalid")
            else:
                try:
                    duration_ms = self.local_asr.probe_duration(
                        Path(audio.path), max_audio_ms=request.budget["max_asr_audio_ms"],
                        control_check=(request.control.checkpoint if request.control is not None else None),
                    )
                    if request.control is not None:
                        request.control.complete_recipe_step(
                            "probe_audio", output_ref=audio_ref,
                            output_state_hash=_state_hash({
                                "audio_ref": audio_ref, "duration_ms": duration_ms,
                            }),
                            consumed={"audio_milliseconds": duration_ms},
                        )
                except Exception:
                    if request.control is not None:
                        request.control.mark_recipe_step_unknown("probe_audio")
                    raise
            remaining_wall = request.budget["max_wall_ms"] - _elapsed_ms(
                start, self.monotonic()
            )
            asr_input_hash = _state_hash({
                "step": "transcribe_document", "audio_ref": audio_ref,
                "duration_ms": duration_ms, "provider_revision": self.local_asr.provider_revision,
                "manifest_ref": request.manifest_ref,
                "manifest_revision": request.manifest_revision,
            })
            recovered_asr = (
                request.control.begin_recipe_step("transcribe_document", asr_input_hash)
                if request.control is not None else None
            )
            if recovered_asr is not None:
                document = self._restore_step_document(recovered_asr)
                recovered_consumed = recovered_asr.get("consumed")
                asr_audio_ms = (
                    recovered_consumed.get("audio_milliseconds")
                    if isinstance(recovered_consumed, Mapping) else 0
                )
                if (
                    not isinstance(asr_audio_ms, int) or isinstance(asr_audio_ms, bool)
                    or asr_audio_ms != duration_ms
                ):
                    raise BilibiliSubtitleMediaOperationError("asr_step_receipt_invalid")
            else:
                try:
                    asr = self.local_asr.transcribe_known_duration(
                        Path(audio.path), title=_manifest_title(artifact.manifest),
                        duration_ms=duration_ms, max_wall_ms=remaining_wall,
                        control_check=(request.control.checkpoint if request.control is not None else None),
                    )
                    transcript = asr.transcript
                    chunks = asr.chunks
                    asr_audio_ms = asr.audio_duration_ms
                    document = self._create_document(
                        request=request, transcript=transcript, chunks=chunks
                    )
                    if request.control is not None:
                        markdown_uri = _required_str(document, "markdown_uri")
                        request.control.complete_recipe_step(
                            "transcribe_document", output_ref=markdown_uri,
                            output_state_hash=_document_state_hash(document),
                            consumed={
                                "audio_milliseconds": asr_audio_ms,
                                "wall_milliseconds": asr.wall_ms,
                            },
                        )
                except Exception:
                    if request.control is not None:
                        request.control.mark_recipe_step_unknown("transcribe_document")
                    raise
        elapsed_ms = _elapsed_ms(start, self.monotonic())
        if used[0] > request.budget["max_download_bytes"]:
            raise BilibiliSubtitleMediaOperationError("subtitle_download_budget_exceeded")
        if elapsed_ms > request.budget["max_wall_ms"]:
            raise BilibiliSubtitleMediaOperationError("subtitle_wall_budget_exceeded")
        if request.control is not None:
            request.control.checkpoint()

        if document is None:
            document = self._create_document(
                request=request, transcript=transcript, chunks=chunks
            )
        if request.control is not None:
            request.control.checkpoint()
        document_id = _required_str(document, "id")
        markdown_uri = _required_str(document, "markdown_uri")
        if document.get("revision") != 1:
            raise BilibiliSubtitleMediaOperationError("document_output_revision_drift")
        consumed = {name: 0 for name in request.budget}
        consumed["max_download_bytes"] = used[0]
        consumed["max_asr_audio_ms"] = asr_audio_ms
        consumed["max_wall_ms"] = elapsed_ms
        if any(consumed[name] > request.budget[name] for name in consumed):
            raise BilibiliSubtitleMediaOperationError("subtitle_budget_exceeded")
        state = {
            "provider_id": self.provider_id,
            "provider_revision": self.provider_revision,
            "document_id": document_id,
            "manifest_ref": request.manifest_ref,
            "manifest_revision": request.manifest_revision,
            "consumed": consumed,
        }
        job_ref_id = media_job_uri_segment(request.job_id)
        return MediaOperationReceipt(
            output={
                "kind": "document",
                "uri": markdown_uri,
                "object_id": document_id,
                "published": True,
            },
            checkpoint={
                "resume_step": "execute_operation",
                "checkpoint_uri": f"crp://{self.artifacts.namespace_id}/jobs/{job_ref_id}/checkpoints/bilibili-official-subtitle-r1",
                "state_hash": "sha256:" + _stable_digest(state),
                "updated_at": self.now(),
            },
            consumed=consumed,
            execution_receipt_ref=(
                f"crp://{self.artifacts.namespace_id}/jobs/{job_ref_id}/receipts/bilibili-official-subtitle-r1"
            ),
        )

    def _create_document(
        self, *, request: MediaOperationRequest,
        transcript: Mapping[str, object] | None,
        chunks: tuple[Mapping[str, object], ...],
    ) -> dict[str, object]:
        markdown = _render_markdown(
            transcript=transcript, chunks=chunks,
            manifest_ref=request.manifest_ref,
            manifest_revision=request.manifest_revision,
        )
        return dict(self.documents.create_or_replay_generated(DocumentDraft(
            title=f"B站字幕：{_transcript_title(transcript)}",
            document_type="media_transcript", markdown=markdown,
            source_refs=({
                "source_id": request.source_id,
                "locator": request.manifest_ref,
                "quote": request.manifest_revision,
            },),
            project_id=request.permission_snapshot.project_id,
        )))

    def _restore_step_document(self, receipt: Mapping[str, object]) -> dict[str, object]:
        output_ref = receipt.get("output_ref")
        if not isinstance(output_ref, str) or "/documents/" not in output_ref or not output_ref.endswith(".md"):
            raise BilibiliSubtitleMediaOperationError("asr_step_receipt_invalid")
        document_id = output_ref.rsplit("/", 1)[-1][:-3]
        document = self.documents.read(document_id)
        if (
            not isinstance(document, Mapping)
            or document.get("revision") != 1
            or document.get("markdown_uri") != output_ref
            or receipt.get("output_state_hash") != _document_state_hash(document)
        ):
            raise BilibiliSubtitleMediaOperationError("asr_step_document_drift")
        return dict(document)

    @staticmethod
    def _validate_request(request: MediaOperationRequest) -> None:
        if request.operation != "analyze_source":
            raise BilibiliSubtitleMediaOperationError("unsupported_media_operation")
        if set(request.budget) != _REQUIRED_BUDGETS or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in request.budget.values()
        ):
            raise BilibiliSubtitleMediaOperationError("media_budget_invalid")
        if request.budget["max_download_bytes"] <= 0 or request.budget["max_wall_ms"] <= 0:
            raise BilibiliSubtitleMediaOperationError("media_budget_exhausted")


@dataclass(frozen=True, slots=True)
class DocumentBackedCanonicalMediaOutputVerifier:
    """Read-only proof that a media receipt points to its immutable r1 Document."""

    documents: DocumentRepositoryPort
    allowed_document_types: frozenset[str] = frozenset({"media_transcript"})

    def assert_output_committed(
        self, *, output: Mapping[str, object], request: MediaOperationRequest
    ) -> None:
        if output.get("kind") != "document" or output.get("published") is not True:
            raise BilibiliSubtitleMediaOperationError("canonical_media_output_invalid")
        document_id = _required_str(output, "object_id")
        document = self.documents.read(document_id)
        if document is None:
            raise BilibiliSubtitleMediaOperationError("canonical_media_document_missing")
        uri = _required_str(output, "uri")
        if (
            document.get("id") != document_id
            or document.get("project_id") != request.permission_snapshot.project_id
            or document.get("type") not in self.allowed_document_types
            or document.get("markdown_uri") != uri
        ):
            raise BilibiliSubtitleMediaOperationError("canonical_media_document_drift")
        expected_refs = [
            {
                "source_id": request.source_id,
                "locator": request.manifest_ref,
                "quote": request.manifest_revision,
            }
        ]
        revision_reader = getattr(self.documents, "revision", None)
        markdown_reader = getattr(self.documents, "markdown", None)
        if not callable(revision_reader) or not callable(markdown_reader):
            raise BilibiliSubtitleMediaOperationError("canonical_media_authority_unavailable")
        revision = revision_reader(document_id, 1)
        markdown = markdown_reader(document_id, revision=1)
        snapshot = revision.get("source_snapshot") if isinstance(revision, Mapping) else None
        if not isinstance(snapshot, Mapping) or snapshot.get("source_refs") != expected_refs:
            raise BilibiliSubtitleMediaOperationError("canonical_media_source_snapshot_drift")
        if (
            not isinstance(revision, Mapping)
            or revision.get("document_id") != document_id
            or revision.get("revision") != 1
            or revision.get("operation") != "create"
            or not isinstance(markdown, str)
            or revision.get("new_content_hash")
            != "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        ):
            raise BilibiliSubtitleMediaOperationError("canonical_media_r1_drift")


def _render_markdown(
    *,
    transcript: Mapping[str, object] | None,
    chunks: tuple[Mapping[str, object], ...],
    manifest_ref: str,
    manifest_revision: str,
) -> str:
    if not isinstance(transcript, Mapping):  # defended by BilibiliOfficialSubtitleProvider
        raise BilibiliSubtitleMediaOperationError("subtitle_transcript_invalid")
    title = _required_str(transcript, "title")
    language = _required_str(transcript, "language")
    segments = transcript.get("segments")
    if not isinstance(segments, list) or not segments or not chunks:
        raise BilibiliSubtitleMediaOperationError("subtitle_transcript_invalid")
    lines = [
        f"# {title}",
        "",
        "## 来源快照",
        f"- Manifest: `{manifest_ref}`",
        f"- Manifest revision: `{manifest_revision}`",
        f"- Transcript source: {_required_str(transcript, 'source')} ({language})",
        "",
        "## 字幕",
    ]
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise BilibiliSubtitleMediaOperationError("subtitle_transcript_invalid")
        lines.append(
            f"- [{_time_value(segment.get('start_seconds')):.3f}–{_time_value(segment.get('end_seconds')):.3f}] {_required_str(segment, 'text')}"
        )
    lines.extend(("", "## 稳定切片"))
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            raise BilibiliSubtitleMediaOperationError("subtitle_chunk_invalid")
        lines.append(
            f"- `{_required_str(chunk, 'chunk_id')}` [{_time_value(chunk.get('start')):.3f}–{_time_value(chunk.get('end')):.3f}] {_required_str(chunk, 'text')}"
        )
    return "\n".join(lines) + "\n"


def _transcript_title(transcript: Mapping[str, object] | None) -> str:
    if not isinstance(transcript, Mapping):
        raise BilibiliSubtitleMediaOperationError("subtitle_transcript_invalid")
    return _required_str(transcript, "title")


def _manifest_title(manifest) -> str:
    title = dict(manifest.metadata.entries).get("title")
    return title.strip() if isinstance(title, str) and title.strip() else "B站视频"


def _elapsed_ms(start: float, end: float) -> int:
    return max(0, int((end - start) * 1000))


def _state_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _document_state_hash(document: Mapping[str, object]) -> str:
    return _state_hash({
        "document_id": _required_str(document, "id"),
        "markdown_uri": _required_str(document, "markdown_uri"),
        "revision": document.get("revision"),
    })


def _stable_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _required_str(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise BilibiliSubtitleMediaOperationError(f"{name}_invalid")
    return item


def _time_value(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BilibiliSubtitleMediaOperationError("subtitle_timestamp_invalid")
    return float(value)
