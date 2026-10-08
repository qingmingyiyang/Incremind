from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any, cast

from .models import AssetRelation, ContentKind, ControlledCredentialBinding, FrozenJsonArray, FrozenJsonObject, ManifestBody, ManifestPermission, SourceAsset, SourceManifest


class SourceManifestCodecError(ValueError):
    """Raised when a source manifest is not an exact, internally coherent DTO."""


class SourceManifestCodec:
    VERSION = "1.1.0"
    LEGACY_VERSION = "1.0.0"
    _MANIFEST_FIELDS_V1 = {"schema_version", "source_id", "source_ref", "platform", "input_identity", "resolver_revision", "normalizer_revision", "content_kind", "body", "metadata", "permission", "provenance_refs", "assets"}
    _MANIFEST_FIELDS_V11 = _MANIFEST_FIELDS_V1 | {"credential_binding"}
    _CREDENTIAL_BINDING_FIELDS = {
        "mode", "provider", "credential_subject_id", "authorization_ref",
        "authorization_revision", "secret_generation", "boundary_profile_id",
        "boundary_profile_revision",
    }
    _ASSET_FIELDS = {"asset_id", "ordinal", "kind", "media_type", "role", "locator", "source_ref", "relations", "evidence_refs"}
    _RELATION_FIELDS = {"relation", "target_asset_id"}
    _CONTENT_KINDS = {"image_set", "video", "mixed", "text", "unknown"}
    _ASSET_KINDS = {"image", "video", "text", "unknown"}
    _REF_PATTERN = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~/-]*$")

    @classmethod
    def decode(cls, value: Mapping[str, Any]) -> SourceManifest:
        schema_version = cls._string(value, "schema_version", "manifest")
        if schema_version == cls.LEGACY_VERSION:
            cls._exact_fields(value, cls._MANIFEST_FIELDS_V1, "manifest")
            credential_binding = None
        elif schema_version == cls.VERSION:
            cls._exact_fields(value, cls._MANIFEST_FIELDS_V11, "manifest")
            credential_binding = cls._decode_credential_binding(value["credential_binding"])
        else:
            raise SourceManifestCodecError("unsupported schema_version")
        content_kind = cls._string(value, "content_kind", "manifest")
        if content_kind not in cls._CONTENT_KINDS:
            raise SourceManifestCodecError("unsupported content_kind")
        if credential_binding is not None and cls._string(value, "platform", "manifest") != credential_binding.provider:
            raise SourceManifestCodecError("credential binding provider does not match manifest platform")
        raw_assets = value["assets"]
        if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes, bytearray)):
            raise SourceManifestCodecError("assets must be an array")
        assets = tuple(cls._decode_asset(item) for item in raw_assets)
        if not assets:
            raise SourceManifestCodecError("manifest must contain at least one asset")
        cls._validate_assets(assets, cast(ContentKind, content_kind))
        return SourceManifest(
            # DTOs are normalized in memory.  Reading v1.0.0 does not mutate
            # its artifact; it only supplies the new optional fact as None.
            schema_version=cls.VERSION,
            source_id=cls._string(value, "source_id", "manifest"),
            source_ref=cls._ref(value["source_ref"], "manifest.source_ref"),
            platform=cls._string(value, "platform", "manifest"),
            input_identity=cls._string(value, "input_identity", "manifest"),
            resolver_revision=cls._string(value, "resolver_revision", "manifest"),
            normalizer_revision=cls._string(value, "normalizer_revision", "manifest"),
            content_kind=cast(ContentKind, content_kind),
            body=cls._decode_body(value["body"]),
            metadata=cls._json_object(value["metadata"], "metadata"),
            permission=cls._decode_permission(value["permission"]),
            provenance_refs=cls._refs(value["provenance_refs"], "provenance_refs"),
            assets=assets,
            credential_binding=credential_binding,
        )

    @classmethod
    def encode(cls, manifest: SourceManifest) -> dict[str, Any]:
        # Re-validate a reconstructed value so even manually built DTOs cannot bypass invariants.
        value = {
            "schema_version": cls.VERSION,
            "source_id": manifest.source_id,
            "source_ref": manifest.source_ref,
            "platform": manifest.platform,
            "input_identity": manifest.input_identity,
            "resolver_revision": manifest.resolver_revision,
            "normalizer_revision": manifest.normalizer_revision,
            "content_kind": manifest.content_kind,
            "body": None if manifest.body is None else {"kind": manifest.body.kind, "text": manifest.body.text, "source_ref": manifest.body.source_ref},
            "metadata": cls._thaw_json(manifest.metadata),
            "permission": {"decision": manifest.permission.decision, "evidence_refs": list(manifest.permission.evidence_refs)},
            "provenance_refs": list(manifest.provenance_refs),
            "credential_binding": None if manifest.credential_binding is None else {
                "mode": manifest.credential_binding.mode,
                "provider": manifest.credential_binding.provider,
                "credential_subject_id": manifest.credential_binding.credential_subject_id,
                "authorization_ref": manifest.credential_binding.authorization_ref,
                "authorization_revision": manifest.credential_binding.authorization_revision,
                "secret_generation": manifest.credential_binding.secret_generation,
                "boundary_profile_id": manifest.credential_binding.boundary_profile_id,
                "boundary_profile_revision": manifest.credential_binding.boundary_profile_revision,
            },
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "ordinal": asset.ordinal,
                    "kind": asset.kind,
                    "media_type": asset.media_type,
                    "role": asset.role,
                    "locator": asset.locator,
                    "source_ref": asset.source_ref,
                    "relations": [
                        {"relation": relation.relation, "target_asset_id": relation.target_asset_id}
                        for relation in asset.relations
                    ],
                    "evidence_refs": list(asset.evidence_refs),
                }
                for asset in manifest.assets
            ],
        }
        cls.decode(value)
        return value

    @classmethod
    def _decode_credential_binding(cls, value: Any) -> ControlledCredentialBinding | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise SourceManifestCodecError("credential_binding must be null or an object")
        cls._exact_fields(value, cls._CREDENTIAL_BINDING_FIELDS, "credential_binding")
        if cls._string(value, "mode", "credential_binding") != "controlled_credential":
            raise SourceManifestCodecError("unsupported credential binding mode")
        if cls._string(value, "provider", "credential_binding") != "xiaohongshu":
            raise SourceManifestCodecError("unsupported credential binding provider")
        return ControlledCredentialBinding(
            mode="controlled_credential",
            provider="xiaohongshu",
            credential_subject_id=cls._identifier(value, "credential_subject_id", "credential_binding"),
            authorization_ref=cls._ref(value["authorization_ref"], "credential_binding.authorization_ref"),
            authorization_revision=cls._positive_int(value, "authorization_revision", "credential_binding"),
            secret_generation=cls._positive_int(value, "secret_generation", "credential_binding"),
            boundary_profile_id=cls._identifier(value, "boundary_profile_id", "credential_binding"),
            boundary_profile_revision=cls._positive_int(value, "boundary_profile_revision", "credential_binding"),
        )

    @classmethod
    def _decode_asset(cls, value: Any) -> SourceAsset:
        if not isinstance(value, Mapping):
            raise SourceManifestCodecError("asset must be an object")
        cls._exact_fields(value, cls._ASSET_FIELDS, "asset")
        kind = cls._string(value, "kind", "asset")
        if kind not in cls._ASSET_KINDS:
            raise SourceManifestCodecError("unsupported asset kind")
        ordinal = value["ordinal"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise SourceManifestCodecError("asset ordinal must be a non-negative integer")
        locator = value["locator"]
        if locator is not None and (not isinstance(locator, str) or not locator):
            raise SourceManifestCodecError("asset locator must be null or a non-empty string")
        return SourceAsset(
            asset_id=cls._string(value, "asset_id", "asset"),
            ordinal=ordinal,
            kind=cast(Any, kind),
            media_type=cls._nullable_string(value["media_type"], "asset.media_type"),
            role=cls._string(value, "role", "asset"),
            locator=locator,
            source_ref=cls._ref(value["source_ref"], "asset.source_ref"),
            relations=cls._decode_relations(value["relations"]),
            evidence_refs=cls._refs(value["evidence_refs"], "asset.evidence_refs"),
        )

    @classmethod
    def _decode_relations(cls, value: Any) -> tuple[AssetRelation, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise SourceManifestCodecError("relations must be an array")
        decoded: list[AssetRelation] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise SourceManifestCodecError("relation must be an object")
            cls._exact_fields(item, cls._RELATION_FIELDS, "relation")
            decoded.append(AssetRelation(cls._string(item, "relation", "relation"), cls._string(item, "target_asset_id", "relation")))
        return tuple(decoded)

    @classmethod
    def _decode_body(cls, value: Any) -> ManifestBody | None:
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != {"kind", "text", "source_ref"}:
            raise SourceManifestCodecError("body fields are not exact")
        kind = cls._string(value, "kind", "body")
        if kind == "text" and isinstance(value["text"], str) and value["text"] and value["source_ref"] is None:
            return ManifestBody("text", value["text"], None)
        if kind == "ref" and value["text"] is None:
            return ManifestBody("ref", None, cls._ref(value["source_ref"], "body.source_ref"))
        raise SourceManifestCodecError("invalid body variant")

    @classmethod
    def _decode_permission(cls, value: Any) -> ManifestPermission:
        if not isinstance(value, Mapping) or set(value) != {"decision", "evidence_refs"}:
            raise SourceManifestCodecError("permission fields are not exact")
        decision = cls._string(value, "decision", "permission")
        if decision not in {"granted", "denied", "unknown"}:
            raise SourceManifestCodecError("unsupported permission decision")
        return ManifestPermission(cast(Any, decision), cls._refs(value["evidence_refs"], "permission.evidence_refs"))

    @classmethod
    def _refs(cls, value: Any, label: str) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise SourceManifestCodecError(f"{label} must be an array")
        refs = tuple(cls._ref(item, label) for item in value)
        if not refs:
            raise SourceManifestCodecError(f"{label} must contain at least one reference")
        if len(refs) != len(set(refs)):
            raise SourceManifestCodecError(f"{label} contains duplicate references")
        return refs

    @classmethod
    def _ref(cls, value: Any, label: str) -> str:
        if not isinstance(value, str) or cls._REF_PATTERN.fullmatch(value) is None:
            raise SourceManifestCodecError(f"{label} must be a controlled crp reference")
        return value

    @classmethod
    def _json_object(cls, value: Any, label: str) -> FrozenJsonObject:
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise SourceManifestCodecError(f"{label} must be a JSON object")
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise SourceManifestCodecError(f"{label} must contain JSON values") from error
        return FrozenJsonObject(tuple(sorted((key, cls._freeze_json(item)) for key, item in value.items())))

    @classmethod
    def _freeze_json(cls, value: Any) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return FrozenJsonArray(tuple(cls._freeze_json(item) for item in value))
        if isinstance(value, Mapping):
            if not all(isinstance(key, str) for key in value):
                raise SourceManifestCodecError("metadata object keys must be strings")
            return FrozenJsonObject(tuple(sorted((key, cls._freeze_json(item)) for key, item in value.items())))
        raise SourceManifestCodecError("metadata must contain JSON values")

    @classmethod
    def _thaw_json(cls, value: object) -> object:
        if isinstance(value, FrozenJsonObject):
            return {key: cls._thaw_json(item) for key, item in value.entries}
        if isinstance(value, FrozenJsonArray):
            return [cls._thaw_json(item) for item in value.items]
        return value

    @classmethod
    def _validate_assets(cls, assets: tuple[SourceAsset, ...], content_kind: ContentKind) -> None:
        ids = [asset.asset_id for asset in assets]
        if len(ids) != len(set(ids)):
            raise SourceManifestCodecError("duplicate asset_id")
        if [asset.ordinal for asset in assets] != list(range(len(assets))):
            raise SourceManifestCodecError("asset ordinal/order drift")
        known_ids = set(ids)
        for asset in assets:
            for relation in asset.relations:
                if relation.target_asset_id not in known_ids:
                    raise SourceManifestCodecError("relation target must reference a manifest asset")
                if relation.target_asset_id == asset.asset_id:
                    raise SourceManifestCodecError("asset cannot relate to itself")
        kinds = {asset.kind for asset in assets}
        expected = {
            "image_set": {"image"},
            "video": {"video"},
            "text": {"text"},
        }
        if content_kind in expected and kinds != expected[content_kind]:
            raise SourceManifestCodecError("content_kind and assets disagree")
        if content_kind == "mixed" and len(kinds & {"image", "video", "text"}) < 2:
            raise SourceManifestCodecError("mixed requires at least two supported asset kinds")
        if content_kind == "unknown" and any(kind != "unknown" for kind in kinds):
            raise SourceManifestCodecError("unknown may only contain unknown assets")

    @staticmethod
    def _exact_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
        if set(value) != fields:
            raise SourceManifestCodecError(f"{label} fields are not exact")

    @staticmethod
    def _string(value: Mapping[str, Any], field: str, label: str) -> str:
        result = value.get(field)
        if not isinstance(result, str) or not result:
            raise SourceManifestCodecError(f"{label}.{field} must be a non-empty string")
        return result

    @classmethod
    def _identifier(cls, value: Mapping[str, Any], field: str, label: str) -> str:
        result = cls._string(value, field, label)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", result) is None:
            raise SourceManifestCodecError(f"{label}.{field} must be a controlled identifier")
        return result

    @staticmethod
    def _positive_int(value: Mapping[str, Any], field: str, label: str) -> int:
        result = value.get(field)
        if not isinstance(result, int) or isinstance(result, bool) or result < 1:
            raise SourceManifestCodecError(f"{label}.{field} must be a positive integer")
        return result

    @staticmethod
    def _nullable_string(value: Any, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise SourceManifestCodecError(f"{label} must be null or a non-empty string")
        return value
