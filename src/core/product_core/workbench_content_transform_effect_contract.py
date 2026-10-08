"""Immutable Effect-v2 contract for background Workbench content transforms."""

EFFECT_KIND = "workbench_content_transform"
INTENT_SCHEMA = "workbench-content-transform-intent-v2"
RECEIPT_KIND = "workbench-content-transform.receipt"
RECEIPT_SCHEMA = "workbench-content-transform-receipt-v2"
RECEIPT_TABLE = "workbench_content_transform_receipt"

__all__ = (
    "EFFECT_KIND",
    "INTENT_SCHEMA",
    "RECEIPT_KIND",
    "RECEIPT_SCHEMA",
    "RECEIPT_TABLE",
)
