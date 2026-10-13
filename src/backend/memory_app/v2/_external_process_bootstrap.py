"""等父进程绑定生命周期 owner 后，才从标准输入接收启动包。"""
import json
import os
import subprocess
import sys
import threading


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
            # 完整启动行是父进程成功绑定后的启动闸；后续 stdin 不再解析成启动命令。
            controlled = len(sys.argv) == 3 and sys.argv[2] in {'interactive', 'isolated'}
            interactive = len(sys.argv) == 3 and sys.argv[2] == 'interactive'
            raw = (sys.stdin.buffer.readline(32 * 1024 * 1024 + 1)
                if controlled else sys.stdin.buffer.read())
            if controlled and (not raw.endswith(b'\n') or len(raw) > 32 * 1024 * 1024):
                raise ValueError('external_process_start_failed')
            body = json.loads(raw.decode('utf-8'))
            if 'linux_isolation' in body:
                import importlib.util
                from pathlib import Path
                source = Path(__file__).with_name('_external_linux_bootstrap.py')
                specification = importlib.util.spec_from_file_location('_chriptmas_linux_bootstrap', source)
                module = importlib.util.module_from_spec(specification)
                specification.loader.exec_module(module)
                child = module.launch(body, report)
                prefix = b'G'
            else:
                child = subprocess.Popen(body['command'], cwd=body['cwd'], env=body['environment'],
                    stdin=subprocess.PIPE, close_fds=True)
                prefix = b''
        except Exception:
            report({'error': 'external_process_start_failed'})
            return 125
        report({'started': True})
        try:
            if interactive:
                child.stdin.write(prefix + body['input_text'].encode('utf-8'))
                child.stdin.flush()
                failed = []
                def forward():
                    try:
                        while True:
                            chunk = sys.stdin.buffer.read1(65536)
                            if not chunk:
                                break
                            child.stdin.write(chunk)
                            child.stdin.flush()
                    except (OSError, ValueError):
                        if child.poll() is None:
                            failed.append(True)
                            child.terminate()
                    finally:
                        try:
                            child.stdin.close()
                        except (OSError, ValueError):
                            pass
                # 子进程退出不依赖父stdin EOF；阻塞读取线程由bootstrap退出收口。
                threading.Thread(target=forward, daemon=True).start()
                child.wait()
                if failed:
                    report({'error': 'external_process_failed'})
                    os._exit(125)
            else:
                child.communicate(input=prefix + body['input_text'].encode('utf-8'))
            report({'exit_code': child.returncode})
            if interactive:
                # 控制管道无缓冲且已报告实际退出；避免CPython等待daemon持有的stdin锁。
                os._exit(0)
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
