from __future__ import annotations

from dataclasses import dataclass

from .models import SourceManifest


@dataclass(frozen=True)
class PipelineDeclaration:
    pipeline: str
    asset_ids: tuple[str, ...]


@dataclass(frozen=True)
class MediaRoutingOutcome:
    status: str
    mode: str
    reason: str | None
    pipelines: tuple[PipelineDeclaration, ...]
    source_ref: str
    evidence_refs: tuple[str, ...]


class MediaRouter:
    """Declares processing only. It neither obtains bytes nor runs a pipeline."""

    def route(self, manifest: SourceManifest) -> MediaRoutingOutcome:
        if manifest.content_kind == "unknown":
            return self._terminal(manifest, "unknown_content_kind")
        if manifest.permission.decision != "granted":
            return self._terminal(
                manifest,
                "source_permission_denied"
                if manifest.permission.decision == "denied"
                else "source_permission_unresolved",
            )
        by_kind = {
            kind: tuple(asset.asset_id for asset in manifest.assets if asset.kind == kind)
            for kind in ("image", "video", "text")
        }
        if manifest.content_kind == "image_set":
            return self._single(manifest, "image", by_kind["image"])
        if manifest.content_kind == "video":
            return self._single(manifest, "video", by_kind["video"])
        if manifest.content_kind == "text":
            return self._single(manifest, "text", by_kind["text"])
        if manifest.content_kind == "mixed":
            declarations = tuple(
                PipelineDeclaration(kind, asset_ids)
                for kind, asset_ids in by_kind.items()
                if asset_ids
            )
            if declarations:
                return MediaRoutingOutcome("routed", "parallel_composite", None, declarations, manifest.source_ref, manifest.provenance_refs)
        return self._terminal(manifest, "unsupported_content_kind")

    @staticmethod
    def _single(manifest: SourceManifest, kind: str, asset_ids: tuple[str, ...]) -> MediaRoutingOutcome:
        if not asset_ids:
            return MediaRouter._terminal(manifest, "unsupported_content_kind")
        return MediaRoutingOutcome("routed", "single", None, (PipelineDeclaration(kind, asset_ids),), manifest.source_ref, manifest.provenance_refs)

    @staticmethod
    def _terminal(manifest: SourceManifest, reason: str) -> MediaRoutingOutcome:
        evidence = tuple(dict.fromkeys((
            *manifest.provenance_refs,
            *manifest.permission.evidence_refs,
            *(ref for asset in manifest.assets for ref in asset.evidence_refs),
        )))
        return MediaRoutingOutcome("terminal", "none", reason, (), manifest.source_ref, evidence)
