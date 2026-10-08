from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from core.application_skill import (
    ApplicationSkillCatalog,
    ApplicationSkillError,
    ApplicationSkillPackageLoader,
    ApplicationSkillSource,
    ApplicationSkillVerifiedContent,
)


def _skill(
    source: Path,
    skill_id: str,
    *,
    description: str = "Use for project interview review and structured preparation.",
    body: str = "# Workflow\n\n1. Read project evidence.\n2. Update the existing structure.\n",
) -> Path:
    root = source / skill_id
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )
    return root


def _discover(source: Path):
    return ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("test-user", source, "user")]
    )


def test_catalog_keeps_metadata_and_resource_index_without_instruction_body(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _skill(source, "prepare-interview-review")
    (root / "references").mkdir()
    (root / "references" / "schema.md").write_text("# Evidence schema\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "format.py").write_text("print('not executed')\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "template.bin").write_bytes(b"asset")
    (root / "agents").mkdir()
    (root / "agents" / "openai.yaml").write_text("display_name: Interview Review\n", encoding="utf-8")

    snapshot = _discover(source)

    assert snapshot.issues == ()
    assert len(snapshot.packages) == 1
    package = snapshot.packages[0]
    assert package.skill_id == "prepare-interview-review"
    assert package.description.startswith("Use for project")
    assert not hasattr(package, "body")
    assert not hasattr(package, "markdown")
    assert [item.relative_path for item in package.resources] == [
        "agents/openai.yaml",
        "assets/template.bin",
        "references/schema.md",
        "scripts/format.py",
    ]
    assert all(not hasattr(item, "content") for item in package.resources)


def test_selected_package_body_loads_only_after_fingerprint_revalidation(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _skill(source, "prepare-interview-review", body="# Unique body\n\nFollow evidence.\n")
    package = _discover(source).packages[0]

    loaded = ApplicationSkillPackageLoader().load_instructions(package)

    assert loaded.skill_id == package.skill_id
    assert loaded.fingerprint == package.fingerprint
    assert loaded.markdown == "# Unique body\n\nFollow evidence.\n"
    (root / "SKILL.md").write_text(
        "---\nname: prepare-interview-review\ndescription: changed\n---\n\n# Changed\n",
        encoding="utf-8",
    )
    with pytest.raises(ApplicationSkillError, match="fingerprint drifted"):
        ApplicationSkillPackageLoader().load_instructions(package)


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("name: bad\n", "requires name and description"),
        ("name: bad\ndescription: x\nversion: 1\n", "unsupported .* field"),
        ("name: Bad_Name\ndescription: x\n", "lowercase hyphen-case"),
        ("name: other-name\ndescription: x\n", "match its package directory"),
        ("name: bad\ndescription: [x]\n", "only supports scalar"),
    ],
)
def test_invalid_frontmatter_isolated_as_catalog_issue(
    tmp_path: Path,
    frontmatter: str,
    message: str,
) -> None:
    source = tmp_path / "skills"
    bad = source / "bad"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n# Body\n", encoding="utf-8")
    _skill(source, "good-skill")

    snapshot = _discover(source)

    assert [item.skill_id for item in snapshot.packages] == ["good-skill"]
    assert len(snapshot.issues) == 1
    assert snapshot.issues[0].code == "invalid_package"
    assert __import__("re").search(message, snapshot.issues[0].detail)


def test_duplicate_identity_across_sources_rejects_every_candidate(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    _skill(bundled, "shared-method", body="# Bundled\n")
    _skill(user, "shared-method", body="# User\n")

    snapshot = ApplicationSkillCatalog().discover(
        [
            ApplicationSkillSource("bundled", bundled, "bundled"),
            ApplicationSkillSource("user", user, "user"),
        ]
    )

    assert snapshot.packages == ()
    assert [issue.code for issue in snapshot.issues] == ["duplicate_identity", "duplicate_identity"]


def test_unknown_entries_and_secret_like_material_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    unknown = _skill(source, "unknown-entry")
    (unknown / "README.md").write_text("extra", encoding="utf-8")
    _skill(source, "secret-body", body="Use token sk-abcdefghijklmnop1234\n")

    snapshot = _discover(source)

    assert snapshot.packages == ()
    details = "\n".join(issue.detail for issue in snapshot.issues)
    assert "unsupported top-level" in details
    assert "secret-like material" in details


def test_text_resources_require_utf8_and_assets_remain_opaque(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    invalid = _skill(source, "invalid-reference")
    (invalid / "references").mkdir()
    (invalid / "references" / "binary.md").write_bytes(b"\xff\xfe")
    opaque = _skill(source, "opaque-asset")
    (opaque / "assets").mkdir()
    (opaque / "assets" / "binary.bin").write_bytes(b"\xff\xfe")

    snapshot = _discover(source)

    assert [item.skill_id for item in snapshot.packages] == ["opaque-asset"]
    assert "valid UTF-8" in snapshot.issues[0].detail


def test_package_size_and_body_size_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _skill(source, "oversized-body", body="x" * (16 * 1024 + 1))

    snapshot = _discover(source)

    assert snapshot.packages == ()
    assert "size limit" in snapshot.issues[0].detail


def test_package_file_count_is_bounded(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _skill(source, "too-many-files")
    references = root / "references"
    references.mkdir()
    for index in range(128):
        (references / f"item-{index:03d}.md").write_text("bounded", encoding="utf-8")

    snapshot = _discover(source)

    assert snapshot.packages == ()
    assert "file count" in snapshot.issues[0].detail


def test_fingerprint_is_portable_and_changes_with_any_indexed_resource(tmp_path: Path) -> None:
    first_source = tmp_path / "first"
    second_source = tmp_path / "second"
    first = _skill(first_source, "portable-skill")
    second = _skill(second_source, "portable-skill")
    for root in (first, second):
        (root / "references").mkdir()
        (root / "references" / "guide.md").write_text("same", encoding="utf-8")

    first_package = _discover(first_source).packages[0]
    second_package = ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("other-source", second_source)]
    ).packages[0]
    assert first_package.fingerprint == second_package.fingerprint

    (second / "references" / "guide.md").write_text("changed", encoding="utf-8")
    changed = ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("other-source", second_source)]
    ).packages[0]
    assert changed.fingerprint != first_package.fingerprint


def test_symlink_resource_is_rejected_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _skill(source, "symlink-skill")
    (root / "references").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = root / "references" / "outside.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available on this Windows host")

    snapshot = _discover(source)

    assert snapshot.packages == ()
    assert "symlink" in snapshot.issues[0].detail


def test_verified_content_loads_after_the_original_directory_is_deleted(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _skill(source, "frozen-skill", body="# Frozen\n\nRead immutable bytes.\n")
    (root / "references").mkdir()
    (root / "references" / "guide.md").write_text("verified reference\n", encoding="utf-8")
    frozen = ApplicationSkillVerifiedContent.from_mapping({
        "SKILL.md": (root / "SKILL.md").read_bytes(),
        "references/guide.md": (root / "references" / "guide.md").read_bytes(),
    })
    package = ApplicationSkillCatalog().package_from_verified_content(
        frozen,
        source_id="external-proof",
        source_kind="external",
        package_root=root,
    )

    shutil.rmtree(root)

    loaded = ApplicationSkillPackageLoader().load_instructions(package)
    assert loaded.markdown.replace("\r\n", "\n") == "# Frozen\n\nRead immutable bytes.\n"
    assert loaded.fingerprint == package.fingerprint


def test_verified_content_revalidates_metadata_fingerprint_and_bytes_fail_closed(tmp_path: Path) -> None:
    root = _skill(tmp_path / "skills", "verified-skill", body="# Original\n")
    content = ApplicationSkillVerifiedContent.from_mapping({"SKILL.md": (root / "SKILL.md").read_bytes()})
    package = ApplicationSkillCatalog().package_from_verified_content(content, package_root=root)
    loader = ApplicationSkillPackageLoader()

    with pytest.raises(ApplicationSkillError, match="contract drifted"):
        loader.load_instructions(replace(package, description="caller altered metadata"))
    with pytest.raises(ApplicationSkillError, match="contract drifted"):
        loader.load_instructions(replace(package, fingerprint="0" * 64))

    changed = ApplicationSkillVerifiedContent.from_mapping({
        "SKILL.md": b"---\nname: verified-skill\ndescription: changed immutable content\n---\n\n# Changed\n",
    })
    with pytest.raises(ApplicationSkillError, match="contract drifted"):
        loader.load_instructions(replace(package, verified_content=changed))
    with pytest.raises(ApplicationSkillError, match="contract drifted"):
        ApplicationSkillCatalog().snapshot_from_packages((replace(package, description="catalog drift"),))


def test_verified_content_obeys_the_same_resource_rules_and_limits_as_disk_packages(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _skill(source, "equivalent-skill")
    (root / "agents").mkdir()
    (root / "agents" / "openai.yaml").write_text("display_name: Equivalent\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "opaque.bin").write_bytes(b"\xff\x00")
    disk = _discover(source).packages[0]
    verified = ApplicationSkillVerifiedContent.from_mapping({
        "SKILL.md": (root / "SKILL.md").read_bytes(),
        "agents/openai.yaml": (root / "agents" / "openai.yaml").read_bytes(),
        "assets/opaque.bin": (root / "assets" / "opaque.bin").read_bytes(),
    })
    frozen = ApplicationSkillCatalog().package_from_verified_content(verified, package_root=root)

    assert frozen.fingerprint == disk.fingerprint
    assert frozen.resources == disk.resources
    with pytest.raises(ApplicationSkillError, match="agents may only"):
        ApplicationSkillCatalog().package_from_verified_content({
            "SKILL.md": (root / "SKILL.md").read_bytes(),
            "agents/other.yaml": b"not allowed\n",
        })
    with pytest.raises(ApplicationSkillError, match="hidden"):
        ApplicationSkillVerifiedContent.from_mapping({
            "SKILL.md": (root / "SKILL.md").read_bytes(), ".hidden": b"x",
        })
    too_many = {"SKILL.md": (root / "SKILL.md").read_bytes()}
    too_many.update({f"references/item-{index}.md": b"x" for index in range(128)})
    with pytest.raises(ApplicationSkillError, match="file count"):
        ApplicationSkillVerifiedContent.from_mapping(too_many)
    with pytest.raises(ApplicationSkillError, match="package size"):
        ApplicationSkillVerifiedContent.from_mapping({
            "SKILL.md": (root / "SKILL.md").read_bytes(),
            "assets/one.bin": b"x" * (1024 * 1024),
            "assets/two.bin": b"y" * (1024 * 1024),
        })


def test_snapshot_from_packages_keeps_duplicate_identity_fail_closed(tmp_path: Path) -> None:
    first = _skill(tmp_path / "one", "shared-frozen")
    second = _skill(tmp_path / "two", "shared-frozen")
    catalog = ApplicationSkillCatalog()
    frozen = catalog.package_from_verified_content(
        {"SKILL.md": (first / "SKILL.md").read_bytes()}, source_id="external-one",
    )
    filesystem = catalog.inspect_package(second, source_id="filesystem-two")

    snapshot = catalog.snapshot_from_packages((frozen, filesystem), scanned_source_count=2)

    assert snapshot.packages == ()
    assert [issue.code for issue in snapshot.issues] == ["duplicate_identity", "duplicate_identity"]
