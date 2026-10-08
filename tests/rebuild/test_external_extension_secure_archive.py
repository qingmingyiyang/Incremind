from __future__ import annotations

import io
import stat
import zipfile

import pytest

import core.external_extension_runtime.secure_archive as secure_archive
from core.external_extension_runtime.secure_archive import SecureArchiveError, zip_bytes_to_inventory


def _zip(files: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_preserves_single_root_by_default_and_strips_only_an_explicit_expected_github_root() -> None:
    payload = _zip(
        {
            "fixture-main/SKILL.md": b"---\nname: fixture\ndescription: test\n---\nUse fixture.\n",
            "fixture-main/references/example.md": b"safe data",
        }
    )

    inventory = zip_bytes_to_inventory(payload)

    assert inventory.paths == ("fixture-main/SKILL.md", "fixture-main/references/example.md")
    stripped = zip_bytes_to_inventory(payload, expected_github_root="fixture-main")
    assert stripped.paths == ("SKILL.md", "references/example.md")
    assert stripped.read_bytes("SKILL.md").startswith(b"---")


def test_selects_a_monorepo_subpath_after_github_root_is_removed() -> None:
    payload = _zip(
        {
            "extensions-v1/packages/weather/SKILL.md": b"weather",
            "extensions-v1/packages/weather/assets/icon.txt": b"icon",
            "extensions-v1/packages/other/SKILL.md": b"other",
        }
    )

    inventory = zip_bytes_to_inventory(
        payload,
        expected_github_root="extensions-v1",
        subpath="packages/weather",
    )

    assert inventory.paths == ("SKILL.md", "assets/icon.txt")


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "C:/drive", "dir\\child", "dir/NUL.txt", "dir/trailing. ", "dir/../escape"],
)
def test_rejects_zip_slip_and_windows_unsafe_paths(path: str) -> None:
    if path == "dir\\child":
        # zipfile normalizes native Windows separators while writing, so create
        # an untrusted wire-format name directly to exercise intake validation.
        payload = _zip({"dir/child": b"x"}).replace(b"dir/child", b"dir\\child")
    else:
        payload = _zip({path: b"x"})
    with pytest.raises(SecureArchiveError, match="path"):
        zip_bytes_to_inventory(payload)


def test_rejects_encrypted_and_non_regular_members() -> None:
    encrypted_payload = bytearray(_zip({"encrypted.txt": b"x"}, compression=zipfile.ZIP_STORED))
    local = encrypted_payload.index(b"PK\x03\x04")
    central = encrypted_payload.index(b"PK\x01\x02")
    encrypted_payload[local + 6] |= 0x1
    encrypted_payload[central + 8] |= 0x1
    with pytest.raises(SecureArchiveError, match="encrypted"):
        zip_bytes_to_inventory(bytes(encrypted_payload))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"target")
    with pytest.raises(SecureArchiveError, match="ordinary"):
        zip_bytes_to_inventory(output.getvalue())


def test_rejects_duplicate_paths_and_resource_bombs() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("same.txt", b"first")
        archive.writestr("same.txt", b"second")
    with pytest.raises(SecureArchiveError, match="collision"):
        zip_bytes_to_inventory(output.getvalue())

    with pytest.raises(SecureArchiveError, match="compression"):
        zip_bytes_to_inventory(_zip({"bomb.txt": b"0" * (1024 * 1024)}))
    with pytest.raises(SecureArchiveError, match="file-size"):
        zip_bytes_to_inventory(_zip({"large.bin": b"x" * (2 * 1024 * 1024 + 1)}, compression=zipfile.ZIP_STORED))


def test_rejects_forged_size_headers_and_aggregate_limits() -> None:
    forged = bytearray(_zip({"declared.txt": b"0123456789"}, compression=zipfile.ZIP_STORED))
    local = forged.index(b"PK\x03\x04")
    central = forged.index(b"PK\x01\x02")
    # Alter both headers consistently enough to pass basic ZIP indexing, while
    # leaving the actual member bytes and CRC as evidence of the forgery.
    forged[local + 22 : local + 26] = (1).to_bytes(4, "little")
    forged[central + 24 : central + 28] = (1).to_bytes(4, "little")
    with pytest.raises(SecureArchiveError, match="cannot be read|expanded size"):
        zip_bytes_to_inventory(bytes(forged))

    too_many = {f"files/{index}.txt": b"x" for index in range(513)}
    with pytest.raises(SecureArchiveError, match="member-count"):
        zip_bytes_to_inventory(_zip(too_many))

    with pytest.raises(SecureArchiveError, match="payload"):
        zip_bytes_to_inventory(b"x" * (32 * 1024 * 1024 + 1))


def test_rejects_invalid_subpath_and_empty_selection() -> None:
    payload = _zip({"repo-main/SKILL.md": b"skill"})
    with pytest.raises(SecureArchiveError, match="subpath"):
        zip_bytes_to_inventory(payload, subpath="../escape")
    with pytest.raises(SecureArchiveError, match="subpath contains no files"):
        zip_bytes_to_inventory(payload, subpath="packages/missing")


def test_rejects_cross_platform_name_collisions_and_windows_forbidden_names() -> None:
    collision = _zip({"docs/Caf\u00e9.txt": b"first", "docs/Cafe\u0301.txt": b"second"})
    with pytest.raises(SecureArchiveError, match="collision"):
        zip_bytes_to_inventory(collision)
    case_collision = _zip({"Skill.md": b"first", "skill.md": b"second"})
    with pytest.raises(SecureArchiveError, match="collision"):
        zip_bytes_to_inventory(case_collision)

    for path in (
        "dir/a?.txt",
        "dir/a\x01.txt",
        "dir/COM\u00b9.txt",
        "dir/LPT\u00b3.log",
        "dir/CONOUT$.txt",
        "dir/CON .txt",
        "dir/COM1 .log",
    ):
        with pytest.raises(SecureArchiveError, match="path"):
            zip_bytes_to_inventory(_zip({path: b"x"}))


def test_rejects_special_types_across_creator_systems_and_forbidden_compression() -> None:
    for creator_system in (0, 3):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            special = zipfile.ZipInfo(f"special-{creator_system}")
            special.create_system = creator_system
            special.external_attr = (stat.S_IFIFO | 0o600) << 16
            archive.writestr(special, b"x")
        with pytest.raises(SecureArchiveError, match="ordinary"):
            zip_bytes_to_inventory(output.getvalue())

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("unsupported.txt", b"x")
    with pytest.raises(SecureArchiveError, match="compression"):
        zip_bytes_to_inventory(output.getvalue())


def test_rejects_local_and_central_header_disagreement_and_wrong_expected_root() -> None:
    payload = bytearray(_zip({"safe.txt": b"content"}, compression=zipfile.ZIP_STORED))
    local = payload.index(b"PK\x03\x04")
    payload[local + 8 : local + 10] = zipfile.ZIP_DEFLATED.to_bytes(2, "little")
    with pytest.raises(SecureArchiveError, match="headers do not match"):
        zip_bytes_to_inventory(bytes(payload))

    with pytest.raises(SecureArchiveError, match="expected GitHub root"):
        zip_bytes_to_inventory(_zip({"fixture-main/SKILL.md": b"x"}), expected_github_root="other-main")


def test_preflights_member_count_before_standard_library_archive_parsing(monkeypatch) -> None:
    payload = bytearray(_zip({"safe.txt": b"x"}, compression=zipfile.ZIP_STORED))
    eocd = payload.rfind(b"PK\x05\x06")
    payload[eocd + 8 : eocd + 10] = (513).to_bytes(2, "little")
    payload[eocd + 10 : eocd + 12] = (513).to_bytes(2, "little")
    monkeypatch.setattr(
        secure_archive.zipfile,
        "ZipFile",
        lambda *_args, **_kwargs: pytest.fail("ZipFile parsed an over-budget central directory"),
    )
    with pytest.raises(SecureArchiveError, match="member-count"):
        zip_bytes_to_inventory(bytes(payload))


def test_rejects_overlong_and_file_directory_collision_paths() -> None:
    with pytest.raises(SecureArchiveError, match="path"):
        zip_bytes_to_inventory(_zip({"a" * 321: b"x"}))
    with pytest.raises(SecureArchiveError, match="file-directory"):
        zip_bytes_to_inventory(_zip({"docs": b"file", "DOCS/readme.md": b"child"}))


def test_rejects_zip64_extra_fields_and_nonzero_member_volume() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
        with archive.open("forced-zip64.txt", "w", force_zip64=True) as member:
            member.write(b"x")
    with pytest.raises(SecureArchiveError, match="ZIP64"):
        zip_bytes_to_inventory(output.getvalue())

    payload = bytearray(_zip({"safe.txt": b"x"}, compression=zipfile.ZIP_STORED))
    central = payload.index(b"PK\x01\x02")
    payload[central + 34 : central + 36] = (1).to_bytes(2, "little")
    with pytest.raises(SecureArchiveError, match="multi-disk"):
        zip_bytes_to_inventory(bytes(payload))


def test_rejects_central_zip64_extra_locator_and_trailing_central_data() -> None:
    central_extra = bytearray(_zip({"safe.txt": b"x"}, compression=zipfile.ZIP_STORED))
    central = central_extra.index(b"PK\x01\x02")
    eocd = central_extra.rfind(b"PK\x05\x06")
    name_length = int.from_bytes(central_extra[central + 28 : central + 30], "little")
    extra_length = int.from_bytes(central_extra[central + 30 : central + 32], "little")
    insert_at = central + 46 + name_length + extra_length
    central_extra[insert_at:insert_at] = b"\x01\x00\x00\x00"
    central_extra[central + 30 : central + 32] = (extra_length + 4).to_bytes(2, "little")
    eocd += 4
    central_size = int.from_bytes(central_extra[eocd + 12 : eocd + 16], "little")
    central_extra[eocd + 12 : eocd + 16] = (central_size + 4).to_bytes(4, "little")
    with pytest.raises(SecureArchiveError, match="ZIP64"):
        zip_bytes_to_inventory(bytes(central_extra))

    locator = bytearray(_zip({"safe.txt": b"x"}, compression=zipfile.ZIP_STORED))
    eocd = locator.rfind(b"PK\x05\x06")
    locator[eocd:eocd] = b"PK\x06\x07" + b"\x00" * 16
    with pytest.raises(SecureArchiveError, match="layout"):
        zip_bytes_to_inventory(bytes(locator))

    trailing = bytearray(_zip({"safe.txt": b"x"}, compression=zipfile.ZIP_STORED))
    eocd = trailing.rfind(b"PK\x05\x06")
    central_size = int.from_bytes(trailing[eocd + 12 : eocd + 16], "little")
    trailing[eocd:eocd] = b"JUNK"
    eocd += 4
    trailing[eocd + 12 : eocd + 16] = (central_size + 4).to_bytes(4, "little")
    with pytest.raises(SecureArchiveError, match="trailing central data"):
        zip_bytes_to_inventory(bytes(trailing))
