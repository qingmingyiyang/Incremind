from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from .codec import SourceManifestCodec, SourceManifestCodecError
from .models import SourceManifest


PlatformName = str
_KNOWN = frozenset({"bilibili", "xiaohongshu"})


@dataclass(frozen=True)
class PlatformProbeResult:
    platform: PlatformName
    confidence: str
    evidence: str


@dataclass(frozen=True)
class PlatformResolution:
    status: str
    platform: PlatformName
    reason: str | None
    manifest: SourceManifest | None


class ProviderPort(Protocol):
    def provide(self, text: str, *, project_id: str) -> SourceManifest:
        """Build a manifest behind the provider's governed I/O boundary."""


class PlatformResolver:
    def __init__(self, providers: dict[PlatformName, ProviderPort]) -> None:
        unknown = set(providers) - _KNOWN
        if unknown:
            raise ValueError("provider registry contains an unsupported platform")
        if any(not callable(getattr(provider, "provide", None)) for provider in providers.values()):
            raise ValueError("provider registry contains an invalid provider")
        self._providers = dict(providers)

    @property
    def registered_platforms(self) -> tuple[PlatformName, ...]:
        return tuple(sorted(self._providers))

    def probe(self, text: str) -> PlatformProbeResult:
        """Classify a source without invoking any platform provider or network."""
        if not isinstance(text, str) or not text.strip():
            return PlatformProbeResult("unknown", "none", "empty_input")
        return self._probe(text)

    def resolve(self, text: str, *, project_id: str) -> PlatformResolution:
        if not isinstance(text, str) or not text.strip():
            return PlatformResolution("unknown", "unknown", "empty_input", None)
        probe = self.probe(text)
        if probe.platform == "unknown":
            reason = "ambiguous_platform" if probe.evidence == "ambiguous_domains" else "unknown_platform"
            return PlatformResolution("unknown", "unknown", reason, None)
        provider = self._providers.get(probe.platform)
        if provider is None:
            return PlatformResolution("unsupported", probe.platform, "provider_missing", None)
        try:
            manifest = SourceManifestCodec.decode(
                SourceManifestCodec.encode(provider.provide(text, project_id=project_id))
            )
        except (SourceManifestCodecError, ValueError, TypeError):
            return PlatformResolution("unsupported", probe.platform, "provider_returned_invalid_manifest", None)
        if manifest.platform != probe.platform:
            return PlatformResolution("unsupported", probe.platform, "provider_platform_mismatch", None)
        if manifest.content_kind == "unknown":
            return PlatformResolution("unsupported", probe.platform, "unknown_content_kind", manifest)
        return PlatformResolution("resolved", probe.platform, None, manifest)

    @staticmethod
    def _probe(text: str) -> PlatformProbeResult:
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        url_tokens = re.findall(
            r"https?://[^\s]+|[a-z0-9.-]+\.[a-z]{2,}(?::[0-9]+)?(?:/[^\s]*)?",
            normalized,
        )
        detected: list[str] = []
        for token in url_tokens:
            parsed = urlsplit(token if "://" in token else f"//{token}")
            if parsed.scheme not in {"", "http", "https"} or parsed.username is not None or parsed.password is not None:
                continue
            hostname = parsed.hostname
            if hostname is None:
                continue
            try:
                hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
            except UnicodeError:
                continue
            if hostname == "bilibili.com" or hostname.endswith(".bilibili.com"):
                detected.append("bilibili")
            elif hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com"):
                detected.append("xiaohongshu")
        detected = list(dict.fromkeys(detected))
        if len(detected) == 1:
            return PlatformProbeResult(detected[0], "domain", "url_domain")
        if len(detected) > 1:
            return PlatformProbeResult("unknown", "none", "ambiguous_domains")
        if url_tokens:
            return PlatformProbeResult("unknown", "none", "unrecognized_url")
        share_hits = {
            "bilibili": "bilibili" in normalized or "b站" in normalized,
            "xiaohongshu": "xiaohongshu" in normalized or "小红书" in normalized,
        }
        detected = [platform for platform, hit in share_hits.items() if hit]
        if len(detected) == 1:
            return PlatformProbeResult(detected[0], "share_text", "normalized_share_text")
        return PlatformProbeResult("unknown", "none", "unrecognized")
