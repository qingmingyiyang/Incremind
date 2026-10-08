from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import (
    EFFECT_V2,
    EffectClass,
    EffectHandlerRegistration,
    EffectRecoveryRegistration,
)
from core.product_core.original_asset_retention import (
    ExecuteOriginalAssetRetentionPurge,
)
from core.product_core.source_retention_purge import ExecuteSourceRetentionPurge
from core.storage_provider.vault_backup_restore import fingerprint_vault_root


def register_retention_effect_handlers(runtime_root: Path, effect_runtime) -> None:
    """Register retention execution and recovery with the shared Core runtime."""

    root = Path(runtime_root)
    store, _settings = build_rebuild_object_store(root)
    source = ExecuteSourceRetentionPurge(
        store,
        effect_runner=effect_runtime.runner,
        active_fingerprint=lambda: fingerprint_vault_root(root / ".rebuild-data"),
        owned_media_roots=_configured_source_media_roots(store),
    )
    original = ExecuteOriginalAssetRetentionPurge(
        store,
        library_root=root / "library",
        effect_runner=effect_runtime.runner,
        active_fingerprint=lambda: fingerprint_vault_root(root / ".rebuild-data"),
    )
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="source_retention_purge",
        effect_class=EffectClass.QUERYABLE,
        handler=source.handle_effect,
        probe=lambda effect: source.verify_effect(effect.operation_id),
        contract_version=EFFECT_V2,
        intent_schema_version="source-retention-purge-intent-v2",
        receipt_kind="source-retention-purge-receipt",
        receipt_schema_version="source-retention-purge-receipt-v2",
    ))
    effect_runtime.recoveries.register(EffectRecoveryRegistration(
        kind="source_retention_purge",
        effect_class=EffectClass.QUERYABLE,
        probe=lambda effect: source.verify_effect(effect.operation_id),
        verify=lambda effect: source.verify_effect(effect.operation_id),
        contract_version=EFFECT_V2,
    ))
    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="original_asset_retention_purge",
        effect_class=EffectClass.QUERYABLE,
        handler=original.handle_effect,
        probe=lambda effect: original.verify_effect(effect.operation_id),
        contract_version=EFFECT_V2,
        intent_schema_version="original-asset-retention-intent-v2",
        receipt_kind="original-asset-retention-receipt",
        receipt_schema_version="original-asset-retention-receipt-v2",
    ))
    effect_runtime.recoveries.register(EffectRecoveryRegistration(
        kind="original_asset_retention_purge",
        effect_class=EffectClass.QUERYABLE,
        probe=lambda effect: original.verify_effect(effect.operation_id),
        verify=lambda effect: original.verify_effect(effect.operation_id),
        contract_version=EFFECT_V2,
    ))


def _configured_source_media_roots(store: object) -> tuple[Path, ...]:
    read = getattr(store, "read", None)
    if not callable(read):
        return ()
    settings = read("video_audio_extractor_settings", "default")
    if not isinstance(settings, Mapping):
        return ()
    raw = settings.get("output_root")
    if not isinstance(raw, str) or not raw.strip():
        return ()
    root = Path(raw).expanduser()
    return (root,) if root.is_absolute() else ()
