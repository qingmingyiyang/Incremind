import os
from pathlib import Path
import sys
from uuid import UUID

from .external_workspace import task_path_identity


def _physical_identity(path, *, directory=False):
    identity = task_path_identity(path, directory=directory)[:2]
    if (any(type(value) is not int for value in identity)
            or identity[0] < 0 or identity[1] <= 0):
        raise ValueError('server_model_process_origin_unavailable')
    return identity


def model_process_origin(users, user_id):
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, kind = winreg.QueryValueEx(key, 'MachineGuid')
        if kind != winreg.REG_SZ:
            raise ValueError('server_model_process_origin_unavailable')
        namespace = 'windows'
    elif sys.platform.startswith('linux'):
        value = Path('/etc/machine-id').read_text(encoding='ascii').strip()
        namespace = 'linux'
    else:
        raise ValueError('server_model_process_origin_unavailable')
    parsed = UUID(value)
    if parsed.int == 0:
        raise ValueError('server_model_process_origin_unavailable')
    identity = namespace + ':' + str(parsed)
    # 目录和数据库的实际身份使复制件保留原预留，原主机的进程证明不能借给复制件。
    return {'os_identity': identity,
        'server_root_identity': _physical_identity(users.server_root, directory=True),
        'user_root_identity': _physical_identity(users.root_for(user_id), directory=True),
        'database_identity': _physical_identity(users.records.database_path)}
