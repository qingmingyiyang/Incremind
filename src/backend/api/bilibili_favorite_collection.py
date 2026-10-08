from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from backend.security.network_adapter import NetworkBoundaryError, SafeTextNetworkAdapter
from core.storage_provider import ObjectStorePort, ObjectStoreRevisionError


_BVID = re.compile(r"^BV[A-Za-z0-9]{10}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SPACE_FAVORITE_PATH = re.compile(r"^/(?P<mid>[0-9]+)/favlist/?$")
_MEDIA_LIST_PATH = re.compile(r"^/medialist/detail/ml(?P<fid>[0-9]+)/?$")
_SCHEMA_VERSION = "1.0.0"
_RESOLVER_REVISION = "bilibili-favorite-list-v1"
_PAGE_SIZE = 20
_MAX_PAGES = 250
_MAX_VIDEO_ITEMS = 5000


class BilibiliFavoriteCollectionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FavoriteCollectionNetworkPort(Protocol):
    def fetch_text(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class BilibiliFavoriteSnapshot:
    payload: Mapping[str, object]
    public_ref: str
    revision: str
    replayed: bool


class BilibiliFavoriteSnapshotRepository:
    collection = "bilibili_favorite_snapshots"

    def __init__(self, object_store: ObjectStorePort, *, namespace_id: str) -> None:
        _safe_id(namespace_id, "namespace_id")
        if getattr(object_store, "namespace_id", None) != namespace_id:
            raise BilibiliFavoriteCollectionError("snapshot_namespace_mismatch")
        self.object_store = object_store
        self.namespace_id = namespace_id

    def find(self, *, project_id: str, snapshot_id: str) -> BilibiliFavoriteSnapshot | None:
        _safe_id(project_id, "project_id")
        _safe_id(snapshot_id, "snapshot_id")
        payload = self.object_store.read(self.collection, snapshot_id)
        if payload is None:
            return None
        normalized = _validate_snapshot(payload)
        if normalized["project_id"] != project_id or normalized["snapshot_id"] != snapshot_id:
            raise BilibiliFavoriteCollectionError("snapshot_identity_mismatch")
        return BilibiliFavoriteSnapshot(
            payload=normalized,
            public_ref=self.public_ref(project_id=project_id, snapshot_id=snapshot_id),
            revision="r1",
            replayed=True,
        )

    def put(self, payload: Mapping[str, object]) -> BilibiliFavoriteSnapshot:
        normalized = _validate_snapshot(payload)
        snapshot_id = str(normalized["snapshot_id"])
        project_id = str(normalized["project_id"])
        try:
            revision = self.object_store.write(
                self.collection, snapshot_id, normalized, expected_revision=0
            )
        except ObjectStoreRevisionError as error:
            existing = self.find(project_id=project_id, snapshot_id=snapshot_id)
            if existing is None or dict(existing.payload) != normalized:
                raise BilibiliFavoriteCollectionError("snapshot_request_conflict") from error
            return existing
        if revision != 1:
            raise BilibiliFavoriteCollectionError("snapshot_revision_invalid")
        return BilibiliFavoriteSnapshot(
            payload=normalized,
            public_ref=self.public_ref(project_id=project_id, snapshot_id=snapshot_id),
            revision="r1",
            replayed=False,
        )

    def resolve_ref(self, *, project_id: str, snapshot_ref: str) -> BilibiliFavoriteSnapshot:
        prefix = (
            f"crp://{self.namespace_id}/bilibili-favorite-snapshots/"
            f"projects/{project_id}/"
        )
        if not isinstance(snapshot_ref, str) or not snapshot_ref.startswith(prefix):
            raise BilibiliFavoriteCollectionError("snapshot_ref_invalid")
        suffix = snapshot_ref.removeprefix(prefix)
        if not suffix.endswith("/r1"):
            raise BilibiliFavoriteCollectionError("snapshot_ref_invalid")
        snapshot_id = suffix[:-3]
        if "/" in snapshot_id:
            raise BilibiliFavoriteCollectionError("snapshot_ref_invalid")
        snapshot = self.find(project_id=project_id, snapshot_id=snapshot_id)
        if snapshot is None or snapshot.public_ref != snapshot_ref:
            raise BilibiliFavoriteCollectionError("snapshot_not_found")
        return snapshot

    def public_ref(self, *, project_id: str, snapshot_id: str) -> str:
        return (
            f"crp://{self.namespace_id}/bilibili-favorite-snapshots/"
            f"projects/{project_id}/{snapshot_id}/r1"
        )


@dataclass(frozen=True, slots=True)
class BilibiliFavoriteCollectionService:
    network: FavoriteCollectionNetworkPort
    snapshots: BilibiliFavoriteSnapshotRepository

    def resolve(
        self,
        *,
        source_url: str,
        project_id: str,
        snapshot_id: str,
        resolved_at: str,
    ) -> BilibiliFavoriteSnapshot:
        _safe_id(project_id, "project_id")
        _safe_id(snapshot_id, "snapshot_id")
        favorite_id, canonical_url = _favorite_identity(source_url)
        existing = self.snapshots.find(project_id=project_id, snapshot_id=snapshot_id)
        if existing is not None:
            if (
                existing.payload.get("source_url") != canonical_url
                or existing.payload.get("favorite_id") != favorite_id
            ):
                raise BilibiliFavoriteCollectionError("snapshot_request_conflict")
            return existing

        items: list[dict[str, object]] = []
        seen_bvids: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        skipped_counts = {"non_video": 0, "unavailable": 0, "duplicate": 0}
        collection_info: dict[str, object] | None = None
        raw_item_count = 0
        page_count = 0
        expected_total: int | None = None

        for page_number in range(1, _MAX_PAGES + 1):
            endpoint = "https://api.bilibili.com/x/v3/fav/resource/list?" + urlencode(
                {
                    "media_id": favorite_id,
                    "pn": page_number,
                    "ps": _PAGE_SIZE,
                    "order": "mtime",
                    "type": 0,
                    "tid": 0,
                    "platform": "web",
                }
            )
            try:
                raw = self.network.fetch_text(endpoint)
            except NetworkBoundaryError as error:
                raise BilibiliFavoriteCollectionError("network_denied") from error
            data = _decode_page(raw)
            page_count = page_number
            if collection_info is None:
                collection_info = _collection_info(data.get("info"), favorite_id=favorite_id)
                expected_total = _optional_non_negative_int(
                    (data.get("info") or {}).get("media_count")
                    if isinstance(data.get("info"), Mapping)
                    else None
                )
            media = data.get("medias")
            if media is None:
                media = []
            if not isinstance(media, list) or not all(isinstance(item, Mapping) for item in media):
                raise BilibiliFavoriteCollectionError("favorite_response_invalid")
            signature = tuple(
                str(item.get("id") or item.get("bvid") or item.get("title") or "")
                for item in media
            )
            if signature and signature in seen_pages:
                raise BilibiliFavoriteCollectionError("favorite_pagination_stalled")
            seen_pages.add(signature)
            raw_item_count += len(media)

            for entry in media:
                entry_type = entry.get("type")
                bvid = entry.get("bvid")
                if entry_type is not None and entry_type != 2:
                    skipped_counts["non_video"] += 1
                    continue
                if not isinstance(bvid, str) or _BVID.fullmatch(bvid) is None:
                    skipped_counts["unavailable"] += 1
                    continue
                if bvid in seen_bvids:
                    skipped_counts["duplicate"] += 1
                    continue
                if len(items) >= _MAX_VIDEO_ITEMS:
                    raise BilibiliFavoriteCollectionError("favorite_item_limit_exceeded")
                seen_bvids.add(bvid)
                upper = entry.get("upper") if isinstance(entry.get("upper"), Mapping) else {}
                items.append(
                    {
                        "ordinal": len(items),
                        "bvid": bvid,
                        "url": f"https://www.bilibili.com/video/{bvid}/",
                        "title": _bounded_text(entry.get("title"), 300) or bvid,
                        "uploader": _bounded_text(upper.get("name"), 100),
                        "duration_seconds": _non_negative_int(entry.get("duration")),
                    }
                )

            has_more = _has_more(data.get("has_more"))
            if has_more is False:
                break
            if has_more is None:
                if expected_total is not None and raw_item_count >= expected_total:
                    break
                if len(media) < _PAGE_SIZE:
                    break
        else:
            raise BilibiliFavoriteCollectionError("favorite_page_limit_exceeded")

        if collection_info is None:
            raise BilibiliFavoriteCollectionError("favorite_response_invalid")
        return self.snapshots.put(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": "bilibili_favorite_snapshot",
                "snapshot_id": snapshot_id,
                "project_id": project_id,
                "source_url": canonical_url,
                "favorite_id": favorite_id,
                "resolver_revision": _RESOLVER_REVISION,
                "resolved_at": resolved_at,
                "title": collection_info["title"],
                "owner": collection_info["owner"],
                "page_count": page_count,
                "raw_item_count": raw_item_count,
                "video_item_count": len(items),
                "skipped_counts": skipped_counts,
                "items": items,
            }
        )


def build_bilibili_favorite_collection_service(
    object_store: ObjectStorePort, *, namespace_id: str
) -> BilibiliFavoriteCollectionService:
    return BilibiliFavoriteCollectionService(
        network=SafeTextNetworkAdapter(
            allowed_hosts=("api.bilibili.com",),
            max_redirects=1,
            max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=20.0,
        ),
        snapshots=BilibiliFavoriteSnapshotRepository(
            object_store, namespace_id=namespace_id
        ),
    )


def _favorite_identity(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 4096:
        raise BilibiliFavoriteCollectionError("favorite_url_invalid")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError) as error:
        raise BilibiliFavoriteCollectionError("favorite_url_invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise BilibiliFavoriteCollectionError("favorite_url_invalid")
    if host == "space.bilibili.com":
        match = _SPACE_FAVORITE_PATH.fullmatch(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        values = query.get("fid", [])
        if match is None or len(values) != 1 or not values[0].isdigit() or int(values[0]) < 1:
            raise BilibiliFavoriteCollectionError("favorite_url_invalid")
        favorite_id = values[0]
    elif host in {"bilibili.com", "www.bilibili.com"}:
        match = _MEDIA_LIST_PATH.fullmatch(parsed.path)
        if match is None:
            raise BilibiliFavoriteCollectionError("favorite_url_invalid")
        favorite_id = match.group("fid")
    else:
        raise BilibiliFavoriteCollectionError("favorite_url_invalid")
    return favorite_id, f"https://www.bilibili.com/medialist/detail/ml{favorite_id}"


def _decode_page(raw: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise BilibiliFavoriteCollectionError("favorite_response_invalid") from error
    if not isinstance(payload, Mapping):
        raise BilibiliFavoriteCollectionError("favorite_response_invalid")
    if payload.get("code") == -403:
        raise BilibiliFavoriteCollectionError("private_favorite_requires_login")
    if payload.get("code") != 0 or not isinstance(payload.get("data"), Mapping):
        raise BilibiliFavoriteCollectionError("favorite_unavailable")
    return payload["data"]


def _collection_info(value: object, *, favorite_id: str) -> dict[str, object]:
    info = value if isinstance(value, Mapping) else {}
    upper = info.get("upper") if isinstance(info.get("upper"), Mapping) else {}
    return {
        "title": _bounded_text(info.get("title"), 300) or f"B站收藏夹 {favorite_id}",
        "owner": _bounded_text(upper.get("name"), 100),
    }


def _validate_snapshot(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "schema_version", "kind", "snapshot_id", "project_id", "source_url",
        "favorite_id", "resolver_revision", "resolved_at", "title", "owner",
        "page_count", "raw_item_count", "video_item_count", "skipped_counts", "items",
    }
    if set(value) != required or value.get("schema_version") != _SCHEMA_VERSION or value.get("kind") != "bilibili_favorite_snapshot":
        raise BilibiliFavoriteCollectionError("snapshot_fields_invalid")
    snapshot_id = _safe_id(value.get("snapshot_id"), "snapshot_id")
    project_id = _safe_id(value.get("project_id"), "project_id")
    favorite_id = value.get("favorite_id")
    if not isinstance(favorite_id, str) or not favorite_id.isdigit() or int(favorite_id) < 1:
        raise BilibiliFavoriteCollectionError("snapshot_favorite_id_invalid")
    items = value.get("items")
    if not isinstance(items, list):
        raise BilibiliFavoriteCollectionError("snapshot_items_invalid")
    for ordinal, item in enumerate(items):
        if not isinstance(item, Mapping) or item.get("ordinal") != ordinal:
            raise BilibiliFavoriteCollectionError("snapshot_items_invalid")
        if not isinstance(item.get("bvid"), str) or _BVID.fullmatch(item["bvid"]) is None:
            raise BilibiliFavoriteCollectionError("snapshot_items_invalid")
    return dict(value, snapshot_id=snapshot_id, project_id=project_id)


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise BilibiliFavoriteCollectionError(f"{label}_invalid")
    return value


def _has_more(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
        return bool(value)
    return None


def _optional_non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    return re.sub(r"\s+", " ", normalized).strip()[:limit]
