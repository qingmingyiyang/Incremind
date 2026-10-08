"""把已审阅的技能字节写入显式目录，不覆盖或安装客户端内容。"""
import os
from pathlib import Path, PurePosixPath
import re

from core.application_skill.package_catalog import ApplicationSkillVerifiedContent
from core.external_extension_runtime.secure_archive import _safe_member_path
from core.external_extension_runtime.skill_materializer import _is_allowed_package_path
from core.external_extension_runtime.windows_handle_io import WindowsHandleIoError, WindowsHandleTreeIo


def _reviewed_files(files):
    if not isinstance(files, dict) or 'SKILL.md' not in files:
        raise ValueError('invalid_skill_folder')
    try:
        frozen = ApplicationSkillVerifiedContent.from_mapping(files).file_mapping()
        keys = set()
        for path in frozen:
            safe = _safe_member_path(path, directory=False)
            if safe != path or not _is_allowed_package_path(safe) or safe.casefold() in keys:
                raise ValueError('invalid_skill_folder')
            keys.add(safe.casefold())
        for path in frozen:
            if any(parent.as_posix().casefold() in keys for parent in PurePosixPath(path).parents):
                raise ValueError('invalid_skill_folder')
        return frozen
    except (ValueError, TypeError):
        raise ValueError('invalid_skill_folder') from None


def write_reviewed_folder(directory: str, name: str, files: dict[str, bytes]) -> str:
    """委托原句柄服务创建产物，已有产物只有完整字节一致才算幂等。"""
    if os.name != 'nt':
        raise ValueError('skill_folder_unsupported')
    if (not isinstance(directory, str) or not directory
            or any(ord(character) < 32 or ord(character) == 127 for character in directory)
            or not isinstance(name, str) or len(name) > 64
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)):
        raise ValueError('invalid_skill_folder')
    try:
        if _safe_member_path(name, directory=False) != name:
            raise ValueError('invalid_skill_folder')
        root = Path(directory)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('invalid_skill_folder')
    except (ValueError, OSError):
        raise ValueError('invalid_skill_folder') from None
    frozen = _reviewed_files(files)
    target, owner = root / name, WindowsHandleTreeIo()
    try:
        created = owner.write_new_tree(root, (name,), frozen)
    except (OSError, WindowsHandleIoError):
        code = 'skill_folder_exists' if os.path.lexists(target) else 'skill_folder_write_failed'
        raise ValueError(code) from None
    # 原写入服务只验证尺寸；这里再读真实字节，已有目录也不能借尺寸相同冒充一致。
    try:
        actual = owner.read_exact_tree(root, (name,), tuple(frozen),
            expected_sizes={path: len(body) for path, body in frozen.items()})
    except (OSError, WindowsHandleIoError):
        raise ValueError('skill_folder_write_failed' if created else 'skill_folder_exists') from None
    if actual != frozen:
        raise ValueError('skill_folder_write_failed' if created else 'skill_folder_exists')
    return str(target)
