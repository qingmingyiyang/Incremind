from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from core.source_processing import BilibiliAdapter, MediaRouter, SourceManifestCodec, SourceManifestCodecError, XiaohongshuAdapter


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "core-contracts" / "rebuild" / "source-processing" / "fixtures"
SCHEMA = ROOT / "core-contracts" / "rebuild" / "source-processing" / "source-manifest.schema.json"
CONTRACT_MANIFEST = ROOT / "core-contracts" / "rebuild" / "source-processing" / "manifest.json"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_schema_is_strict_and_valid() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert validator.is_valid(fixture("xiaohongshu-mixed.json"))
    invalid = fixture("xiaohongshu-video.json")
    invalid["unexpected"] = True
    assert not validator.is_valid(invalid)
    contract = json.loads(CONTRACT_MANIFEST.read_text(encoding="utf-8"))["contracts"][0]
    assert contract["semantic_validator"] == "core.source_processing.SourceManifestCodec"
    assert "relation_target_within_manifest" in contract["semantic_invariants"]


def test_platform_and_content_kind_are_orthogonal() -> None:
    adapter = XiaohongshuAdapter()
    manifests = [adapter.manifest_from_fixture(fixture(name)) for name in ("xiaohongshu-image-set.json", "xiaohongshu-video.json", "xiaohongshu-mixed.json", "xiaohongshu-unknown.json")]
    assert {manifest.platform for manifest in manifests} == {"xiaohongshu"}
    assert [manifest.content_kind for manifest in manifests] == ["image_set", "video", "mixed", "unknown"]


def test_bilibili_video_routes_to_video_without_network_or_file_access() -> None:
    manifest = BilibiliAdapter().manifest_from_fixture(fixture("bilibili-video.json"))
    outcome = MediaRouter().route(manifest)
    assert manifest.platform == "bilibili"
    assert outcome.status == "routed"
    assert outcome.pipelines[0].pipeline == "video"


def test_mixed_preserves_order_relations_and_evidence_with_parallel_declarations() -> None:
    manifest = XiaohongshuAdapter().manifest_from_fixture(fixture("xiaohongshu-mixed.json"))
    outcome = MediaRouter().route(manifest)
    assert [(item.ordinal, item.asset_id) for item in manifest.assets] == [(0, "image-1"), (1, "video-1"), (2, "text-1")]
    assert manifest.assets[0].relations[0].target_asset_id == "video-1"
    assert manifest.assets[2].evidence_refs == ("crp://evidence/xhs-mixed-caption",)
    assert outcome.mode == "parallel_composite"
    assert [item.pipeline for item in outcome.pipelines] == ["image", "video", "text"]


def test_unknown_is_terminal_and_never_defaults_to_video() -> None:
    outcome = MediaRouter().route(XiaohongshuAdapter().manifest_from_fixture(fixture("xiaohongshu-unknown.json")))
    assert outcome.status == "terminal" and outcome.reason == "unknown_content_kind"
    assert outcome.source_ref == "crp://sources/xhs-unknown"
    assert "crp://evidence/xhs-unknown" in {
        ref for asset in XiaohongshuAdapter().manifest_from_fixture(fixture("xiaohongshu-unknown.json")).assets for ref in asset.evidence_refs
    }


@pytest.mark.parametrize(("decision", "reason"), [
    ("denied", "source_permission_denied"),
    ("unknown", "source_permission_unresolved"),
])
def test_unresolved_permission_never_enters_media_pipeline(decision, reason) -> None:
    value = fixture("bilibili-video.json")
    value["permission"]["decision"] = decision
    outcome = MediaRouter().route(BilibiliAdapter().manifest_from_fixture(value))
    assert outcome.status == "terminal" and outcome.reason == reason
    assert outcome.source_ref == value["source_ref"] and outcome.evidence_refs


@pytest.mark.parametrize("mutate", [
    lambda value: value["assets"].append(deepcopy(value["assets"][0])),
    lambda value: value["assets"].__setitem__(1, {**value["assets"][1], "ordinal": 9}),
    lambda value: value["assets"][0]["relations"].__setitem__(0, {"relation": "next", "target_asset_id": "outside"}),
    lambda value: value.__setitem__("unexpected", True),
])
def test_codec_rejects_duplicate_order_drift_cross_asset_reference_and_unknown_fields(mutate) -> None:
    value = fixture("xiaohongshu-image-set.json")
    mutate(value)
    with pytest.raises(SourceManifestCodecError):
        SourceManifestCodec.decode(value)


def test_dto_and_codec_round_trip_are_immutable() -> None:
    manifest = XiaohongshuAdapter().manifest_from_fixture(fixture("xiaohongshu-video.json"))
    with pytest.raises(AttributeError):
        manifest.platform = "bilibili"  # type: ignore[misc]
    assert SourceManifestCodec.decode(SourceManifestCodec.encode(manifest)) == manifest


def test_legacy_manifest_reads_without_credential_binding_and_encodes_as_v11() -> None:
    legacy = fixture("xiaohongshu-video.json")
    assert legacy["schema_version"] == "1.0.0"
    decoded = SourceManifestCodec.decode(legacy)
    assert decoded.schema_version == "1.1.0"
    assert decoded.credential_binding is None
    encoded = SourceManifestCodec.encode(decoded)
    assert encoded["schema_version"] == "1.1.0"
    assert encoded["credential_binding"] is None
    assert SourceManifestCodec.decode(encoded).credential_binding is None


def test_controlled_credential_binding_freezes_only_non_secret_facts() -> None:
    value = fixture("xiaohongshu-video.json")
    value["schema_version"] = "1.1.0"
    value["platform"] = "xiaohongshu"
    value["credential_binding"] = {
        "mode": "controlled_credential",
        "provider": "xiaohongshu",
        "credential_subject_id": "xhs-account-a",
        "authorization_ref": "crp://authority/xhs-credential-authorizations/project-a/auth-a",
        "authorization_revision": 3,
        "secret_generation": 7,
        "boundary_profile_id": "project-boundary-project-a",
        "boundary_profile_revision": 5,
    }
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert Draft202012Validator(schema).is_valid(value)
    manifest = SourceManifestCodec.decode(value)
    assert manifest.credential_binding is not None
    assert manifest.credential_binding.secret_generation == 7
    assert SourceManifestCodec.encode(manifest) == value


@pytest.mark.parametrize("mutate", [
    lambda value: value["credential_binding"].__setitem__("cookie", "plaintext-cookie"),
    lambda value: value["credential_binding"].__setitem__("authorization_ref", "plaintext-cookie"),
    lambda value: value["credential_binding"].__setitem__("secret_generation", 0),
    lambda value: value.__setitem__("credential_binding", {"mode": "controlled_credential"}),
    lambda value: value.__setitem__("platform", "bilibili"),
])
def test_credential_binding_rejects_sensitive_fields_and_incomplete_or_invalid_facts(mutate) -> None:
    value = fixture("xiaohongshu-video.json")
    value["schema_version"] = "1.1.0"
    value["platform"] = "xiaohongshu"
    value["credential_binding"] = {
        "mode": "controlled_credential", "provider": "xiaohongshu",
        "credential_subject_id": "xhs-account-a",
        "authorization_ref": "crp://authority/xhs-credential-authorizations/project-a/auth-a",
        "authorization_revision": 3, "secret_generation": 7,
        "boundary_profile_id": "project-boundary-project-a", "boundary_profile_revision": 5,
    }
    mutate(value)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert not Draft202012Validator(schema).is_valid(value)
    with pytest.raises(SourceManifestCodecError):
        SourceManifestCodec.decode(value)


def test_codec_freezes_nested_metadata() -> None:
    value = fixture("xiaohongshu-video.json")
    value["metadata"] = {"nested": {"labels": ["fixture"]}}
    manifest = XiaohongshuAdapter().manifest_from_fixture(value)
    assert isinstance(manifest.metadata.entries, tuple)
    assert SourceManifestCodec.encode(manifest)["metadata"] == value["metadata"]


def test_codec_requires_controlled_references_and_supports_share_text_input_identity() -> None:
    value = fixture("xiaohongshu-image-set.json")
    assert value["input_identity"] == "xhs share text images"
    value["assets"][0]["evidence_refs"] = ["https://uncontrolled.example/evidence"]
    with pytest.raises(SourceManifestCodecError):
        SourceManifestCodec.decode(value)


@pytest.mark.parametrize("metadata", [{"bad": float("nan")}, {"nested": {1: "bad-key"}}])
def test_codec_rejects_non_json_metadata(metadata) -> None:
    value = fixture("xiaohongshu-video.json")
    value["metadata"] = metadata
    with pytest.raises(SourceManifestCodecError):
        SourceManifestCodec.decode(value)


@pytest.mark.parametrize("path", ["provenance", "permission", "asset"])
def test_schema_and_codec_require_traceable_evidence(path) -> None:
    value = fixture("xiaohongshu-mixed.json")
    if path == "provenance":
        value["provenance_refs"] = []
    elif path == "permission":
        value["permission"]["evidence_refs"] = []
    else:
        value["assets"][0]["evidence_refs"] = []
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert not Draft202012Validator(schema).is_valid(value)
    with pytest.raises(SourceManifestCodecError):
        SourceManifestCodec.decode(value)
