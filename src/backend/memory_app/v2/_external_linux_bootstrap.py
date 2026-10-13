"""仅由私有bootstrap消费固定Linux隔离包，不导入应用或用户代码。"""
import json
import os
import platform
import struct
import subprocess


def seccomp_filter():
    """允许普通进程和Node线程，禁止任务另开namespace及绕过过滤的clone3。"""
    architecture = platform.machine().lower()
    if architecture in {'x86_64', 'amd64'}:
        audit, clone, denied = 0xC000003E, 56, (272, 308, 435, 165, 166, 155, 101, 310, 311)
    elif architecture in {'aarch64', 'arm64'}:
        audit, clone, denied = 0xC00000B7, 220, (97, 268, 435, 40, 39, 41, 117, 270, 271)
    else:
        raise ValueError('external_linux_isolation_unavailable')
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, audit), (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x00050026)]
    for number in denied:
        # clone3返回ENOSYS，让glibc回退到已按flags过滤的clone。
        instructions.extend(((0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | (38 if number == 435 else 1))))
    instructions.extend(((0x15, 0, 3, clone), (0x20, 0, 0, 16),
        (0x45, 0, 1, 0x7E020000), (0x06, 0, 0, 0x00050001), (0x06, 0, 0, 0x7FFF0000)))
    return b''.join(struct.pack('HBBI', *row) for row in instructions)


def launch(body, report):
    spec = body['linux_isolation']
    if set(spec) != {'version', 'mounts', 'links', 'gate', 'python', 'cwd'} or spec['version'] != 'linux-bwrap@1':
        raise ValueError('external_linux_isolation_unavailable')
    info_read, info_write = os.pipe()
    filter_fd = os.memfd_create('chriptmas-seccomp', os.MFD_CLOEXEC)
    try:
        os.write(filter_fd, seccomp_filter())
        os.lseek(filter_fd, 0, os.SEEK_SET)
        command = ['/usr/bin/bwrap', '--unshare-user', '--unshare-pid', '--unshare-ipc', '--unshare-uts',
            '--as-pid-1', '--new-session', '--die-with-parent', '--cap-drop', 'ALL',
            '--info-fd', str(info_write), '--seccomp', str(filter_fd), '--proc', '/proc', '--dev', '/dev',
            '--tmpfs', '/tmp', '--dir', '/etc']
        for source, target in spec['mounts']:
            command.extend(('--ro-bind', source, target))
        for source, target in spec['links']:
            command.extend(('--symlink', source, target))
        command.extend(('--bind', spec['cwd'], spec['cwd'], '--chdir', spec['cwd'],
            '--remount-ro', '/proc', '--remount-ro', '/', '--', spec['python'], '-I', '-S', '-u',
            spec['gate'], *body['command']))
        child = subprocess.Popen(command, cwd=body['cwd'], env=body['environment'], stdin=subprocess.PIPE,
            close_fds=True, pass_fds=(info_write, filter_fd))
    finally:
        os.close(info_write)
        os.close(filter_fd)
    try:
        with os.fdopen(info_read, 'rb', buffering=0) as stream:
            raw = bytearray()
            while len(raw) <= 8192:
                chunk = stream.read(1024)
                if not chunk:
                    break
                raw.extend(chunk)
        data = json.loads(raw)
        identity = data['child-pid']
        if type(identity) is not int or identity <= 1:
            raise ValueError('external_linux_isolation_unavailable')
        report({'namespace_pid': identity, 'launcher_pid': child.pid})
        # 父owner先纳入真实PID1/pidfd，之后才允许原生命令读取冻结stdin。
        if os.read(0, 1) != b'G':
            raise ValueError('external_linux_isolation_unavailable')
        return child
    except Exception:
        child.kill()
        child.wait()
        raise
