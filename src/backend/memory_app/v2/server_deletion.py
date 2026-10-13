"""管理员永久删除的协调边界，停用与作废仍保留原数据。"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
from pathlib import Path
import stat

from backend.security.user_context import UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.vault_backup_restore import _native_io_path
from .devices import DEVICES, PAIRINGS, PRESENCE
from .server_users import DELETIONS, USERS


_KNOWN = {USERS, DEVICES, PAIRINGS, PRESENCE, DELETIONS, 'server_job_usage', 'admin_audit'}
LOGGER = logging.getLogger(__name__)


def _linked(value):
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value, 'st_file_attributes', 0) & 0x400)


def _identity(value):
    return [value.st_dev, value.st_ino]


def _mount_id(descriptor):
    # 同盘 bind mount 的 st_dev 相同；以已绑定 fd 的内核挂载身份判断边界。
    try:
        rows = (Path('/proc/self/fdinfo') / str(descriptor)).read_text(encoding='ascii').splitlines()
        values = [row.split(':', 1)[1].strip() for row in rows if row.startswith('mnt_id:')]
        if len(values) != 1 or not values[0].isdecimal() or int(values[0]) < 1:
            raise ValueError
        return int(values[0])
    except (OSError, UnicodeError, ValueError):
        raise UserError('user_delete_path_invalid') from None


def _directory(path):
    value = path.lstat()
    if _linked(value) or not stat.S_ISDIR(value.st_mode):
        raise UserError('user_delete_path_invalid')
    return value


def _target(users, user_id):
    # 不使用 root_for 的 resolve 结果进行删除，避免链接到另一位用户。
    users.get(user_id)
    root = users.server_root
    for node in (*reversed(root.parents), root):
        _directory(node)
    parent = root / 'users'
    _directory(parent)
    target = parent / user_id
    if target.parent != parent or target in (root, parent) or '..' in target.parts:
        raise UserError('user_delete_path_invalid')
    try:
        information = _directory(target)
    except FileNotFoundError:
        return target, None
    if os.path.ismount(target):
        raise UserError('user_delete_path_invalid')
    return target, _identity(information)


def _inspect_tree(path, *, device=None):
    # 与备份共用扩展路径转换；正常 Win32 路径无法 stat 真实备份中的长文件。
    if device is None and os.name == 'nt':
        path = Path(_native_io_path(path))
    device = path.lstat().st_dev if device is None else device
    for child in path.iterdir():
        information = child.lstat()
        if _linked(information) or information.st_dev != device:
            raise UserError('user_delete_path_invalid')
        if stat.S_ISDIR(information.st_mode):
            if os.path.ismount(child):
                raise UserError('user_delete_path_invalid')
            _inspect_tree(child, device=device)
        elif not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
            raise UserError('user_delete_path_invalid')


def _delete_posix(target, identity):
    # 从文件系统根逐级绑定 fd；子目录和文件都相对已打开的父目录操作。
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    handles = []
    try:
        current = os.open(target.anchor, flags)
        handles.append(current)
        _mount_id(current)
        for component in target.parts[1:-1]:
            current = os.open(component, flags, dir_fd=current)
            handles.append(current)
            _mount_id(current)
        try:
            selected = os.open(target.name, flags, dir_fd=current)
        except FileNotFoundError:
            return
        handles.append(selected)
        if _identity(os.fstat(selected)) != identity:
            raise UserError('user_delete_path_invalid')
        mount = _mount_id(current)
        if _mount_id(selected) != mount:
            raise UserError('user_delete_path_invalid')

        def walk(directory, *, remove):
            if _mount_id(directory) != mount:
                raise UserError('user_delete_path_invalid')
            for name in os.listdir(directory):
                information = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _linked(information) or information.st_dev != identity[0]:
                    raise UserError('user_delete_path_invalid')
                if stat.S_ISDIR(information.st_mode):
                    descriptor = os.open(name, flags, dir_fd=directory)
                    try:
                        if _identity(os.fstat(descriptor)) != _identity(information):
                            raise UserError('user_delete_path_invalid')
                        if _mount_id(descriptor) != mount:
                            raise UserError('user_delete_path_invalid')
                        walk(descriptor, remove=remove)
                        if remove:
                            if _mount_id(descriptor) != mount or _mount_id(directory) != mount:
                                raise UserError('user_delete_path_invalid')
                            if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(information):
                                raise UserError('user_delete_path_invalid')
                            os.rmdir(name, dir_fd=directory)
                    finally:
                        os.close(descriptor)
                elif stat.S_ISREG(information.st_mode):
                    if information.st_nlink != 1:
                        raise UserError('user_delete_path_invalid')
                    if remove:
                        os.unlink(name, dir_fd=directory)
                else:
                    raise UserError('user_delete_path_invalid')
            if remove:
                os.fsync(directory)

        walk(selected, remove=False)
        walk(selected, remove=True)
        if _mount_id(selected) != mount or _mount_id(current) != mount:
            raise UserError('user_delete_path_invalid')
        if _identity(os.stat(target.name, dir_fd=current, follow_symlinks=False)) != identity:
            raise UserError('user_delete_path_invalid')
        os.rmdir(target.name, dir_fd=current)
        os.fsync(current)
    finally:
        for descriptor in reversed(handles):
            os.close(descriptor)


def _delete_windows(target, identity):
    # 复用原生 NT 的相对句柄和 OBJ_DONT_REPARSE；删除指向实际句柄，不重解路径。
    from core.external_extension_runtime.windows_handle_io import (
        _Nt, _IoStatus, _Unicode, _ObjectAttributes, _NOT_FOUND, _status, WindowsHandleIoError,
    )

    class UserTreeNt(_Nt):
        def _create(self, parent, name, access, disposition, options):
            # Nt 的 Length 按 UTF-16 字节计；emoji 等字符在 Python 中只计一个字符。
            length = len(name.encode('utf-16-le'))
            if length > 65532:
                raise UserError('user_delete_path_invalid')
            unit = _Unicode(length, length + 2, name)
            attributes = _ObjectAttributes(ctypes.sizeof(_ObjectAttributes), ctypes.c_void_p(parent or 0),
                ctypes.pointer(unit), 0x40 | 0x1000, None, None)
            handle, io = ctypes.c_void_p(), _IoStatus()
            status = int(self.dll.NtCreateFile(ctypes.byref(handle), access, ctypes.byref(attributes),
                ctypes.byref(io), None, 0, 7, disposition, options, None, 0))
            if status in _NOT_FOUND:
                return 0
            _status(status, 'NtCreateFile')
            return int(handle.value or 0)

    api = UserTreeNt()
    handles = []
    try:
        parent = api.open_root(Path(target.anchor))
        if not parent:
            raise UserError('user_delete_path_invalid')
        handles.append(parent)
        for component in target.parts[1:-1]:
            # 祖先只用于寻址，不请求删除权限，避免与其它应用的合法目录句柄冲突。
            parent = api._create(parent, component, 1 | 0x80 | 0x100000,
                                 1, 1 | 0x20 | 0x00200000)
            if not parent or api.is_reparse(parent) or not api.is_dir(parent):
                raise UserError('user_delete_path_invalid')
            handles.append(parent)
        selected = api.open_dir(parent, target.name)
        if not selected:
            return
        handles.append(selected)
        if api.is_reparse(selected) or not api.is_dir(selected):
            raise UserError('user_delete_path_invalid')
        # 原 NtQueryInformationFile 的 FileInternalInformation 给出同一文件索引。
        index, io = ctypes.c_longlong(), _IoStatus()
        status = int(api.dll.NtQueryInformationFile(ctypes.c_void_p(selected), ctypes.byref(io),
                     ctypes.byref(index), ctypes.sizeof(index), 6))
        if status < 0 or index.value != identity[1]:
            raise UserError('user_delete_path_invalid')

        def dispose(handle):
            flag, io = ctypes.c_ubyte(1), _IoStatus()
            status = int(api.dll.NtSetInformationFile(ctypes.c_void_p(handle), ctypes.byref(io),
                         ctypes.byref(flag), ctypes.sizeof(flag), 13))
            if status < 0:
                raise UserError('user_delete_incomplete')

        def walk(directory, *, remove):
            for name in api.names(directory, maximum=1_000_000):
                # FILE_OPEN 不带目录/文件类型限定，随后核验真实类型。
                handle = api._create(directory, name, 0x10000 | 0x80 | 0x100000 | 1,
                                     1, 0x20 | 0x00200000)
                if not handle:
                    raise UserError('user_delete_path_invalid')
                try:
                    if api.is_reparse(handle):
                        raise UserError('user_delete_path_invalid')
                    if api.is_dir(handle):
                        walk(handle, remove=remove)
                    else:
                        standard, io = (ctypes.c_ubyte * 32)(), _IoStatus()
                        status = int(api.dll.NtQueryInformationFile(ctypes.c_void_p(handle), ctypes.byref(io),
                                     ctypes.byref(standard), len(standard), 5))
                        if status < 0 or int.from_bytes(bytes(standard[16:20]), 'little') != 1:
                            raise UserError('user_delete_path_invalid')
                    if remove:
                        dispose(handle)
                finally:
                    api.close(handle)

        walk(selected, remove=False)
        walk(selected, remove=True)
        dispose(selected)
    except WindowsHandleIoError:
        raise UserError('user_delete_path_invalid') from None
    finally:
        for handle in reversed(handles):
            api.close(handle)


def remove_user_tree(users, user_id, identity):
    target, observed = _target(users, user_id)
    if observed is None:
        return
    if identity is None or observed != identity:
        raise UserError('user_delete_path_invalid')
    if os.name == 'nt':
        _delete_windows(target, identity)
    else:
        _delete_posix(target, identity)
    if target.exists() or target.is_symlink():
        raise UserError('user_delete_incomplete')


def _references(value, identities):
    if isinstance(value, str):
        return value in identities
    if isinstance(value, dict):
        return any(key in identities or _references(item, identities) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_references(item, identities) for item in value)
    return False


def _check_related(tx, user_id):
    devices = {row.object_id for row in tx.list(DEVICES) if row.payload['user_id'] == user_id}
    identities = {user_id, *devices}
    # 未登记的中央关联必须明确处理，不能把遗留数据隐藏成已彻底删除。
    collections = [row[0] for row in tx.connection.execute('SELECT DISTINCT collection FROM crp_structured_records')]
    for collection in collections:
        if collection in _KNOWN:
            continue
        for row in tx.list(collection):
            if row.object_id in identities or _references(row.payload, identities):
                raise UserError('user_delete_shared_owner' if 'shared' in collection else 'user_delete_related_data')
    return devices


class ServerUserDeletion:
    def __init__(self, users, pool, jobs):
        self.users, self.pool, self.jobs = users, pool, jobs

    def _validate(self, tx, actor, user_id, expected_revision, confirmation):
        self.users._admin(tx, actor)
        row = tx.read(USERS, user_id)
        if row is None:
            raise UserError('user_not_found')
        if user_id == actor.user_id or row.payload.get('role') != 'user':
            raise UserError('user_delete_protected')
        if confirmation != user_id:
            raise UserError('user_delete_confirmation_invalid')
        if row.revision != expected_revision:
            raise UserError('user_revision_conflict')
        if row.payload.get('disabled_at') is None:
            raise UserError('user_delete_requires_disabled')
        _check_related(tx, user_id)
        return row

    def _prepare(self, actor, user_id, expected_revision, confirmation):
        if self.jobs.pending_for(user_id):
            raise UserError('user_delete_busy')
        with self.users.records.begin() as tx:
            self._validate(tx, actor, user_id, expected_revision, confirmation)
            marker = tx.read(DELETIONS, user_id)
            if marker is not None:
                if marker.payload['user_revision'] != expected_revision:
                    raise UserError('user_revision_conflict')
                return marker.payload['root_identity']
        # 目录遍历不持有中央数据库的写锁，落标记前再核准入和 CAS。
        target, identity = _target(self.users, user_id)
        if identity is not None:
            _inspect_tree(target)
        with self.users.records.begin() as tx:
            self._validate(tx, actor, user_id, expected_revision, confirmation)
            marker = tx.read(DELETIONS, user_id)
            if marker is None:
                marker = tx.put(DELETIONS, user_id, {'user_id': user_id,
                    'user_revision': expected_revision, 'root_identity': identity}, expected_revision=0)
                tx.commit()
            elif marker.payload['user_revision'] != expected_revision:
                raise UserError('user_revision_conflict')
            return marker.payload['root_identity']

    def _finish(self, actor, user_id, expected_revision, confirmation, intent_id):
        with self.users.records.begin() as tx:
            self._validate(tx, actor, user_id, expected_revision, confirmation)
            marker = tx.read(DELETIONS, user_id)
            if marker is None or marker.payload['user_revision'] != expected_revision:
                raise UserError('user_revision_conflict')
            _, identity = _target(self.users, user_id)
            if identity is not None:
                raise UserError('user_delete_incomplete')
            devices = _check_related(tx, user_id)
            intent_removed = False
            for collection in (PAIRINGS, PRESENCE, 'server_job_usage', 'admin_audit', DEVICES):
                for row in tx.list(collection):
                    value = row.payload
                    selected = (collection == PAIRINGS and (value.get('user_id') == user_id
                                or value.get('issuer') in devices or value.get('device_id') in devices)
                        or collection == PRESENCE and row.object_id in devices
                        or collection in {'server_job_usage', DEVICES} and value.get('user_id') == user_id
                        or collection == 'admin_audit' and user_id in (value.get('target_user_id'), value.get('actor_user_id')))
                    if selected:
                        # 本轮用户明确授权永久删除账号时一并删除其访问历史。
                        tx.delete(collection, row.object_id, expected_revision=row.revision)
                        intent_removed = intent_removed or (collection == 'admin_audit' and row.object_id == intent_id)
            tx.delete(USERS, user_id, expected_revision=expected_revision)
            tx.delete(DELETIONS, user_id, expected_revision=marker.revision)
            tx.commit()
        return intent_removed

    async def delete(self, actor, user_id, *, expected_revision, confirmation, intent_id=None, control_token=None):
        if type(expected_revision) is not int or expected_revision < 1:
            raise UserError('user_revision_invalid')
        try:
            with self.users.records.begin() as tx:
                self.users._admin(tx, actor)
                self.users.get(user_id)
                self._validate(tx, actor, user_id, expected_revision, confirmation)
            async with self.pool.deletion_lease(user_id,
                    lambda: asyncio.to_thread(self._prepare, actor, user_id, expected_revision, confirmation),
                    control_token=control_token) as identity:
                await asyncio.to_thread(remove_user_tree, self.users, user_id, identity)
                removed = self._finish(actor, user_id, expected_revision, confirmation, intent_id)
            return {'user_id': user_id, 'deleted': True}, removed
        except SQLiteUnitOfWorkConflict:
            raise UserError('user_revision_conflict') from None
        except OSError:
            # 标记保留，重启后仍不可接收新工作；同一明确删除请求可继续清理。
            raise UserError('user_delete_incomplete') from None
        except UserError:
            raise
        except Exception as error:
            LOGGER.error('server_user_delete_failed exception_type=%s', type(error).__name__)
            raise UserError('user_delete_incomplete') from None
