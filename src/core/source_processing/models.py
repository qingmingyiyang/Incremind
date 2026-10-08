from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ContentKind = Literal["image_set", "video", "mixed", "text", "unknown"]
AssetKind = Literal["image", "video", "text", "unknown"]


@dataclass(frozen=True)
class ManifestBody:
    kind: Literal["text", "ref"]
    text: str | None
    source_ref: str | None


@dataclass(frozen=True)
class ManifestPermission:
    decision: Literal["granted", "denied", "unknown"]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ControlledCredentialBinding:
    """Frozen non-secret authorization facts for one governed credential use."""

    mode: Literal["controlled_credential"]
    provider: Literal["xiaohongshu"]
    credential_subject_id: str
    authorization_ref: str
    authorization_revision: int
    secret_generation: int
    boundary_profile_id: str
    boundary_profile_revision: int


@dataclass(frozen=True)
class FrozenJsonObject:
    entries: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class FrozenJsonArray:
    items: tuple[object, ...]


@dataclass(frozen=True)
class AssetRelation:
    relation: str
    target_asset_id: str


@dataclass(frozen=True)
class SourceAsset:
    asset_id: str
    ordinal: int
    kind: AssetKind
    media_type: str | None
    role: str
    locator: str | None
    source_ref: str
    relations: tuple[AssetRelation, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class SourceManifest:
    schema_version: str
    source_id: str
    source_ref: str
    platform: str
    input_identity: str
    resolver_revision: str
    normalizer_revision: str
    content_kind: ContentKind
    body: ManifestBody | None
    metadata: FrozenJsonObject
    permission: ManifestPermission
    provenance_refs: tuple[str, ...]
    assets: tuple[SourceAsset, ...]
    credential_binding: ControlledCredentialBinding | None = None
