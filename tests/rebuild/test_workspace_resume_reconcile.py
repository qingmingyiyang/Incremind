from __future__ import annotations

import pytest

from core.product_core.workspace_resume_reconcile import (
    WorkspaceResumeReconcileError,
    reconcile_workspace_manifests,
)


def _entry(digest: str, size: int = 1, revision: str | None = "base-1") -> dict[str, object]:
    return {"digest": digest, "size_bytes": size, "base_revision": revision}


def test_workspace_reconcile_is_plan_only_and_classifies_every_change_deterministically() -> None:
    plan = reconcile_workspace_manifests(
        source_manifest={
            "added.md": _entry("source-added"),
            "changed.md": _entry("source-change"),
            "conflict.md": _entry("source-conflict"),
            "deleted-on-host.md": _entry("base-delete-host"),
            "same-change.md": _entry("same-change"),
            "unchanged.md": _entry("same"),
        },
        host_manifest={
            "changed.md": _entry("base-change"),
            "conflict.md": _entry("host-conflict"),
            "deleted.md": _entry("base-delete"),
            "host-only.md": _entry("host-only"),
            "same-change.md": _entry("same-change"),
            "unchanged.md": _entry("same"),
        },
        base_manifest={
            "changed.md": _entry("base-change"),
            "conflict.md": _entry("base-conflict"),
            "deleted-on-host.md": _entry("base-delete-host"),
            "deleted.md": _entry("base-delete"),
            "same-change.md": _entry("base-same-change"),
            "unchanged.md": _entry("same"),
        },
    )

    assert plan.mode == "plan_only"
    assert [(entry.relative_path, entry.classification) for entry in plan.entries] == [
        ("added.md", "add"),
        ("changed.md", "modify"),
        ("conflict.md", "conflict"),
        ("deleted-on-host.md", "unchanged"),
        ("deleted.md", "delete"),
        ("host-only.md", "unchanged"),
        ("same-change.md", "unchanged"),
        ("unchanged.md", "unchanged"),
    ]
    assert plan.classifications == {
        "unchanged": ("deleted-on-host.md", "host-only.md", "same-change.md", "unchanged.md"),
        "add": ("added.md",),
        "modify": ("changed.md",),
        "delete": ("deleted.md",),
        "conflict": ("conflict.md",),
    }
    assert plan.has_conflicts is True
    assert plan.as_dict()["apply_supported"] is False


def test_workspace_reconcile_conflicts_when_source_and_host_diverge_from_base() -> None:
    plan = reconcile_workspace_manifests(
        source_manifest={"notes.md": _entry("source")},
        host_manifest={"notes.md": _entry("host")},
        base_manifest={"notes.md": _entry("base")},
    )

    assert plan.entries[0].classification == "conflict"


@pytest.mark.parametrize(
    "path",
    (
        "/absolute.md",
        "C:/absolute.md",
        "dir\\windows.md",
        "../escape.md",
        "dir/../escape.md",
        "dir//empty.md",
        ".env",
        "secrets/token.json",
        "keys/id_rsa",
    ),
)
def test_workspace_reconcile_rejects_unportable_or_secret_like_paths(path: str) -> None:
    with pytest.raises(WorkspaceResumeReconcileError):
        reconcile_workspace_manifests(
            source_manifest={path: _entry("digest")},
            host_manifest={},
            base_manifest={},
        )


def test_workspace_reconcile_rejects_symlink_intent_and_non_plan_mode() -> None:
    with pytest.raises(WorkspaceResumeReconcileError, match="unsupported field"):
        reconcile_workspace_manifests(
            source_manifest={"notes.md": {**_entry("digest"), "symlink": "elsewhere"}},
            host_manifest={},
            base_manifest={},
        )
    with pytest.raises(WorkspaceResumeReconcileError, match="plan_only"):
        reconcile_workspace_manifests(
            source_manifest={}, host_manifest={}, base_manifest={}, mode="apply"  # type: ignore[arg-type]
        )
