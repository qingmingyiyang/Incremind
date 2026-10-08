"""等父进程绑定生命周期 owner 后，才从标准输入接收启动包。"""
import json
import os
import subprocess
import sys


def main():
    control = int(sys.argv[1])
    if os.name == 'nt':
        import msvcrt
        control = msvcrt.open_osfhandle(control, os.O_WRONLY | os.O_BINARY)
    os.set_inheritable(control, False)
    with os.fdopen(control, 'wb', buffering=0) as output:
        def report(body):
            output.write((json.dumps(body) + '\n').encode('ascii'))
        try:
            # EOF 是父进程成功绑定后的启动闸；此前不会创建实际命令。
            body = json.loads(sys.stdin.buffer.read().decode('utf-8'))
            child = subprocess.Popen(body['command'], cwd=body['cwd'], env=body['environment'],
                stdin=subprocess.PIPE, close_fds=True)
        except Exception:
            report({'error': 'external_process_start_failed'})
            return 125
        report({'started': True})
        try:
            child.communicate(input=body['input_text'].encode('utf-8'))
            report({'exit_code': child.returncode})
            return 0
        except Exception:
            report({'error': 'external_process_failed'})
            return 125


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        # 不向继承的标准错误泄漏启动参数、环境或异常内容。
        sys.exit(125)
