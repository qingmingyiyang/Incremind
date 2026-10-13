"""本机管理员网页登录入口，配对凭据仅保留在内存中。"""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import ssl
import subprocess
import sys
import threading
import urllib.request
import webbrowser


CONNECTION_DIR = Path(__file__).resolve().parent
STATUS_LOCK = threading.Lock()
STATUS_EVENTS = []


def record_status(event, error=None):
    # 仅保留固定阶段和异常类型；写入失败不能覆盖原有业务异常。
    entry = {"event": event, "utc": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}
    if error is not None:
        entry["exception_class"] = type(error).__name__
    with STATUS_LOCK:
        STATUS_EVENTS.append(entry)
        del STATUS_EVENTS[:-32]
        try:
            (CONNECTION_DIR / "login-status.json").write_text(
                json.dumps(STATUS_EVENTS, ensure_ascii=True), encoding="utf-8"
            )
        except OSError:
            return False
    return True


def server_origin():
    config = json.loads((CONNECTION_DIR / "connection.json").read_text(encoding="utf-8"))
    return "https://" + str(ipaddress.IPv4Address(config["host"]))


def check_health(origin):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    with opener.open(origin + "/api/health", timeout=12) as response:
        if response.status != 200 or json.loads(response.read(4096)) != {"status": "ok"}:
            raise ValueError("health")


def pairing_url(origin):
    # pythonw 没有控制台标准流，SSH 助手必须由同目录 python.exe 执行。
    python = Path(sys.executable).with_name("python.exe")
    command = (
        "runuser -u chriptmas -- /opt/chriptmas/.venv/bin/python "
        "/opt/chriptmas/tools/server_admin.py pair --root /var/lib/chriptmas --url " + origin
    )
    result = subprocess.run(
        [str(python), "-I", str(CONNECTION_DIR / "ssh_run.py"), "--command", command, "--timeout", "20"],
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError("pairing")
    url = json.loads(result.stdout)["url"]
    if not isinstance(url, str) or not re.fullmatch(
        re.escape(origin) + r"/pair#code=[A-Za-z0-9_-]{43}", url
    ):
        raise ValueError("pairing destination")
    return url


def show_window(initial_action=None):
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title("Incremind 登录")
    window.minsize(440, 235)
    panel = ttk.Frame(window, padding=24)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="Incremind 服务器", font=("Microsoft YaHei UI", 15)).pack(anchor="w")
    ttk.Label(panel, text="已配对的浏览器直接打开网页；新浏览器选择重新登录。", wraplength=410).pack(anchor="w", pady=(12, 18))
    actions = ttk.Frame(panel)
    actions.pack(anchor="w")
    status = tk.StringVar(value="请选择操作。")
    ttk.Label(panel, textvariable=status, wraplength=410).pack(anchor="w", pady=(18, 0))
    results = queue.Queue()
    busy = False

    def worker(pair):
        # 后台线程仅联网；Tk 控件与浏览器调用由主线程处理。
        try:
            origin = server_origin()
            check_health(origin)
            record_status("https_healthy")
            url = pairing_url(origin) if pair else origin + "/"
            if pair:
                record_status("pairing_link_received")
            results.put((True, pair, url))
        except Exception as error:
            # 不展示子进程输出和异常内容，避免凭据进入窗口或日志。
            record_status("connection_error", error)
            results.put((False, pair, None))

    def start(pair):
        nonlocal busy
        if busy:
            return
        record_status("pairing_started" if pair else "open_started")
        busy = True
        for button in buttons:
            button.state(["disabled"])
        status.set("正在生成新的登录入口…" if pair else "正在连接服务器…")
        threading.Thread(target=worker, args=(pair,), daemon=True).start()

    buttons = [
        ttk.Button(actions, text="打开网页", command=lambda: start(False)),
        ttk.Button(actions, text="重新登录", command=lambda: start(True)),
    ]
    for button in buttons:
        button.pack(side="left", padx=(0, 12))

    def poll():
        nonlocal busy
        try:
            success, pair, url = results.get_nowait()
        except queue.Empty:
            pass
        else:
            busy = False
            for button in buttons:
                button.state(["!disabled"])
            if success:
                try:
                    opened = webbrowser.open(url)
                    record_status("browser_open_true" if opened else "browser_open_false")
                except Exception as error:
                    record_status("browser_error", error)
                    opened = False
                status.set(
                    ("已打开登录页，请在 10 分钟内完成配对。" if pair else "已打开网页。")
                    if opened else "浏览器未能打开，请重试并检查默认浏览器。"
                )
            else:
                status.set("连接失败。请检查网络与本机服务器连接文件，然后重试。")
        window.after(100, poll)

    def callback_error(_exception_type, error, _traceback):
        record_status("window_callback_error", error)
        status.set("操作失败，请关闭窗口后重试。")

    window.report_callback_exception = callback_error
    record_status("window_ready")
    window.after(100, poll)
    if initial_action:
        window.after(150, lambda: start(initial_action == "pair"))
    window.mainloop()


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--pair", action="store_true")
    actions.add_argument("--open", action="store_true")
    actions.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        try:
            origin = server_origin()
            check_health(origin)
        except Exception:
            print(json.dumps({"healthy": False}))
            return 1
        print(json.dumps({"healthy": True, "origin": origin, "uses_tunnel": False}))
        return 0
    record_status("program_started")
    try:
        show_window("pair" if args.pair else "open" if args.open else None)
    except Exception as error:
        record_status("window_error", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
