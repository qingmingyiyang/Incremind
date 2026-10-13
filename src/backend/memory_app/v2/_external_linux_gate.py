"""隔离PID1先等待原owner核验，开闸后仅exec宿主冻结的原生命令。"""
import os
import sys


if __name__ == '__main__':
    try:
        if os.read(0, 1) != b'G' or len(sys.argv) < 2 or not os.path.isabs(sys.argv[1]):
            os._exit(125)
        os.execve(sys.argv[1], sys.argv[1:], dict(os.environ))
    except Exception:
        os._exit(125)
