from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from backend.api.xiaohongshu_asset_materializer import (
    XiaohongshuAssetMaterializationError,
    XiaohongshuAssetMaterializer,
    build_xiaohongshu_asset_materializer,
)
from backend.security import DownloadedBinary
from core.job_runner import JobStepBlockedError
from core.source_processing import ControlledCredentialBinding, SourceManifestCodec


NOTE_ID = "65f1234567890abc12345678"


def _note(kind: str, *, host: str = "img.xhscdn.com") -> dict[str, object]:
    result: dict[str, object] = {
        "noteId": NOTE_ID, "type": kind, "title": "冻结标题", "desc": "冻结正文", "time": 1_725_638_400_000,
    }
    if kind == "image":
        result["imageList"] = [
            {"urlDefault": f"https://{host}/one.jpg?xsec_token=private-a"},
            {"urlDefault": f"https://{host}/two.jpg?xsec_token=private-b"},
        ]
    elif kind == "video":
        result["video"] = {"masterUrl": f"https://{host}/one.mp4?xsec_token=private-video"}
    elif kind == "mixed":
        result["mediaList"] = [
            {"type": "image", "url": f"https://{host}/one.jpg?xsec_token=private-a"},
            {"type": "video", "url": f"https://{host}/two.mp4?xsec_token=private-b"},
            {"type": "image", "url": f"https://{host}/three.jpg?xsec_token=private-c"},
        ]
    return result


def _html(note: dict[str, object]) -> str:
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(
        {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}
    ) + "</script>"


def _manifest(kind: str):
    if kind == "image":
        shapes = [("image", "image/jpeg", "gallery"), ("image", "image/jpeg", "gallery")]
        content_kind = "image_set"
    elif kind == "video":
        shapes = [("video", "video/mp4", "primary")]
        content_kind = "video"
    else:
        shapes = [
            ("image", "image/jpeg", "sequence"), ("video", "video/mp4", "sequence"),
            ("image", "image/jpeg", "sequence"), ("text", "text/plain", "caption"),
        ]
        content_kind = "mixed"
    assets = []
    for ordinal, (asset_kind, media_type, role) in enumerate(shapes):
        asset_id = f"{asset_kind}-{ordinal + 1}"
        relations = []
        if ordinal:
            relations.append({"relation": "previous", "target_asset_id": f"{shapes[ordinal - 1][0]}-{ordinal}"})
        if ordinal + 1 < len(shapes):
            relations.append({"relation": "next", "target_asset_id": f"{shapes[ordinal + 1][0]}-{ordinal + 2}"})
        assets.append({
            "asset_id": asset_id, "ordinal": ordinal, "kind": asset_kind, "media_type": media_type,
            "role": role, "locator": None, "source_ref": f"crp://default/sources/xhs-{NOTE_ID}/assets/{asset_id}",
            "relations": relations, "evidence_refs": ["crp://default/evidence/xhs"],
        })
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0", "source_id": f"xhs-{NOTE_ID}",
        "source_ref": f"crp://default/sources/xhs-{NOTE_ID}", "platform": "xiaohongshu",
        "input_identity": f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
        "resolver_revision": "xhs-html-v1", "normalizer_revision": "xhs-manifest-v1",
        "content_kind": content_kind,
        "body": {"kind": "text", "text": "冻结正文", "source_ref": None},
        "metadata": {"note_id": NOTE_ID, "note_type": "image" if kind == "image" else kind,
                     "title": "冻结标题", "published_at": "2024-09-06", "asset_count": len(assets)},
        "permission": {"decision": "granted", "evidence_refs": ["crp://default/evidence/xhs"]},
        "provenance_refs": ["crp://default/evidence/xhs"], "assets": assets,
    })


def _credential_manifest(kind: str = "image"):
    return replace(
        _manifest(kind),
        credential_binding=ControlledCredentialBinding(
            mode="controlled_credential",
            provider="xiaohongshu",
            credential_subject_id="xhs-account-1",
            authorization_ref="crp://default/credentials/xhs-account-1",
            authorization_revision=3,
            secret_generation=7,
            boundary_profile_id="media-boundary",
            boundary_profile_revision=4,
        ),
    )


class _PageNetwork:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []

    def fetch_text(self, url: str) -> str:
        self.calls.append(url)
        return self.html


class _BinaryNetwork:
    def __init__(self, root: Path, *, byte_count: int = 5) -> None:
        self.root, self.byte_count, self.calls = root, byte_count, []

    def download(self, url: str, **kwargs) -> DownloadedBinary:
        self.calls.append((url, kwargs))
        path = self.root / kwargs["relative_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * self.byte_count)
        media_type = "image/jpeg" if path.suffix == ".jpg" else "video/mp4"
        return DownloadedBinary(path, self.byte_count, media_type)


def test_builder_accepts_an_os_composed_binary_port_without_changing_default_contract(
    tmp_path: Path,
) -> None:
    injected = _BinaryNetwork(tmp_path)

    materializer = build_xiaohongshu_asset_materializer(
        tmp_path / "staging", binary_network=injected
    )

    assert materializer.binary_network is injected


class _CredentialLease:
    """Test-only controlled adapter: materializer never receives its Cookie."""

    def __init__(self, *, generation_current: bool = True) -> None:
        self.current = generation_current
        self.page_calls = 0
        self.binary_calls = 0
        self.headers_seen: list[dict[str, str]] = []

    def fetch_text(self, network, url: str, *, control_check):
        self.page_calls += 1
        control_check()
        return network.fetch_text(url)

    def download(self, network, url: str, **kwargs):
        self.binary_calls += 1
        self.headers_seen.append(dict(kwargs["headers"]))
        assert "Cookie" not in kwargs["headers"]
        return network.download(url, **kwargs)

    def generation_current(self) -> bool:
        return self.current


class _CredentialRuntime:
    def __init__(self, leases: list[_CredentialLease], *, fail_at: int | None = None) -> None:
        self.leases = leases
        self.fail_at = fail_at
        self.operations: list[str] = []

    def issue_wire_lease(self, manifest, *, project_id: str, operation: str):
        self.operations.append(operation)
        if self.fail_at is not None and len(self.operations) == self.fail_at:
            raise RuntimeError("authorization changed")
        return self.leases[len(self.operations) - 1]


@pytest.mark.parametrize("kind, expected", [("image", ["image", "image"]), ("video", ["video"]), ("mixed", ["image", "video", "image", "text"])])
def test_materializes_frozen_image_video_and_mixed_assets_in_manifest_order(tmp_path: Path, kind: str, expected: list[str]) -> None:
    page = _PageNetwork(_html(_note(kind)))
    binary = _BinaryNetwork(tmp_path)
    result = XiaohongshuAssetMaterializer(page, binary).materialize(
        _manifest(kind), job_id="media_hands:xhs:analyze_source", max_download_bytes=10000, timeout_seconds=5,
    )

    assert [item.kind for item in result.assets] == expected
    assert [item.ordinal for item in result.assets] == list(range(len(expected)))
    assert [item.byte_count for item in result.assets] == ([5] * (len(expected) - (kind == "mixed")) + ([0] if kind == "mixed" else []))
    assert result.total_download_bytes == len(_html(_note(kind)).encode("utf-8")) + 5 * len([value for value in expected if value != "text"])
    assert len(binary.calls) == len([value for value in expected if value != "text"])
    assert all("xsec_token" not in item.staged_path for item in result.assets if item.staged_path)
    assert all("xsec_token" not in args["relative_path"] for _url, args in binary.calls)
    assert all("xiaohongshu/" in args["relative_path"] for _url, args in binary.calls)


def test_manifest_structure_drift_fails_closed_before_any_binary_download(tmp_path: Path) -> None:
    changed = _note("image")
    changed["title"] = "漂移标题"
    binary = _BinaryNetwork(tmp_path)
    with pytest.raises(XiaohongshuAssetMaterializationError, match="manifest_drift"):
        XiaohongshuAssetMaterializer(_PageNetwork(_html(changed)), binary).materialize(
            _manifest("image"), job_id="job-xhs-drift", max_download_bytes=10000, timeout_seconds=5,
        )
    assert binary.calls == []


@pytest.mark.parametrize("host", ["evil.example", "xhscdn.com", "img.xhscdn.com:444", "user@img.xhscdn.com"])
def test_unreviewed_or_ambiguous_locator_never_reaches_downloader_or_error(tmp_path: Path, host: str) -> None:
    binary = _BinaryNetwork(tmp_path)
    with pytest.raises(XiaohongshuAssetMaterializationError) as captured:
        XiaohongshuAssetMaterializer(_PageNetwork(_html(_note("image", host=host))), binary).materialize(
            _manifest("image"), job_id="job-xhs-private", max_download_bytes=10000, timeout_seconds=5,
        )
    assert str(captured.value) == "asset_locator_denied"
    assert binary.calls == []
    assert "private" not in str(captured.value) and "xhscdn" not in str(captured.value)


def test_cumulative_byte_budget_and_cancellation_are_fail_closed(tmp_path: Path) -> None:
    binary = _BinaryNetwork(tmp_path, byte_count=6)
    materializer = XiaohongshuAssetMaterializer(_PageNetwork(_html(_note("image"))), binary)
    page_bytes = len(_html(_note("image")).encode("utf-8"))
    with pytest.raises(XiaohongshuAssetMaterializationError, match="asset_download_budget_exhausted"):
        materializer.materialize(_manifest("image"), job_id="job-xhs-budget", max_download_bytes=page_bytes + 10, timeout_seconds=5)
    assert [call[1]["max_response_bytes"] for call in binary.calls] == [10, 4]
    assert not any(tmp_path.rglob("*.jpg"))

    cancelled_binary = _BinaryNetwork(tmp_path / "cancel")
    with pytest.raises(XiaohongshuAssetMaterializationError, match="asset_materialization_interrupted") as captured:
        XiaohongshuAssetMaterializer(_PageNetwork(_html(_note("image"))), cancelled_binary).materialize(
            _manifest("image"), job_id="job-xhs-cancel", max_download_bytes=10000, timeout_seconds=5,
            control_check=lambda: (_ for _ in ()).throw(RuntimeError("private-token")),
        )
    assert cancelled_binary.calls == []
    assert "private-token" not in str(captured.value)


def test_direct_job_control_failure_from_network_port_keeps_blocked_classification(tmp_path: Path) -> None:
    class BlockedPageNetwork:
        def fetch_text(self, url: str) -> str:
            raise JobStepBlockedError(code="media.cancelled", message="Media job was cancelled.")

    with pytest.raises(JobStepBlockedError) as captured:
        XiaohongshuAssetMaterializer(BlockedPageNetwork(), _BinaryNetwork(tmp_path)).materialize(
            _manifest("image"), job_id="job-xhs-blocked", max_download_bytes=10000,
            timeout_seconds=5, project_id="project-1",
        )

    assert captured.value.code == "media.cancelled"
    assert not any(tmp_path.rglob("*"))


def test_controlled_credential_uses_a_fresh_os_owned_lease_for_page_and_each_binary(tmp_path: Path) -> None:
    page = _PageNetwork(_html(_note("image")))
    binary = _BinaryNetwork(tmp_path)
    leases = [_CredentialLease(), _CredentialLease(), _CredentialLease()]
    runtime = _CredentialRuntime(leases)

    result = XiaohongshuAssetMaterializer(
        page, binary, controlled_credential_runtime=runtime
    ).materialize(
        _credential_manifest(), job_id="job-xhs-controlled", max_download_bytes=10_000,
                timeout_seconds=5, project_id="project-1",
    )

    assert len(result.assets) == 2
    assert runtime.operations == ["page", "binary", "binary"]
    assert [lease.page_calls for lease in leases] == [1, 0, 0]
    assert [lease.binary_calls for lease in leases] == [0, 1, 1]
    assert all("Cookie" not in header for lease in leases for header in lease.headers_seen)


def test_controlled_credential_pre_wire_drift_makes_no_platform_request(tmp_path: Path) -> None:
    page = _PageNetwork(_html(_note("image")))
    binary = _BinaryNetwork(tmp_path)
    runtime = _CredentialRuntime([], fail_at=1)

    with pytest.raises(XiaohongshuAssetMaterializationError, match="controlled_credential_confirmed_none"):
        XiaohongshuAssetMaterializer(
            page, binary, controlled_credential_runtime=runtime
        ).materialize(
            _credential_manifest(), job_id="job-xhs-pre-fence", max_download_bytes=10_000,
                timeout_seconds=5, project_id="project-1",
        )

    assert page.calls == []
    assert binary.calls == []


def test_controlled_credential_post_page_rotation_stops_before_any_binary_request(tmp_path: Path) -> None:
    page = _PageNetwork(_html(_note("image")))
    binary = _BinaryNetwork(tmp_path)
    runtime = _CredentialRuntime([_CredentialLease(generation_current=False)])

    with pytest.raises(XiaohongshuAssetMaterializationError, match="controlled_credential_post_wire_unknown"):
        XiaohongshuAssetMaterializer(
            page, binary, controlled_credential_runtime=runtime
        ).materialize(
            _credential_manifest(), job_id="job-xhs-rotate-page", max_download_bytes=10_000,
                timeout_seconds=5, project_id="project-1",
        )

    assert page.calls
    assert binary.calls == []


def test_controlled_credential_post_binary_revoke_cleans_staging_and_never_retries(tmp_path: Path) -> None:
    page = _PageNetwork(_html(_note("image")))
    binary = _BinaryNetwork(tmp_path)
    runtime = _CredentialRuntime([
        _CredentialLease(),
        _CredentialLease(generation_current=False),
    ])

    with pytest.raises(XiaohongshuAssetMaterializationError, match="controlled_credential_post_wire_unknown"):
        XiaohongshuAssetMaterializer(
            page, binary, controlled_credential_runtime=runtime
        ).materialize(
            _credential_manifest(), job_id="job-xhs-revoke-binary", max_download_bytes=10_000,
            timeout_seconds=5, project_id="project-1",
        )

    assert runtime.operations == ["page", "binary"]
    assert len(binary.calls) == 1
    assert not any(tmp_path.rglob("*.jpg"))


def test_controlled_credential_boundary_drift_before_second_binary_never_sends_second_request(tmp_path: Path) -> None:
    page = _PageNetwork(_html(_note("image")))
    binary = _BinaryNetwork(tmp_path)
    runtime = _CredentialRuntime([_CredentialLease(), _CredentialLease()], fail_at=3)

    with pytest.raises(XiaohongshuAssetMaterializationError, match="controlled_credential_confirmed_none"):
        XiaohongshuAssetMaterializer(
            page, binary, controlled_credential_runtime=runtime
        ).materialize(
            _credential_manifest(), job_id="job-xhs-boundary-race", max_download_bytes=10_000,
            timeout_seconds=5, project_id="project-1",
        )

    assert runtime.operations == ["page", "binary", "binary"]
    assert len(binary.calls) == 1
    assert not any(tmp_path.rglob("*.jpg"))
