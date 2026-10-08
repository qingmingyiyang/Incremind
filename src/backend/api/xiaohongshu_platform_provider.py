from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Protocol
from urllib.parse import urlsplit

from backend.api.source_resolution_evidence import (
    SourceResolutionEvidenceError,
    SourceResolutionEvidenceRepository,
)
from backend.api.xiaohongshu_controlled_credential_runtime import (
    SafeControlledCookieTextNetworkAdapter,
    XiaohongshuControlledCredentialRuntime,
    XiaohongshuControlledCredentialRuntimeError,
)
from backend.security.network_adapter import NetworkBoundaryError, SafeTextNetworkAdapter
from backend.security.network_egress_profile import (
    NetworkEgressProfile,
    loopback_proxy_for_capability,
)
from core.source_processing import ControlledCredentialBinding, SourceManifest, SourceManifestCodec
from core.storage_provider import ObjectStorePort


_NOTE_ID = re.compile(r"^[A-Za-z0-9]{24}$")
_RESOLVER_REVISION = "xhs-html-v1"
_NORMALIZER_REVISION = "xhs-manifest-v1"
_STATE_SCRIPT_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
_INITIAL_STATE_ASSIGNMENT = re.compile(
    r"\A\s*window\.__INITIAL_STATE__\s*=\s*(.*?)\s*;?\s*\Z", re.DOTALL
)


class XiaohongshuMetadataProviderError(ValueError):
    """Stable public error; page, network and privacy details stay internal."""


class TextNetworkPort(Protocol):
    def fetch_text(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class XiaohongshuAnonymousMetadataPlatformProvider:
    """Anonymous metadata-only HTML provider; no Cookie, browser, download or Secret."""

    network: TextNetworkPort
    evidence: SourceResolutionEvidenceRepository
    namespace_id: str

    def provide(self, text: str, *, project_id: str) -> SourceManifest:
        note_id, canonical_url = _canonical_source(text)
        try:
            html = self.network.fetch_text(canonical_url)
        except NetworkBoundaryError as error:
            raise XiaohongshuMetadataProviderError("network_denied") from error
        note = _extract_note(html, note_id=note_id)
        normalized, assets, content_kind, body = _normalize_note(note, note_id=note_id)
        source_id = f"xhs-{note_id}"
        evidence_id = f"{source_id}--public-html-v1"
        try:
            proof = self.evidence.put(
                project_id=project_id,
                evidence_id=evidence_id,
                kind="xiaohongshu_metadata_resolution",
                payload={
                    "source_id": source_id,
                    "input_identity": canonical_url,
                    "resolver_revision": _RESOLVER_REVISION,
                    "normalizer_revision": _NORMALIZER_REVISION,
                    "metadata": normalized,
                    "asset_kinds": [item["kind"] for item in assets],
                },
            )
        except SourceResolutionEvidenceError as error:
            raise XiaohongshuMetadataProviderError(str(error)) from error
        source_ref = f"crp://{self.namespace_id}/sources/{source_id}"
        manifest_assets: list[dict[str, object]] = []
        for ordinal, item in enumerate(assets):
            asset_id = f"{item['kind']}-{ordinal + 1}"
            relations: list[dict[str, str]] = []
            if ordinal > 0:
                relations.append(
                    {"relation": "previous", "target_asset_id": f"{assets[ordinal - 1]['kind']}-{ordinal}"}
                )
            if ordinal + 1 < len(assets):
                relations.append(
                    {"relation": "next", "target_asset_id": f"{assets[ordinal + 1]['kind']}-{ordinal + 2}"}
                )
            manifest_assets.append(
                {
                    "asset_id": asset_id,
                    "ordinal": ordinal,
                    "kind": item["kind"],
                    "media_type": item["media_type"],
                    "role": item["role"],
                    # Metadata discovery never persists signed or unvalidated CDN URLs.
                    "locator": None,
                    "source_ref": f"{source_ref}/assets/{asset_id}",
                    "relations": relations,
                    "evidence_refs": [proof.public_ref],
                }
            )
        return SourceManifestCodec.decode(
            {
                "schema_version": "1.0.0",
                "source_id": source_id,
                "source_ref": source_ref,
                "platform": "xiaohongshu",
                "input_identity": canonical_url,
                "resolver_revision": _RESOLVER_REVISION,
                "normalizer_revision": _NORMALIZER_REVISION,
                "content_kind": content_kind,
                "body": body,
                "metadata": normalized,
                "permission": {"decision": "unknown", "evidence_refs": [proof.public_ref]},
                "provenance_refs": [proof.public_ref],
                "assets": manifest_assets,
            }
        )


@dataclass(frozen=True, slots=True)
class XiaohongshuControlledMetadataPlatformProvider:
    """Explicit login-required metadata provider with no Cookie-bearing API.

    ``credential_subject_id`` is a non-secret user selection.  The OS runtime
    derives the frozen authorization facts and injects the Cookie only inside
    its dedicated pinned request adapter.  There is intentionally no anonymous
    fallback in either direction.
    """

    runtime: XiaohongshuControlledCredentialRuntime
    network: SafeControlledCookieTextNetworkAdapter
    evidence: SourceResolutionEvidenceRepository
    namespace_id: str

    def provide_controlled(
        self, text: str, *, project_id: str, credential_subject_id: str
    ) -> SourceManifest:
        note_id, canonical_url = _canonical_source(text)
        try:
            binding = self.runtime.current_binding(
                project_id=project_id, credential_subject_id=credential_subject_id,
            )
            html = self.runtime.fetch_text(
                canonical_url, project_id=project_id, binding=binding, network=self.network,
            )
        except XiaohongshuControlledCredentialRuntimeError as error:
            raise XiaohongshuMetadataProviderError(str(error)) from error
        except NetworkBoundaryError as error:
            raise XiaohongshuMetadataProviderError("network_denied") from error
        note = _extract_note(html, note_id=note_id)
        normalized, assets, content_kind, body = _normalize_note(note, note_id=note_id)
        return _manifest_from_note(
            project_id=project_id,
            note_id=note_id,
            canonical_url=canonical_url,
            normalized=normalized,
            assets=assets,
            content_kind=content_kind,
            body=body,
            evidence=self.evidence,
            namespace_id=self.namespace_id,
            credential_binding=binding,
        )

    # Keep a narrow compatibility shim for direct unit consumers; production
    # composition calls the explicit method so the mode cannot be inferred.
    def provide(
        self, text: str, *, project_id: str, credential_subject_id: str
    ) -> SourceManifest:
        return self.provide_controlled(
            text, project_id=project_id, credential_subject_id=credential_subject_id,
        )


def _manifest_from_note(
    *,
    project_id: str,
    note_id: str,
    canonical_url: str,
    normalized: dict[str, object],
    assets: list[dict[str, str]],
    content_kind: str,
    body: dict[str, object] | None,
    evidence: SourceResolutionEvidenceRepository,
    namespace_id: str,
    credential_binding: ControlledCredentialBinding,
) -> SourceManifest:
    """Persist only scrubbed note facts and freeze non-secret credential use."""

    source_id = f"xhs-{note_id}"
    evidence_id = f"{source_id}--controlled-html-v1"
    try:
        proof = evidence.put(
            project_id=project_id,
            evidence_id=evidence_id,
            kind="xiaohongshu_metadata_resolution",
            payload={
                "source_id": source_id,
                "input_identity": canonical_url,
                "resolver_revision": _RESOLVER_REVISION,
                "normalizer_revision": _NORMALIZER_REVISION,
                "metadata": normalized,
                "asset_kinds": [item["kind"] for item in assets],
                # Evidence proves scrubbed remote content only.  The frozen
                # authorization facts belong to the manifest/receipt binding;
                # putting a rotating Secret generation here would turn a
                # harmless credential rotation into source-evidence drift.
            },
        )
    except SourceResolutionEvidenceError as error:
        raise XiaohongshuMetadataProviderError(str(error)) from error
    source_ref = f"crp://{namespace_id}/sources/{source_id}"
    manifest_assets: list[dict[str, object]] = []
    for ordinal, item in enumerate(assets):
        asset_id = f"{item['kind']}-{ordinal + 1}"
        relations: list[dict[str, str]] = []
        if ordinal > 0:
            relations.append({"relation": "previous", "target_asset_id": f"{assets[ordinal - 1]['kind']}-{ordinal}"})
        if ordinal + 1 < len(assets):
            relations.append({"relation": "next", "target_asset_id": f"{assets[ordinal + 1]['kind']}-{ordinal + 2}"})
        manifest_assets.append({
            "asset_id": asset_id,
            "ordinal": ordinal,
            "kind": item["kind"],
            "media_type": item["media_type"],
            "role": item["role"],
            "locator": None,
            "source_ref": f"{source_ref}/assets/{asset_id}",
            "relations": relations,
            "evidence_refs": [proof.public_ref],
        })
    return SourceManifestCodec.decode({
        "schema_version": "1.1.0",
        "source_id": source_id,
        "source_ref": source_ref,
        "platform": "xiaohongshu",
        "input_identity": canonical_url,
        "resolver_revision": _RESOLVER_REVISION,
        "normalizer_revision": _NORMALIZER_REVISION,
        "content_kind": content_kind,
        "body": body,
        "metadata": normalized,
        "permission": {"decision": "unknown", "evidence_refs": [proof.public_ref]},
        "provenance_refs": [proof.public_ref],
        "credential_binding": _credential_binding_payload(credential_binding),
        "assets": manifest_assets,
    })


def _credential_binding_payload(binding: ControlledCredentialBinding) -> dict[str, object]:
    return {
        "mode": binding.mode,
        "provider": binding.provider,
        "credential_subject_id": binding.credential_subject_id,
        "authorization_ref": binding.authorization_ref,
        "authorization_revision": binding.authorization_revision,
        "secret_generation": binding.secret_generation,
        "boundary_profile_id": binding.boundary_profile_id,
        "boundary_profile_revision": binding.boundary_profile_revision,
    }


def verify_xiaohongshu_frozen_manifest(
    manifest: SourceManifest, note: Mapping[str, object]
) -> tuple[str, tuple[str | None, ...]]:
    """Re-bind an execution fetch to the public note and its frozen shape.

    The returned locators are deliberately ephemeral.  Callers must first use
    this function to prove the page still represents the frozen manifest, then
    consume the locators in memory only.
    """

    if not isinstance(manifest, SourceManifest) or manifest.platform != "xiaohongshu":
        raise XiaohongshuMetadataProviderError("manifest_not_xiaohongshu")
    if manifest.permission.decision != "granted":
        raise XiaohongshuMetadataProviderError("manifest_not_authorized")
    try:
        note_id, canonical_url = _canonical_source(manifest.input_identity)
    except XiaohongshuMetadataProviderError as error:
        raise XiaohongshuMetadataProviderError("manifest_identity_invalid") from error
    if (
        manifest.source_id != f"xhs-{note_id}"
        or manifest.input_identity != canonical_url
        or manifest.resolver_revision != _RESOLVER_REVISION
        or not _is_executable_normalizer_revision(manifest.normalizer_revision)
        or any(asset.locator is not None for asset in manifest.assets)
    ):
        raise XiaohongshuMetadataProviderError("manifest_identity_invalid")
    # This also checks the embedded note id before comparing any mutable page
    # fields with the manifest we froze at admission time.
    normalized, shapes, content_kind, body = _normalize_note(note, note_id=note_id)
    if (
        manifest.content_kind != content_kind
        or dict(manifest.metadata.entries) != normalized
        or len(manifest.assets) != len(shapes)
    ):
        raise XiaohongshuMetadataProviderError("manifest_drift")
    expected = _manifest_payload_for_compare(manifest, normalized, shapes, content_kind, body)
    candidate = SourceManifestCodec.decode(expected)
    if candidate != manifest:
        raise XiaohongshuMetadataProviderError("manifest_drift")
    return note_id, _asset_locators(note, content_kind=content_kind, expected_shapes=shapes)


def _is_executable_normalizer_revision(value: str) -> bool:
    """Accept the immutable base or one canonical SourcePermission derivative."""

    return value == _NORMALIZER_REVISION or re.fullmatch(
        rf"{re.escape(_NORMALIZER_REVISION)}-permission-r[1-9][0-9]*", value
    ) is not None


def _manifest_payload_for_compare(
    manifest: SourceManifest,
    metadata: dict[str, object],
    shapes: list[dict[str, str]],
    content_kind: str,
    body: dict[str, object] | None,
) -> dict[str, object]:
    assets: list[dict[str, object]] = []
    for ordinal, shape in enumerate(shapes):
        asset_id = f"{shape['kind']}-{ordinal + 1}"
        relations: list[dict[str, str]] = []
        if ordinal > 0:
            relations.append({"relation": "previous", "target_asset_id": f"{shapes[ordinal - 1]['kind']}-{ordinal}"})
        if ordinal + 1 < len(shapes):
            relations.append({"relation": "next", "target_asset_id": f"{shapes[ordinal + 1]['kind']}-{ordinal + 2}"})
        assets.append({
            "asset_id": asset_id,
            "ordinal": ordinal,
            "kind": shape["kind"],
            "media_type": shape["media_type"],
            "role": shape["role"],
            "locator": None,
            "source_ref": f"{manifest.source_ref}/assets/{asset_id}",
            "relations": relations,
            "evidence_refs": list(manifest.provenance_refs),
        })
    return {
        "schema_version": manifest.schema_version,
        "source_id": manifest.source_id,
        "source_ref": manifest.source_ref,
        "platform": manifest.platform,
        "input_identity": manifest.input_identity,
        "resolver_revision": manifest.resolver_revision,
        "normalizer_revision": manifest.normalizer_revision,
        "content_kind": content_kind,
        "body": body,
        "metadata": metadata,
        "permission": {
            "decision": manifest.permission.decision,
            "evidence_refs": list(manifest.permission.evidence_refs),
        },
        "provenance_refs": list(manifest.provenance_refs),
        # v1.1 comparison must preserve the frozen non-secret credential
        # identity.  Omitting it would falsely turn a controlled manifest into
        # an anonymous one during execution-time revalidation.
        "credential_binding": (
            None if manifest.credential_binding is None
            else _credential_binding_payload(manifest.credential_binding)
        ),
        "assets": assets,
    }


def _asset_locators(
    note: Mapping[str, object], *, content_kind: str, expected_shapes: list[dict[str, str]]
) -> tuple[str | None, ...]:
    locators: list[str | None] = []
    if content_kind == "image_set":
        images = note.get("imageList")
        if not isinstance(images, list):
            raise XiaohongshuMetadataProviderError("manifest_drift")
        locators = [_asset_locator(item, kind="image") for item in images]
    elif content_kind == "video":
        locators = [_asset_locator(note.get("video"), kind="video")]
    elif content_kind == "mixed":
        media = note.get("mediaList")
        if not isinstance(media, list):
            raise XiaohongshuMetadataProviderError("manifest_drift")
        locators = [
            _asset_locator(item, kind=_bounded_text(item.get("type"), 10).lower())
            if isinstance(item, Mapping) else None
            for item in media
        ]
        if expected_shapes and expected_shapes[-1]["kind"] == "text":
            locators.append(None)
    else:
        locators = [None for _shape in expected_shapes]
    if len(locators) != len(expected_shapes):
        raise XiaohongshuMetadataProviderError("manifest_drift")
    for locator, shape in zip(locators, expected_shapes, strict=True):
        if shape["kind"] == "text":
            if locator is not None:
                raise XiaohongshuMetadataProviderError("manifest_drift")
        elif not isinstance(locator, str) or not locator:
            raise XiaohongshuMetadataProviderError("asset_locator_unavailable")
    return tuple(locators)


def _asset_locator(value: object, *, kind: str) -> str | None:
    if not isinstance(value, Mapping) or kind not in {"image", "video"}:
        return None
    for key in ("url", "urlDefault", "masterUrl"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    info = value.get("infoList")
    if isinstance(info, list):
        for item in info:
            if isinstance(item, Mapping) and isinstance(item.get("url"), str) and item["url"]:
                return item["url"]
    if kind != "video":
        return None
    media = value.get("media")
    stream = media.get("stream") if isinstance(media, Mapping) else None
    if not isinstance(stream, Mapping):
        return None
    for codec in ("h264", "h265"):
        candidates = stream.get(codec)
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            master = candidate.get("masterUrl")
            if isinstance(master, str) and master:
                return master
            backups = candidate.get("backupUrls")
            if isinstance(backups, list):
                for backup in backups:
                    if isinstance(backup, str) and backup:
                        return backup
    return None


def build_xiaohongshu_anonymous_metadata_platform_provider(
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
    network_profile: NetworkEgressProfile | None = None,
) -> XiaohongshuAnonymousMetadataPlatformProvider:
    connect_proxy = (
        None
        if network_profile is None
        else loopback_proxy_for_capability(network_profile, "anonymous_public_media")
    )
    return XiaohongshuAnonymousMetadataPlatformProvider(
        network=SafeTextNetworkAdapter(
            allowed_hosts=("xiaohongshu.com", "www.xiaohongshu.com"),
            max_redirects=2,
            max_response_bytes=1024 * 1024,
            timeout_seconds=20.0,
            connect_proxy=connect_proxy,
        ),
        evidence=SourceResolutionEvidenceRepository(
            object_store, namespace_id=namespace_id
        ),
        namespace_id=namespace_id,
    )


def _canonical_source(text: str) -> tuple[str, str]:
    matches = re.findall(r"https?://[^\s\]】]+", text)
    if len(matches) != 1:
        raise XiaohongshuMetadataProviderError("invalid_source")
    try:
        parsed = urlsplit(matches[0])
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError) as error:
        raise XiaohongshuMetadataProviderError("invalid_source") from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or host not in {"xiaohongshu.com", "www.xiaohongshu.com"}
        or parsed.query
    ):
        raise XiaohongshuMetadataProviderError("invalid_source")
    match = re.fullmatch(r"/(?:explore|discovery/item)/([A-Za-z0-9]{24})/?", parsed.path)
    if match is None or _NOTE_ID.fullmatch(match.group(1)) is None:
        raise XiaohongshuMetadataProviderError("invalid_source")
    note_id = match.group(1)
    return note_id, f"https://www.xiaohongshu.com/explore/{note_id}"


class _StateScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._state_script_id = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        values = {name.lower(): value for name, value in attrs}
        self._capturing = True
        self._state_script_id = values.get("id") == _STATE_SCRIPT_ID
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            script = "".join(self._parts)
            if self._state_script_id or _INITIAL_STATE_ASSIGNMENT.fullmatch(script):
                self.scripts.append(script)
            self._capturing = False
            self._state_script_id = False
            self._parts = []


def _extract_note(html: str, *, note_id: str) -> Mapping[str, object]:
    parser = _StateScriptParser()
    try:
        parser.feed(html)
    except Exception as error:
        raise XiaohongshuMetadataProviderError("metadata_unavailable") from error
    if len(parser.scripts) != 1:
        raise XiaohongshuMetadataProviderError("unsupported_source")
    try:
        script = parser.scripts[0]
        assignment = _INITIAL_STATE_ASSIGNMENT.fullmatch(script)
        if assignment is not None:
            script = _replace_bare_undefined(assignment.group(1))
        payload = json.loads(script)
    except (TypeError, json.JSONDecodeError) as error:
        raise XiaohongshuMetadataProviderError("metadata_unavailable") from error
    if not isinstance(payload, Mapping):
        raise XiaohongshuMetadataProviderError("metadata_unavailable")
    candidates: list[object] = []
    note_root = payload.get("note")
    if isinstance(note_root, Mapping):
        candidates.append(note_root.get("noteDetailMap"))
    web_root = payload.get("webNoteDetailData")
    if isinstance(web_root, Mapping) and isinstance(web_root.get("note"), Mapping):
        candidates.append(web_root["note"].get("noteDetailMap"))
    candidates.append(payload.get("xhsNoteDetail"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        entry = candidate.get(note_id)
        if entry is None:
            continue
        if isinstance(entry, Mapping) and isinstance(entry.get("note"), Mapping):
            entry = entry["note"]
        if not isinstance(entry, Mapping):
            continue
        embedded_id = entry.get("noteId")
        if not isinstance(embedded_id, str) or embedded_id != note_id:
            raise XiaohongshuMetadataProviderError("metadata_identity_mismatch")
        return entry
    raise XiaohongshuMetadataProviderError("unsupported_source")


def _replace_bare_undefined(value: str) -> str:
    """Normalize SSR's JavaScript-only undefined token without touching strings."""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        character = value[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if value.startswith("undefined", index):
            before = value[index - 1] if index else ""
            after_index = index + len("undefined")
            after = value[after_index] if after_index < len(value) else ""
            if not (before.isalnum() or before in "_$" or after.isalnum() or after in "_$"):
                output.append("null")
                index = after_index
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _normalize_note(
    note: Mapping[str, object], *, note_id: str
) -> tuple[dict[str, object], list[dict[str, str]], str, dict[str, object] | None]:
    note_type = _bounded_text(note.get("type"), 20).lower()
    title = _bounded_text(note.get("title"), 300)
    description = _bounded_text(note.get("desc"), 4000)
    assets: list[dict[str, str]] = []
    if note_type == "mixed":
        media = note.get("mediaList")
        if not isinstance(media, list) or not 2 <= len(media) <= 50:
            raise XiaohongshuMetadataProviderError("unsupported_source")
        for item in media:
            kind = _bounded_text(item.get("type"), 10).lower() if isinstance(item, Mapping) else ""
            if kind not in {"image", "video"} or not _has_asset_locator(item, kind=kind):
                raise XiaohongshuMetadataProviderError("unsupported_source")
            assets.append(_asset_shape(kind, mixed=True))
        if {item["kind"] for item in assets} != {"image", "video"}:
            raise XiaohongshuMetadataProviderError("unsupported_source")
        if description:
            assets.append({"kind": "text", "media_type": "text/plain", "role": "caption"})
        content_kind = "mixed"
    elif note_type == "video":
        if not isinstance(note.get("video"), Mapping) or not _has_asset_locator(
            note["video"], kind="video"
        ):
            raise XiaohongshuMetadataProviderError("unsupported_source")
        assets = [_asset_shape("video", mixed=False)]
        content_kind = "video"
    else:
        images = note.get("imageList")
        if (
            isinstance(images, list)
            and 1 <= len(images) <= 50
            and all(
                isinstance(item, Mapping) and _has_asset_locator(item, kind="image")
                for item in images
            )
        ):
            assets = [_asset_shape("image", mixed=False) for _item in images]
            content_kind = "image_set"
            note_type = "image"
        elif description:
            assets = [{"kind": "text", "media_type": "text/plain", "role": "body"}]
            content_kind = "text"
            note_type = "text"
        else:
            raise XiaohongshuMetadataProviderError("unsupported_source")
    published_at = _published_date(note.get("time"))
    metadata = {
        "note_id": note_id,
        "note_type": note_type,
        "title": title or "小红书笔记",
        "published_at": published_at,
        "asset_count": len(assets),
    }
    body = {"kind": "text", "text": description, "source_ref": None} if description else None
    return metadata, assets, content_kind, body


def _asset_shape(kind: str, *, mixed: bool) -> dict[str, str]:
    return {
        "kind": kind,
        "media_type": "image/jpeg" if kind == "image" else "video/mp4",
        "role": "sequence" if mixed else ("gallery" if kind == "image" else "primary"),
    }


def _has_asset_locator(value: Mapping[str, object], *, kind: str) -> bool:
    direct = ("url", "urlDefault", "masterUrl")
    if any(isinstance(value.get(key), str) and bool(value[key]) for key in direct):
        return True
    info = value.get("infoList")
    if isinstance(info, list) and any(
        isinstance(item, Mapping)
        and isinstance(item.get("url"), str)
        and bool(item["url"])
        for item in info
    ):
        return True
    if kind != "video":
        return False
    media = value.get("media")
    stream = media.get("stream") if isinstance(media, Mapping) else None
    if not isinstance(stream, Mapping):
        return False
    for codec in ("h264", "h265"):
        candidates = stream.get(codec)
        if isinstance(candidates, list) and any(
            isinstance(item, Mapping)
            and (
                isinstance(item.get("masterUrl"), str)
                and bool(item["masterUrl"])
                or isinstance(item.get("backupUrls"), list)
                and any(isinstance(url, str) and url for url in item["backupUrls"])
            )
            for item in candidates
        ):
            return True
    return False


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)
    return re.sub(r"\s+", " ", normalized).strip()[:limit]


def _published_date(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return ""
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""
