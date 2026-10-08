from __future__ import annotations

from pathlib import Path

from core.product_core.ports import ObjectStorePort
from core.product_core.workbench_original_asset import (
    ResolveWorkbenchOriginalAsset,
    StoreWorkbenchOriginalAsset,
    StoreWorkbenchOriginalAssetBatch,
)


def original_assets_root(runtime_root: Path) -> Path:
    return runtime_root / "library" / "assets" / "originals"


def build_original_asset_store(
    runtime_root: Path,
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
) -> StoreWorkbenchOriginalAsset:
    return StoreWorkbenchOriginalAsset(
        object_store=object_store,
        assets_root=original_assets_root(runtime_root),
        namespace_id=namespace_id,
    )


def build_original_asset_batch_store(
    runtime_root: Path,
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
) -> StoreWorkbenchOriginalAssetBatch:
    return StoreWorkbenchOriginalAssetBatch(
        object_store=object_store,
        assets_root=original_assets_root(runtime_root),
        namespace_id=namespace_id,
    )


def build_original_asset_resolver(
    runtime_root: Path,
    object_store: ObjectStorePort,
) -> ResolveWorkbenchOriginalAsset:
    return ResolveWorkbenchOriginalAsset(
        object_store=object_store,
        library_root=runtime_root / "library",
    )
