"""Unresolved portable references recorded by the existing import authority."""

from collections.abc import Mapping


def unresolved_import_evidence(reader):
    """Yield receipt, target experience ID and issue without granting eligibility."""
    for imported in reader.list("recognition_migration_imports"):
        payload = imported.payload
        if not isinstance(payload.get("scope"), Mapping):
            continue
        mapping = payload.get("mapping", {}).get("experiences", {})
        for issue in payload.get("receipt", {}).get("issues", ()):
            if (issue.get("location") == "provenance"
                    and issue.get("code") in {"external_provenance_reference", "source_revision_mismatch"}
                    and issue.get("record_id") in mapping):
                yield payload, mapping[issue["record_id"]], issue


def unresolved_provenance_id(import_id, index):
    return f"migration-{import_id}-external-provenance-{index}"
