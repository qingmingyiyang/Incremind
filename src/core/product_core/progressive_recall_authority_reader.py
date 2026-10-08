from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.product_core.progressive_recall_drilldown import (
    AuthorityEvidenceCandidate,
    EvidenceSourceRef,
    ProgressiveRecallDrilldownError,
)
from core.product_core.object_store_port import ProductObjectStorePort


_ELIGIBLE_TRUST = frozenset({"trusted", "user_confirmed", "system_generated"})
_READABLE_SOURCE_STATES = frozenset({"captured", "queued", "processing", "ready"})
_READABLE_DOCUMENT_STATES = frozenset({"draft", "published"})
_TERM_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass(slots=True)
class ObjectStoreProgressiveRecallAuthorityReader:
    """Reads current R2/R3 evidence through fresh R1 source references only."""

    object_store: ProductObjectStorePort

    def read_structured(
        self,
        *,
        project_id: str,
        series_ids: tuple[str, ...],
        allowed_source_refs: tuple[tuple[str, str], ...],
        query: str,
    ) -> Sequence[AuthorityEvidenceCandidate]:
        clean_project = _required_text(project_id, "project_id")
        allowed = _allowed_map(allowed_source_refs)
        if not allowed:
            return ()
        query_terms = _terms(query)
        source_cache: dict[str, Mapping[str, object] | None] = {}
        result: list[AuthorityEvidenceCandidate] = []
        for document in self.object_store.list("documents"):
            if (
                document.get("project_id") != clean_project
                or document.get("status") not in _READABLE_DOCUMENT_STATES
            ):
                continue
            document_id = _required_text(document.get("id"), "document id")
            revision = _positive_int(document.get("revision"), "document revision")
            document_hash = _hash_value(document.get("content_hash"))
            for block in _mapping_sequence(document.get("blocks")):
                refs = self._current_refs(
                    block.get("source_refs"),
                    allowed,
                    source_cache,
                )
                content = block.get("content")
                block_id = block.get("id")
                if not refs or not isinstance(content, str) or not content.strip():
                    continue
                if not isinstance(block_id, str) or not block_id:
                    continue
                result.append(
                    AuthorityEvidenceCandidate(
                        layer="r2_structured_content",
                        object_type="document_block",
                        object_id=f"{document_id}#{block_id}",
                        revision_identity=f"document:{document_id}:r{revision}:{document_hash}",
                        content=content,
                        content_hash=_sha256(content),
                        source_refs=refs,
                        series_id=_series_id(document, series_ids),
                        relevance_score=_relevance(query_terms, content),
                    )
                )
        for structure in self.object_store.list("source_structures"):
            if structure.get("status") != "completed":
                continue
            source_id = structure.get("source_id")
            if not isinstance(source_id, str) or source_id not in allowed:
                continue
            if source_id not in source_cache:
                source_cache[source_id] = self._current_source(source_id)
            current_source = source_cache[source_id]
            if (
                current_source is None
                or structure.get("id") != _current_structure_id(current_source)
            ):
                continue
            refs = self._current_refs(
                [{"source_id": source_id, "locator": locator} for locator in allowed[source_id]],
                allowed,
                source_cache,
            )
            content = _first_text(
                structure.get("structured_body"),
                structure.get("summary"),
            )
            structure_id = structure.get("id")
            if not refs or not content or not isinstance(structure_id, str):
                continue
            result.append(
                AuthorityEvidenceCandidate(
                    layer="r2_structured_content",
                    object_type="source_structure",
                    object_id=structure_id,
                    revision_identity=f"source-structure:{structure_id}:{_sha256(content)}",
                    content=content,
                    content_hash=_sha256(content),
                    source_refs=refs,
                    series_id=_series_from_structure(structure, series_ids),
                    relevance_score=_relevance(query_terms, content),
                )
            )
        for output in self.object_store.list("media_processing_outputs"):
            if output.get("status") != "completed" or output.get("output_kind") != "summary":
                continue
            source_id = output.get("source_id")
            if not isinstance(source_id, str) or source_id not in allowed:
                continue
            if source_id not in source_cache:
                source_cache[source_id] = self._current_source(source_id)
            current_source = source_cache[source_id]
            if (
                current_source is None
                or output.get("id") != _current_summary_output_id(current_source)
            ):
                continue
            refs = self._current_refs(
                [{"source_id": source_id, "locator": locator} for locator in allowed[source_id]],
                allowed,
                source_cache,
            )
            content = _first_text(output.get("markdown"), output.get("text"))
            output_id = output.get("id")
            if not refs or not content or not isinstance(output_id, str):
                continue
            result.append(
                AuthorityEvidenceCandidate(
                    layer="r2_structured_content",
                    object_type="media_structure",
                    object_id=output_id,
                    revision_identity=f"media-structure:{output_id}:{_sha256(content)}",
                    content=content,
                    content_hash=_sha256(content),
                    source_refs=refs,
                    series_id=None,
                    relevance_score=_relevance(query_terms, content),
                )
            )
        return tuple(result)

    def read_source_evidence(
        self,
        *,
        project_id: str,
        source_refs: tuple[EvidenceSourceRef, ...],
        allowed_source_refs: tuple[tuple[str, str], ...],
        query: str,
    ) -> Sequence[AuthorityEvidenceCandidate]:
        _required_text(project_id, "project_id")
        allowed = _allowed_map(allowed_source_refs)
        expected = {
            (ref.source_id, ref.locator): ref.source_content_hash
            for ref in source_refs
        }
        query_terms = _terms(query)
        targets = tuple(expected) if expected else tuple(
            (source_id, locator)
            for source_id, locators in allowed.items()
            for locator in locators
        )
        result: list[AuthorityEvidenceCandidate] = []
        for source_id, locator in targets:
            if source_id not in allowed or locator not in allowed[source_id]:
                continue
            source = self._current_source(source_id)
            if source is None:
                continue
            source_hash = _hash_value(source.get("content_hash"))
            expected_hash = expected.get((source_id, locator))
            if expected_hash is not None and expected_hash != source_hash:
                continue
            ref = EvidenceSourceRef(source_id, locator, source_hash)
            candidates = self._source_bodies(source_id, source, ref, query_terms)
            current = self._current_source(source_id)
            if (
                current is None
                or _hash_value(current.get("content_hash")) != source_hash
            ):
                continue
            result.extend(candidates)
        return tuple(result)

    def _source_bodies(
        self,
        source_id: str,
        source: Mapping[str, object],
        ref: EvidenceSourceRef,
        query_terms: frozenset[str],
    ) -> tuple[AuthorityEvidenceCandidate, ...]:
        bodies: list[AuthorityEvidenceCandidate] = []
        current_read_id = _current_content_read_id(source)
        for read in self.object_store.list("source_content_reads"):
            if (
                read.get("source_id") != source_id
                or read.get("status") != "completed"
                or read.get("id") != current_read_id
            ):
                continue
            content = _verified_record_text(read, require_hash=True)
            read_id = read.get("id")
            if isinstance(content, str) and content.strip() and isinstance(read_id, str):
                bodies.append(_r3_candidate("source_content_read", read_id, content, ref, query_terms))
        for output in self.object_store.list("media_processing_outputs"):
            current_transcript_id = _current_transcript_output_id(source)
            if (
                output.get("source_id") != source_id
                or output.get("status") != "completed"
                or output.get("output_kind") != "transcript"
                or output.get("id") != current_transcript_id
            ):
                continue
            content = _verified_record_text(output, require_hash=False)
            output_id = output.get("id")
            if isinstance(content, str) and content.strip() and isinstance(output_id, str):
                bodies.append(_r3_candidate("source_content_read", output_id, content, ref, query_terms))
        metadata = source.get("metadata")
        inline = metadata.get("content") if isinstance(metadata, Mapping) else None
        if (
            not bodies
            and source.get("capture_mode") == "inline"
            and isinstance(inline, str)
            and inline.strip()
        ):
            bodies.append(_r3_candidate("inline_source", source_id, inline, ref, query_terms))
        return tuple(bodies)

    def _current_refs(
        self,
        raw_refs: object,
        allowed: Mapping[str, tuple[str, ...]],
        source_cache: dict[str, Mapping[str, object] | None],
    ) -> tuple[EvidenceSourceRef, ...]:
        refs: list[EvidenceSourceRef] = []
        for payload in _mapping_sequence(raw_refs):
            source_id = payload.get("source_id")
            locator = payload.get("locator")
            if (
                not isinstance(source_id, str)
                or not isinstance(locator, str)
                or source_id not in allowed
                or locator not in allowed[source_id]
            ):
                continue
            if source_id not in source_cache:
                source_cache[source_id] = self._current_source(source_id)
            source = source_cache[source_id]
            if source is None:
                continue
            ref = EvidenceSourceRef(
                source_id=source_id,
                locator=locator,
                source_content_hash=_hash_value(source.get("content_hash")),
            )
            if ref not in refs:
                refs.append(ref)
        return tuple(refs)

    def _current_source(self, source_id: str) -> Mapping[str, object] | None:
        source = self.object_store.read("sources", source_id)
        if source is None:
            return None
        if (
            source.get("trust_status") not in _ELIGIBLE_TRUST
            or source.get("processing_state") not in _READABLE_SOURCE_STATES
        ):
            return None
        if source.get("identity_method") == "workspace_confirmation":
            # EvidenceSourceRef and the drilldown validator still require a
            # SHA-256 source fingerprint. A revision is not interchangeable
            # with a content digest, so refuse this source until that shared
            # evidence contract supports revision-bound references.
            raise ProgressiveRecallDrilldownError(
                "source_revision_evidence_unsupported: "
                f"Source {source_id} requires revision-bound recall evidence"
            )
        try:
            source_hash = _hash_value(source.get("content_hash"))
        except ProgressiveRecallDrilldownError:
            return None
        metadata = source.get("metadata")
        inline = metadata.get("content") if isinstance(metadata, Mapping) else None
        if (
            source.get("capture_mode") == "inline"
            and isinstance(inline, str)
            and _sha256(inline) != source_hash
        ):
            return None
        return source


def _r3_candidate(
    object_type: str,
    object_id: str,
    content: str,
    ref: EvidenceSourceRef,
    query_terms: frozenset[str],
) -> AuthorityEvidenceCandidate:
    digest = _sha256(content)
    return AuthorityEvidenceCandidate(
        layer="r3_source_evidence",
        object_type=object_type,
        object_id=object_id,
        revision_identity=f"{object_type}:{object_id}:{digest}",
        content=content,
        content_hash=digest,
        source_refs=(ref,),
        relevance_score=_relevance(query_terms, content),
    )


def _allowed_map(
    refs: tuple[tuple[str, str], ...],
) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for source_id, locator in refs:
        clean_source = _required_text(source_id, "source_id")
        clean_locator = _safe_locator(locator)
        values.setdefault(clean_source, [])
        if clean_locator not in values[clean_source]:
            values[clean_source].append(clean_locator)
    return {key: tuple(value) for key, value in values.items()}


def _series_id(
    payload: Mapping[str, object],
    allowed: tuple[str, ...],
) -> str | None:
    value = payload.get("series_id")
    return value if isinstance(value, str) and value in allowed else None


def _series_from_structure(
    payload: Mapping[str, object],
    allowed: tuple[str, ...],
) -> str | None:
    for key in ("series_id", "series_candidate"):
        value = payload.get(key)
        if isinstance(value, str) and value in allowed:
            return value
    return None


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _first_text(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _current_content_read_id(source: Mapping[str, object]) -> str | None:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    state = metadata.get("content_read")
    if not isinstance(state, Mapping) or state.get("status") != "completed":
        return None
    read_ref = state.get("read_ref")
    marker = "/source-content-reads/"
    if not isinstance(read_ref, str) or marker not in read_ref:
        return None
    value = read_ref.rsplit(marker, 1)[-1].removesuffix(".json")
    return value or None


def _current_transcript_output_id(source: Mapping[str, object]) -> str | None:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    for key in ("audio_transcription", "audio_track_extraction"):
        state = metadata.get(key)
        if not isinstance(state, Mapping) or state.get("asr_state") != "completed":
            continue
        value = state.get("transcript_output_id")
        if isinstance(value, str) and value:
            return value
    return None


def _current_structure_id(source: Mapping[str, object]) -> str | None:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    state = metadata.get("content_structure")
    if not isinstance(state, Mapping) or state.get("status") != "completed":
        return None
    value = state.get("structure_id")
    return value if isinstance(value, str) and value else None


def _current_summary_output_id(source: Mapping[str, object]) -> str | None:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    for key in ("audio_transcription", "audio_track_extraction"):
        state = metadata.get(key)
        if not isinstance(state, Mapping) or state.get("summary_state") != "completed":
            continue
        value = state.get("summary_output_id")
        if isinstance(value, str) and value:
            return value
    return None


def _verified_record_text(
    record: Mapping[str, object],
    *,
    require_hash: bool,
) -> str | None:
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    encoded = text.encode("utf-8")
    if record.get("char_count") != len(text) or record.get("byte_count") != len(encoded):
        return None
    expected = record.get("text_sha256")
    if require_hash and expected != hashlib.sha256(encoded).hexdigest():
        return None
    if expected is not None and expected != hashlib.sha256(encoded).hexdigest():
        return None
    return text


def _hash_value(value: object) -> str:
    if isinstance(value, str) and value.startswith("sha256:"):
        value = value[7:]
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProgressiveRecallDrilldownError("content hash must be SHA-256")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProgressiveRecallDrilldownError(f"{field} must be positive")
    return value


def _safe_locator(value: object) -> str:
    locator = _required_text(value, "locator")
    if (
        locator.startswith(("/", "\\"))
        or "\\" in locator
        or (len(locator) > 2 and locator[1] == ":")
    ):
        raise ProgressiveRecallDrilldownError("locator must be platform neutral")
    return locator


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProgressiveRecallDrilldownError(f"{field} is required")
    return value.strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _terms(value: str) -> frozenset[str]:
    return frozenset(
        _TERM_PATTERN.findall(unicodedata.normalize("NFKC", value).casefold())
    )


def _relevance(query_terms: frozenset[str], content: str) -> float:
    if not query_terms:
        return 0.0
    content_terms = _terms(content)
    return round(len(query_terms & content_terms) / len(query_terms), 6)
