from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .codec import SourceManifestCodec
from .models import SourceManifest


class _FixtureAdapter:
    platform: str

    def manifest_from_fixture(self, fixture: Mapping[str, Any]) -> SourceManifest:
        value = dict(fixture)
        value["platform"] = self.platform
        return SourceManifestCodec.decode(value)


class BilibiliAdapter(_FixtureAdapter):
    """Platform parser boundary; this contract slice performs no URL resolution."""

    platform = "bilibili"


class XiaohongshuAdapter(_FixtureAdapter):
    """Platform parser boundary; content kind remains derived from discovered assets."""

    platform = "xiaohongshu"
