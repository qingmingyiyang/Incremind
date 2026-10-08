"""Fail-closed, memory-only ZIP intake for external extension artifacts.

The archive is an untrusted transport format.  This adapter deliberately does
not extract to a filesystem or invoke any archive member: it only turns a
strictly bounded, ordinary-file ZIP into the immutable artifact contract.
"""

from __future__ import annotations

import io
import stat
import struct
import unicodedata
import zipfile
from pathlib import PurePosixPath
from typing import Final

from core.external_extensions import ArtifactInventory


class SecureArchiveError(ValueError):
    """Raised when an untrusted ZIP cannot safely become artifact evidence."""


_MAX_ARCHIVE_BYTES: Final = 32 * 1024 * 1024
_MAX_FILES: Final = 512
_MAX_FILE_BYTES: Final = 2 * 1024 * 1024
_MAX_TOTAL_BYTES: Final = 16 * 1024 * 1024
_MAX_COMPRESSION_RATIO: Final = 100
_MAX_CENTRAL_DIRECTORY_BYTES: Final = 512 * 1024
_MAX_PATH_BYTES: Final = 320
_EOCD_SIGNATURE: Final = b"PK\x05\x06"
_LOCAL_HEADER_SIGNATURE: Final = b"PK\x03\x04"
_CENTRAL_HEADER_SIGNATURE: Final = b"PK\x01\x02"
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def zip_bytes_to_inventory(
    payload: bytes,
    *,
    subpath: str | None = None,
    expected_github_root: str | None = None,
) -> ArtifactInventory:
    """Return a bounded inventory from a safe ZIP payload without touching disk."""

    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_ARCHIVE_BYTES:
        raise SecureArchiveError("ZIP payload exceeds the intake limit")
    selected_subpath = _safe_relative_path(subpath, label="archive subpath") if subpath is not None else None
    expected_root = _expected_root(expected_github_root)
    central_offset, expected_entries = _preflight_central_directory(payload)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
    except (OSError, ValueError, EOFError, struct.error, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise SecureArchiveError("ZIP archive is unsafe") from error

    try:
        if archive.start_dir != central_offset:
            raise SecureArchiveError("ZIP central directory identity drifted")
        entries = archive.infolist()
        if len(entries) != expected_entries:
            raise SecureArchiveError("ZIP central directory entry count drifted")
        members = _validate_members(entries, payload)
        files = _read_members(archive, members)
    except (OSError, RuntimeError, ValueError, EOFError, struct.error, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        if isinstance(error, SecureArchiveError):
            raise
        raise SecureArchiveError("ZIP archive is unsafe") from error
    finally:
        archive.close()

    if expected_root is not None:
        files = _strip_expected_root(files, expected_root)
    if selected_subpath is not None:
        files = _select_subpath(files, selected_subpath)
    if not files:
        raise SecureArchiveError("ZIP selection contains no files")
    try:
        return ArtifactInventory.capture(files)
    except ValueError as error:
        raise SecureArchiveError("ZIP artifact inventory is invalid") from error


def _validate_members(entries: list[zipfile.ZipInfo], payload: bytes) -> tuple[tuple[str, zipfile.ZipInfo], ...]:
    files: list[tuple[str, zipfile.ZipInfo]] = []
    seen: dict[str, bool] = {}
    total_declared = 0
    for info in entries:
        local_name = _local_member_name(payload, info)
        if local_name != info.orig_filename:
            raise SecureArchiveError("ZIP local and central member names do not match")
        _safe_member_path(local_name, directory=info.is_dir())
        raw_name = info.filename
        is_directory = raw_name.endswith("/")
        path = _safe_member_path(raw_name, directory=is_directory)
        _assert_ordinary_member(info, is_directory=is_directory)
        platform_key = _platform_path_key(path)
        if platform_key in seen:
            raise SecureArchiveError("ZIP archive contains a cross-platform path collision")
        ancestors = tuple(
            "/".join(platform_key.split("/")[:index])
            for index in range(1, len(platform_key.split("/")))
        )
        if any(seen.get(ancestor) is False for ancestor in ancestors):
            raise SecureArchiveError("ZIP archive contains a file-directory path collision")
        if not is_directory and any(existing.startswith(platform_key + "/") for existing in seen):
            raise SecureArchiveError("ZIP archive contains a file-directory path collision")
        seen[platform_key] = is_directory
        if is_directory:
            continue
        if info.file_size < 0 or info.file_size > _MAX_FILE_BYTES:
            raise SecureArchiveError("ZIP member exceeds the file-size limit")
        total_declared += info.file_size
        if total_declared > _MAX_TOTAL_BYTES:
            raise SecureArchiveError("ZIP archive exceeds the expanded-size limit")
        _assert_compression_ratio(info)
        files.append((path, info))
    return tuple(files)


def _safe_member_path(value: str, *, directory: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SecureArchiveError("ZIP member path is invalid")
    value = unicodedata.normalize("NFC", value)
    if directory:
        value = value[:-1]
    if not value or len(value.encode("utf-8")) > _MAX_PATH_BYTES or "\\" in value or ":" in value:
        raise SecureArchiveError("ZIP member path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."}
        or part.rstrip(" .") != part
        or any(ord(character) < 32 or ord(character) == 127 or character in '<>"|?*' for character in part)
        or _is_windows_reserved(part)
        for part in path.parts
    ):
        raise SecureArchiveError("ZIP member path is unsafe")
    return path.as_posix()


def _safe_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value.endswith("/"):
        raise SecureArchiveError(f"{label} is invalid")
    try:
        return _safe_member_path(value, directory=False)
    except SecureArchiveError as error:
        raise SecureArchiveError(f"{label} is unsafe") from error


def _local_member_name(payload: bytes, info: zipfile.ZipInfo) -> str:
    """Read the unnormalised local-header name before Python can rewrite it."""

    offset = info.header_offset
    if offset < 0 or offset + 30 > len(payload) or payload[offset : offset + 4] != _LOCAL_HEADER_SIGNATURE:
        raise SecureArchiveError("ZIP local header is invalid")
    _version, flags, compression, _time, _date, crc, compressed_size, file_size, name_length, extra_length = struct.unpack_from(
        "<HHHHHIIIHH", payload, offset + 4
    )
    start = offset + 30
    end = start + name_length
    if end > len(payload) or end + extra_length > len(payload):
        raise SecureArchiveError("ZIP local header is truncated")
    _assert_extra_fields(payload[end : end + extra_length])
    if flags & 0x8 or (
        flags,
        compression,
        crc,
        compressed_size,
        file_size,
    ) != (info.flag_bits, info.compress_type, info.CRC, info.compress_size, info.file_size):
        raise SecureArchiveError("ZIP local and central headers do not match")
    try:
        return payload[start:end].decode("utf-8" if flags & 0x800 else "cp437", "strict")
    except UnicodeError as error:
        raise SecureArchiveError("ZIP member name is invalid") from error


def _assert_ordinary_member(info: zipfile.ZipInfo, *, is_directory: bool) -> None:
    if info.flag_bits & 0x1:
        raise SecureArchiveError("encrypted ZIP members are forbidden")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise SecureArchiveError("ZIP compression method is forbidden")
    if info.volume != 0:
        raise SecureArchiveError("ZIP multi-disk members are forbidden")
    _assert_extra_fields(info.extra)
    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise SecureArchiveError("ZIP member is not an ordinary file")
    if is_directory:
        if file_type not in {0, stat.S_IFDIR}:
            raise SecureArchiveError("ZIP directory member has an unsafe type")
    elif file_type == stat.S_IFDIR:
        raise SecureArchiveError("ZIP file member has an unsafe type")


def _assert_compression_ratio(info: zipfile.ZipInfo) -> None:
    if info.compress_size < 0:
        raise SecureArchiveError("ZIP member size is invalid")
    if info.file_size == 0:
        return
    if info.compress_size == 0 or info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO:
        raise SecureArchiveError("ZIP member compression ratio exceeds the intake limit")


def _read_members(archive: zipfile.ZipFile, members: tuple[tuple[str, zipfile.ZipInfo], ...]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path, info in members:
        try:
            content = archive.read(info)
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise SecureArchiveError("ZIP member cannot be read safely") from error
        if len(content) != info.file_size:
            raise SecureArchiveError("ZIP member expanded size does not match its header")
        files[path] = content
    return files


def _expected_root(value: str | None) -> str | None:
    if value is None:
        return None
    root = _safe_relative_path(value, label="expected GitHub root")
    if "/" in root:
        raise SecureArchiveError("expected GitHub root must be a single path segment")
    return root


def _strip_expected_root(files: dict[str, bytes], root: str) -> dict[str, bytes]:
    prefix = root + "/"
    if not files or any(not path.startswith(prefix) for path in files):
        raise SecureArchiveError("ZIP archive does not match the expected GitHub root")
    return {path.removeprefix(prefix): content for path, content in files.items()}


def _platform_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _is_windows_reserved(part: str) -> bool:
    normalized = unicodedata.normalize("NFKC", part)
    base = normalized.split(".", 1)[0].rstrip(" .").upper()
    return base in _WINDOWS_RESERVED_NAMES


def _assert_extra_fields(value: bytes) -> None:
    if not isinstance(value, bytes):
        raise SecureArchiveError("ZIP extra fields are invalid")
    cursor = 0
    while cursor < len(value):
        if cursor + 4 > len(value):
            raise SecureArchiveError("ZIP extra fields are invalid")
        field_id, field_size = struct.unpack_from("<HH", value, cursor)
        cursor += 4
        end = cursor + field_size
        if end > len(value):
            raise SecureArchiveError("ZIP extra fields are invalid")
        if field_id == 0x0001:
            raise SecureArchiveError("ZIP64 extra fields are forbidden")
        cursor = end


def _select_subpath(files: dict[str, bytes], subpath: str) -> dict[str, bytes]:
    prefix = subpath + "/"
    selected = {path.removeprefix(prefix): content for path, content in files.items() if path.startswith(prefix)}
    if not selected:
        raise SecureArchiveError("ZIP archive subpath contains no files")
    return selected


def _preflight_central_directory(payload: bytes) -> tuple[int, int]:
    """Reject oversized or ambiguous central directories before ``ZipFile`` parses them."""

    minimum = 22
    search_start = max(0, len(payload) - (minimum + 65_535))
    end_offset: int | None = None
    fields: tuple[int, int, int, int, int, int] | None = None
    for offset in range(len(payload) - minimum, search_start - 1, -1):
        if payload[offset : offset + 4] != _EOCD_SIGNATURE:
            continue
        try:
            (
                _signature,
                disk_number,
                central_disk,
                disk_entries,
                total_entries,
                central_size,
                central_offset,
                comment_length,
            ) = struct.unpack_from("<4s4H2LH", payload, offset)
        except struct.error:
            continue
        if offset + minimum + comment_length != len(payload):
            continue
        end_offset = offset
        fields = (
            disk_number,
            central_disk,
            disk_entries,
            total_entries,
            central_size,
            central_offset,
        )
        break
    if end_offset is None or fields is None:
        raise SecureArchiveError("ZIP end-of-central-directory record is invalid")
    disk_number, central_disk, disk_entries, total_entries, central_size, central_offset = fields
    if (
        disk_number != 0
        or central_disk != 0
        or disk_entries != total_entries
        or total_entries in {0, 0xFFFF}
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise SecureArchiveError("ZIP multi-disk, empty, or ZIP64 archives are forbidden")
    if total_entries > _MAX_FILES:
        raise SecureArchiveError("ZIP archive exceeds the member-count limit")
    if central_size > _MAX_CENTRAL_DIRECTORY_BYTES:
        raise SecureArchiveError("ZIP central directory exceeds the intake limit")
    if (
        central_offset < 4
        or central_offset + central_size != end_offset
        or payload[:4] != _LOCAL_HEADER_SIGNATURE
        or payload[central_offset : central_offset + 4] != _CENTRAL_HEADER_SIGNATURE
    ):
        raise SecureArchiveError("ZIP central directory layout is invalid")
    _preflight_central_entries(
        payload,
        central_offset=central_offset,
        central_size=central_size,
        total_entries=total_entries,
    )
    return central_offset, total_entries


def _preflight_central_entries(
    payload: bytes,
    *,
    central_offset: int,
    central_size: int,
    total_entries: int,
) -> None:
    cursor = central_offset
    limit = central_offset + central_size
    for _index in range(total_entries):
        if cursor + 46 > limit or payload[cursor : cursor + 4] != _CENTRAL_HEADER_SIGNATURE:
            raise SecureArchiveError("ZIP central directory entry is invalid")
        name_length, extra_length, comment_length, disk_start = struct.unpack_from(
            "<4H", payload, cursor + 28
        )
        entry_end = cursor + 46 + name_length + extra_length + comment_length
        if entry_end > limit or disk_start != 0:
            raise SecureArchiveError("ZIP central directory contains a multi-disk member")
        extra_start = cursor + 46 + name_length
        _assert_extra_fields(payload[extra_start : extra_start + extra_length])
        cursor = entry_end
    if cursor != limit:
        raise SecureArchiveError("ZIP64 locator, record, or trailing central data is forbidden")
