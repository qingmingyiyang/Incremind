from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from backend.security.network_adapter import NetworkBoundaryError, SafeTextNetworkAdapter
from core.source_processing import SourceManifest, SourceManifestCodec
from core.storage_provider import ObjectStorePort
from backend.api.source_resolution_evidence import (
    SourceResolutionEvidenceError,
    SourceResolutionEvidenceRepository,
)


_BVID = re.compile(r"^BV[A-Za-z0-9]{10}$")
_RESOLVER_REVISION = "bilibili-view-api-v1"
_NORMALIZER_REVISION = "bilibili-manifest-v1"


class BilibiliMetadataProviderError(ValueError):
    """Stable public error; raw network/API details never cross this boundary."""


class TextNetworkPort(Protocol):
    """Bounded transport that reduces network failures to NetworkBoundaryError."""

    def fetch_text(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class BilibiliMetadataEvidence:
    public_ref: str
    revision: str


class BilibiliMetadataEvidenceRepository:
    collection = "source_resolution_evidence"

    def __init__(self, object_store: ObjectStorePort, *, namespace_id: str) -> None:
        self._delegate = SourceResolutionEvidenceRepository(
            object_store, namespace_id=namespace_id
        )
        self.namespace_id = namespace_id

    def put(self, *, project_id: str, evidence_id: str, payload: Mapping[str, object]) -> BilibiliMetadataEvidence:
        try:
            evidence = self._delegate.put(
                project_id=project_id,
                evidence_id=evidence_id,
                kind="bilibili_metadata_resolution",
                payload=payload,
            )
        except SourceResolutionEvidenceError as error:
            raise BilibiliMetadataProviderError(str(error)) from error
        return BilibiliMetadataEvidence(evidence.public_ref, evidence.revision)


@dataclass(frozen=True, slots=True)
class BilibiliViewApiPlatformProvider:
    """Anonymous metadata-only provider; no yt-dlp, Cookie, download or legacy workspace writes."""

    network: TextNetworkPort
    evidence: BilibiliMetadataEvidenceRepository
    namespace_id: str

    def provide(self, text: str, *, project_id: str) -> SourceManifest:
        bvid, page, canonical_url = _canonical_source(text)
        endpoint = "https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid})
        try:
            raw = self.network.fetch_text(endpoint)
        except NetworkBoundaryError as error:
            raise BilibiliMetadataProviderError("network_denied") from error
        data = _decode_view_payload(raw, bvid=bvid)
        selected_page = _select_page(data, page)
        source_id = f"bili-{bvid}-p{page}"
        evidence_id = f"{source_id}--view-v1"
        normalized = _normalized_metadata(data, selected_page, bvid=bvid, page=page)
        proof = self.evidence.put(
            project_id=project_id,
            evidence_id=evidence_id,
            payload={
                "source_id": source_id,
                "input_identity": canonical_url,
                "resolver_revision": _RESOLVER_REVISION,
                "normalizer_revision": _NORMALIZER_REVISION,
                "metadata": normalized,
            },
        )
        source_ref = f"crp://{self.namespace_id}/sources/{source_id}"
        asset_id = f"video-{bvid}-p{page}"
        manifest = {
            "schema_version": "1.0.0",
            "source_id": source_id,
            "source_ref": source_ref,
            "platform": "bilibili",
            "input_identity": canonical_url,
            "resolver_revision": _RESOLVER_REVISION,
            "normalizer_revision": _NORMALIZER_REVISION,
            "content_kind": "video",
            "body": None,
            "metadata": normalized,
            "permission": {"decision": "unknown", "evidence_refs": [proof.public_ref]},
            "provenance_refs": [proof.public_ref],
            "assets": [
                {
                    "asset_id": asset_id,
                    "ordinal": 0,
                    "kind": "video",
                    "media_type": None,
                    "role": "primary",
                    "locator": canonical_url,
                    "source_ref": f"{source_ref}/assets/{asset_id}",
                    "relations": [],
                    "evidence_refs": [proof.public_ref],
                }
            ],
        }
        return SourceManifestCodec.decode(manifest)


def build_bilibili_view_api_platform_provider(
    object_store: ObjectStorePort, *, namespace_id: str
) -> BilibiliViewApiPlatformProvider:
    return BilibiliViewApiPlatformProvider(
        network=SafeTextNetworkAdapter(
            allowed_hosts=("api.bilibili.com",),
            max_redirects=1,
            max_response_bytes=512 * 1024,
            timeout_seconds=20.0,
        ),
        evidence=BilibiliMetadataEvidenceRepository(object_store, namespace_id=namespace_id),
        namespace_id=namespace_id,
    )


def _canonical_source(text: str) -> tuple[str, int, str]:
    matches = re.findall(r"https?://[^\s]+", text)
    if len(matches) != 1:
        raise BilibiliMetadataProviderError("invalid_source")
    try:
        parsed = urlsplit(matches[0])
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as error:
        raise BilibiliMetadataProviderError("invalid_source") from error
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise BilibiliMetadataProviderError("invalid_source")
    if host not in {"bilibili.com", "www.bilibili.com"}:
        raise BilibiliMetadataProviderError("invalid_source")
    match = re.search(r"/video/(BV[A-Za-z0-9]{10})(?:/|$)", parsed.path)
    if match is None or _BVID.fullmatch(match.group(1)) is None:
        raise BilibiliMetadataProviderError("invalid_source")
    query = parse_qs(parsed.query, keep_blank_values=True)
    raw_page = query.get("p", ["1"])
    if len(raw_page) != 1 or not raw_page[0].isdigit() or int(raw_page[0]) < 1:
        raise BilibiliMetadataProviderError("invalid_source")
    page = int(raw_page[0])
    bvid = match.group(1)
    canonical = f"https://www.bilibili.com/video/{bvid}/"
    if page > 1:
        canonical += f"?p={page}"
    return bvid, page, canonical


def _decode_view_payload(raw: str, *, bvid: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise BilibiliMetadataProviderError("metadata_unavailable") from error
    if not isinstance(payload, Mapping) or payload.get("code") != 0 or not isinstance(payload.get("data"), Mapping):
        raise BilibiliMetadataProviderError("metadata_unavailable")
    data = payload["data"]
    if _text(data.get("bvid")) != bvid:
        raise BilibiliMetadataProviderError("metadata_identity_mismatch")
    return data


def _select_page(data: Mapping[str, object], page: int) -> Mapping[str, object]:
    pages = data.get("pages")
    if not isinstance(pages, list):
        raise BilibiliMetadataProviderError("metadata_unavailable")
    for item in pages:
        if isinstance(item, Mapping) and item.get("page") == page:
            return item
    raise BilibiliMetadataProviderError("unsupported_source")


def _normalized_metadata(
    data: Mapping[str, object], page_data: Mapping[str, object], *, bvid: str, page: int
) -> dict[str, object]:
    owner = data.get("owner") if isinstance(data.get("owner"), Mapping) else {}
    published = data.get("pubdate")
    published_at = ""
    if isinstance(published, int) and not isinstance(published, bool) and published >= 0:
        try:
            published_at = datetime.fromtimestamp(published, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            published_at = ""
    cid = _positive_int(page_data.get("cid"))
    if cid == 0:
        raise BilibiliMetadataProviderError("metadata_unavailable")
    return {
        "bvid": bvid,
        "page": page,
        "cid": cid,
        "title": _bounded_text(page_data.get("part"), 300) or _bounded_text(data.get("title"), 300) or bvid,
        "series_title": _bounded_text(data.get("title"), 300),
        "uploader": _bounded_text(owner.get("name"), 100),
        "published_at": published_at,
        "duration_seconds": _positive_int(page_data.get("duration")) or _positive_int(data.get("duration")),
        "description": _bounded_text(data.get("desc"), 4000),
    }


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    return re.sub(r"\s+", " ", normalized).strip()[:limit]


def _positive_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
