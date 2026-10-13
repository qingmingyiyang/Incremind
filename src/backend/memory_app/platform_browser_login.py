"""在隔离浏览器中运行小红书官方扫码流程，不接触用户的浏览器资料。"""
from __future__ import annotations

import base64
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from importlib.util import find_spec
from io import BytesIO
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import sys
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from core.storage_provider import SQLiteUnitOfWorkConflict

from .platform_sessions import PlatformSessionError, parse_cookies


_PLATFORM = 'xiaohongshu'
_ENTRY = 'https://www.xiaohongshu.com/explore'
_TTL = 180
_POLL_AFTER = 3
_CAPACITY = BoundedSemaphore(2)
_API_HOSTS = frozenset({'www.xiaohongshu.com', 'edith.xiaohongshu.com'})
# 2026-10-12 官方首页和登录组件实际引用的主站、风控与静态资源域。
_HOSTS = _API_HOSTS | frozenset({
    'xiaohongshu.com', 'as.xiaohongshu.com', 'so.xiaohongshu.com',
    'picasso-static.xiaohongshu.com', 'pages.xiaohongshu.com', 'fe-static.xhscdn.com',
    'fe-video-qc.xhscdn.com', 'sns-avatar-qc.xhscdn.com',
    'sns-webpic-qc.xhscdn.com', 'sns-video-bd.xhscdn.com',
    'sns-video-hw.xhscdn.com', 'sns-video-hw.xhscdn.net',
    'sns-video-qc.xhscdn.com', 'sns-video-qn.xhscdn.com',
})
_CREATE = '/api/sns/web/v1/login/qrcode/create'
_USERINFO = '/api/qrcode/userinfo'
_STATUS = '/api/sns/web/v1/login/qrcode/status'
_ERROR_CODES = frozenset({
    'platform_login_browser_unavailable', 'platform_login_dependency_missing',
    'platform_login_runtime_unavailable', 'platform_login_unavailable',
    'platform_login_verification_required', 'platform_login_not_found',
    'platform_login_owner_invalid', 'platform_login_busy',
    'platform_login_closed', 'platform_login_timeout',
    'platform_login_save_failed', 'platform_session_revision_conflict',
})


def _browser_executable():
    """只检查安装元数据和可执行文件，不启动 Playwright 驱动或浏览器。"""
    if sys.platform == 'win32':
        for root in (os.environ.get('PROGRAMFILES(X86)'), os.environ.get('PROGRAMFILES')):
            if root:
                edge = Path(root) / 'Microsoft/Edge/Application/msedge.exe'
                if edge.is_file():
                    return str(edge)
    else:
        edge = shutil.which('microsoft-edge') or shutil.which('microsoft-edge-stable')
        if edge:
            return edge
    spec = find_spec('playwright')
    if spec is None or not spec.origin:
        return None
    package = Path(spec.origin).parent / 'driver/package'
    metadata = json.loads((package / 'browsers.json').read_text('utf-8'))
    configured = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    if configured == '0':
        cache = package / '.local-browsers'
    elif configured:
        cache = Path(configured).expanduser()
    elif sys.platform == 'win32':
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            return None
        cache = Path(local) / 'ms-playwright'
    elif sys.platform == 'darwin':
        cache = Path.home() / 'Library/Caches/ms-playwright'
    else:
        cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'ms-playwright'
    layouts = {
        'win32': ('chrome-win64/chrome.exe', 'chrome-win/chrome.exe'),
        'darwin': ('chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
                   'chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
    }
    for browser in metadata['browsers']:
        if browser['name'] != 'chromium':
            continue
        # 安装器的 revisionOverrides 只用于寻找实际存在的受支持安装，不执行下载。
        revisions = {browser['revision'], *browser.get('revisionOverrides', {}).values()}
        for revision in revisions:
            for suffix in layouts.get(sys.platform, ('chrome-linux64/chrome', 'chrome-linux/chrome')):
                executable = cache / ('chromium-' + str(revision)) / suffix
                if executable.is_file():
                    return str(executable)
    return None


def browser_login_capability():
    """设置页可安全重复调用；不会创建进程、访问平台或读取用户登录资料。"""
    try:
        if find_spec('playwright') is None:
            return dict(available=False, reason='platform_login_dependency_missing')
        if not _browser_executable():
            return dict(available=False, reason='platform_login_browser_unavailable')
    except Exception:
        return dict(available=False, reason='platform_login_runtime_unavailable')
    return dict(available=True, reason=None)


def _owner(owner):
    if (not isinstance(owner, tuple) or len(owner) != 2
            or any(not isinstance(part, str) or not part or len(part) > 256 for part in owner)):
        raise PlatformSessionError('platform_login_owner_invalid')
    return owner


def _safe_error(error):
    if isinstance(error, SQLiteUnitOfWorkConflict):
        return 'platform_session_revision_conflict'
    if isinstance(error, PlatformSessionError) and str(error) in _ERROR_CODES:
        return str(error)
    return 'platform_login_unavailable'


@dataclass
class _Attempt:
    owner: tuple[str, str]
    revision: int
    deadline: float
    context: object = None
    page: object = None
    public_requests: object = None
    network_deadline: float = 0
    qr_image: str = ''
    qr_id: str = ''
    qr_code: str = ''
    state: str = 'waiting'
    error: str | None = None
    confirmed: bool = False
    authenticated: bool = False
    slot: bool = True
    session: dict | None = None


class XiaohongshuBrowserLogin:
    """同步 API 的线程边界；所有 Playwright 对象仅由单一工作线程持有。"""

    def __init__(self, sessions):
        self.sessions = sessions
        self._commands = Queue(maxsize=16)
        self._lock = Lock()
        self._stop = Event()
        self._closed = False
        self._thread = None
        self._attempts = {}
        self._playwright = None
        self._browser = None

    def start(self, owner: tuple[str, str]):
        return self._call('start', _owner(owner))

    def poll(self, login_id, owner: tuple[str, str]):
        return self._call('poll', login_id, _owner(owner))

    def cancel(self, login_id, owner: tuple[str, str]):
        return self._call('cancel', login_id, _owner(owner))

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=40)
            if thread.is_alive():
                raise PlatformSessionError('platform_login_timeout')

    def _call(self, command, *args):
        future, abandoned = Future(), Event()
        with self._lock:
            if self._closed:
                raise PlatformSessionError('platform_login_closed')
            if self._thread is None:
                self._thread = Thread(target=self._run, name='xiaohongshu-login', daemon=True)
                self._thread.start()
            try:
                self._commands.put_nowait((command, args, future, abandoned))
            except Full:
                raise PlatformSessionError('platform_login_busy') from None
        try:
            return future.result(timeout=40)
        except TimeoutError:
            abandoned.set()
            future.cancel()
            raise PlatformSessionError('platform_login_timeout') from None
        except Exception as error:
            raise PlatformSessionError(_safe_error(error)) from None

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    command, args, future, abandoned = self._commands.get(timeout=0.1)
                except Empty:
                    self._maintain()
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = getattr(self, '_' + command)(*args)
                    # HTTP 调用方超时后不能留下一个仍可保存登录态的孤立二维码。
                    if abandoned.is_set() and command == 'start':
                        self._cancel(result['login_id'], args[0])
                    future.set_result(result)
                except Exception as error:
                    future.set_exception(PlatformSessionError(_safe_error(error)))
                self._maintain()
        finally:
            for attempt in self._attempts.values():
                self._dispose(attempt)
            self._shutdown_browser()
            while True:
                try:
                    _command, _args, future, _abandoned = self._commands.get_nowait()
                except Empty:
                    break
                if not future.done():
                    future.set_exception(PlatformSessionError('platform_login_closed'))

    def _launch(self):
        if self._browser is not None:
            return
        capability = browser_login_capability()
        if not capability['available']:
            raise PlatformSessionError(capability['reason'])
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                executable_path=_browser_executable(), headless=True, chromium_sandbox=True,
                timeout=15000, handle_sigint=False, handle_sigterm=False, handle_sighup=False,
            )
        except Exception:
            self._shutdown_browser()
            raise PlatformSessionError('platform_login_runtime_unavailable') from None

    def _start(self, owner):
        start_deadline = monotonic() + 30
        if len(self._attempts) >= 32:
            raise PlatformSessionError('platform_login_busy')
        for attempt in self._attempts.values():
            if attempt.owner == owner and attempt.context is not None:
                attempt.state = 'cancelled'
                self._dispose(attempt)
        if not _CAPACITY.acquire(blocking=False):
            raise PlatformSessionError('platform_login_busy')
        attempt = _Attempt(owner, 0, monotonic() + _TTL, network_deadline=start_deadline)
        try:
            attempt.revision = self.sessions.status(_PLATFORM)['revision']
            self._launch()
            attempt.context = self._browser.new_context(
                accept_downloads=False, service_workers='block', viewport={'width': 1280, 'height': 800},
            )
            attempt.public_requests = self._playwright.request.new_context()
            attempt.context.set_default_timeout(3000)
            attempt.context.route('**/*', lambda route: self._route(attempt, route))
            # 登录使用普通 HTTPS 轮询；阻止页面另开 WebSocket 或弹出新页面。
            attempt.context.route_web_socket('**/*', lambda socket: socket.close())
            attempt.context.on('page', lambda page: self._close_page(page) if attempt.page is not None else None)
            attempt.page = attempt.context.new_page()
            attempt.page.on('response', lambda response: self._response(attempt, response))
            attempt.page.on('dialog', lambda dialog: dialog.dismiss())
            attempt.page.goto(_ENTRY, wait_until='domcontentloaded',
                              timeout=max(1, min(10000, (start_deadline - monotonic()) * 1000)))
            deadline = min(attempt.deadline, start_deadline)
            clicked = False
            while not attempt.qr_image and not attempt.error and monotonic() < deadline:
                if self._stop.is_set():
                    raise PlatformSessionError('platform_login_closed')
                if not clicked:
                    clicked = self._click_login(attempt.page)
                if self._verification_visible(attempt.page):
                    attempt.error = 'platform_login_verification_required'
                    break
                attempt.page.wait_for_timeout(100)
            if attempt.error:
                raise PlatformSessionError(attempt.error)
            if not attempt.qr_image:
                raise PlatformSessionError('platform_login_unavailable')
            attempt.deadline = monotonic() + _TTL
            attempt.network_deadline = attempt.deadline
            login_id = uuid4().hex
            self._attempts[login_id] = attempt
            return dict(login_id=login_id, platform=_PLATFORM, state='waiting',
                        expires_in=_TTL, poll_after=_POLL_AFTER, qr_image=attempt.qr_image)
        except Exception:
            self._dispose(attempt)
            if not any(item.context is not None for item in self._attempts.values()):
                self._shutdown_browser()
            raise

    def _get(self, login_id, owner):
        attempt = self._attempts.get(login_id) if isinstance(login_id, str) else None
        if attempt is None or attempt.owner != owner:
            raise PlatformSessionError('platform_login_not_found')
        return attempt

    def _poll(self, login_id, owner):
        attempt = self._get(login_id, owner)
        if attempt.state == 'cancelled':
            raise PlatformSessionError('platform_login_not_found')
        self._refresh(attempt)
        if attempt.error:
            raise PlatformSessionError(attempt.error)
        if attempt.confirmed and attempt.authenticated and attempt.context is not None:
            # 只有平台确认 + 官方 status 成功 + 真正会话 Cookie 三者齐备才能保存。
            try:
                raw = [cookie for cookie in attempt.context.cookies()
                       if cookie['domain'] in {'.xiaohongshu.com', 'xiaohongshu.com', 'www.xiaohongshu.com'}]
                if any(cookie['name'] == 'web_session' and cookie['value'] for cookie in raw):
                    cookies = parse_cookies(_PLATFORM, json.dumps(raw))
                    attempt.session = self.sessions.save(_PLATFORM, cookies, expected_revision=attempt.revision)
                    attempt.state = 'saved'
            except SQLiteUnitOfWorkConflict:
                attempt.error = 'platform_session_revision_conflict'
            except Exception:
                attempt.error = 'platform_login_save_failed'
            if attempt.state == 'saved' or attempt.error:
                self._dispose(attempt)
                if attempt.error:
                    raise PlatformSessionError(attempt.error)
        if attempt.state == 'saved':
            return dict(state='saved', session=attempt.session)
        return dict(state=attempt.state, **({'poll_after': _POLL_AFTER}
                    if attempt.state in {'waiting', 'scanned'} else {}))

    def _cancel(self, login_id, owner):
        attempt = self._get(login_id, owner)
        if attempt.state != 'saved':
            attempt.state = 'cancelled'
        self._dispose(attempt)
        return dict(state='cancelled')

    @staticmethod
    def _click_login(page):
        # 只点击当前真实 DOM 中可见的登录入口，不依赖构建版本里的固定 ID。
        for locator in (page.get_by_role('button', name='登录', exact=True),
                        page.get_by_text('登录', exact=True)):
            for index in range(min(locator.count(), 8)):
                button = locator.nth(index)
                if button.is_visible():
                    button.click(timeout=3000)
                    return True
        return False

    @staticmethod
    def _close_page(page):
        try:
            page.close()
        except Exception:
            # 平台弹出页不在本次授权导航范围内，关闭时不暴露页面信息。
            pass

    def _route(self, attempt, route):
        request = route.request
        response = None
        try:
            parsed = urlsplit(request.url)
            allowed = (parsed.scheme == 'https' and parsed.hostname in _HOSTS
                       and parsed.port in {None, 443} and not parsed.username and not parsed.password)
            if request.is_navigation_request():
                allowed = allowed and parsed.hostname == 'www.xiaohongshu.com' and parsed.path == '/explore'
            # 首页的视频和图片与扫码无关；不下载内容，不打开用户指定链接。
            allowed = allowed and request.resource_type not in {'media', 'image', 'font'}
            remaining = min(5000, (attempt.network_deadline - monotonic()) * 1000)
            if not allowed or self._stop.is_set() or remaining <= 0:
                route.abort()
                return
            if parsed.hostname in _API_HOSTS:
                # 保留官方页面生成的签名、请求体和自己的 Cookie；绝不自动跟随重定向。
                response = route.fetch(max_redirects=0, max_retries=0, timeout=remaining)
            else:
                # continue_ 无法覆盖浏览器的 Cookie 头；资源使用完全独立的请求 Cookie jar。
                headers = {key: value for key, value in request.all_headers().items()
                           if key.lower() not in {'cookie', 'authorization', 'proxy-authorization'}}
                headers['Cookie'] = ''
                response = attempt.public_requests.fetch(
                    request.url, method=request.method, headers=headers, data=request.post_data_buffer,
                    max_redirects=0, max_retries=0, timeout=remaining,
                )
            if 300 <= response.status < 400:
                if request.is_navigation_request() or parsed.path in {_CREATE, _USERINFO, _STATUS}:
                    attempt.error = 'platform_login_unavailable'
                route.abort()
                return
            headers = response.headers
            if parsed.hostname not in _API_HOSTS:
                headers = {key: value for key, value in headers.items() if key.lower() != 'set-cookie'}
            route.fulfill(response=response, headers=headers)
        except Exception:
            # 关闭上下文会中断尚未完成的路由；不输出可能含凭据的 Playwright 异常。
            try:
                route.abort()
            except Exception:
                pass
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    pass

    @staticmethod
    def _verification_visible(page):
        return page.locator('#jarvis, #jarvis-container, .captcha-verify, .geetest_panel').first.is_visible()

    @staticmethod
    def _matches(attempt, request):
        parsed = urlsplit(request.url)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
        if request.method == 'POST':
            data = request.post_data_json
            if isinstance(data, dict):
                params.update(data)
        return params.get('qrId') == attempt.qr_id and params.get('code') == attempt.qr_code

    def _response(self, attempt, response):
        if attempt.error or attempt.state not in {'waiting', 'scanned'}:
            return
        try:
            parsed = urlsplit(response.url)
            if parsed.hostname not in _API_HOSTS or parsed.path not in {_CREATE, _USERINFO, _STATUS}:
                return
            payload = response.json()
            if not isinstance(payload, dict):
                attempt.error = 'platform_login_unavailable'
                return
            if response.status in {461, 471} or payload.get('code') in {461, 471, -13002, -13020}:
                attempt.error = 'platform_login_verification_required'
                return
            if response.status < 200 or response.status >= 300:
                attempt.error = 'platform_login_unavailable'
                return
            data = payload.get('data', payload)
            if not isinstance(data, dict):
                attempt.error = 'platform_login_unavailable'
                return
            if parsed.path == _CREATE:
                if payload.get('success') is not True or payload.get('code') != 0:
                    attempt.error = 'platform_login_unavailable'
                    return
                if attempt.qr_id:
                    # 页面自动刷新不能把返回给手机的二维码换成另一个。
                    attempt.state = 'expired'
                    return
                url, qr_id, code = data.get('url'), data.get('qrId'), data.get('code')
                if not all(isinstance(value, str) and 0 < len(value) <= 4096 for value in (url, qr_id, code)):
                    attempt.error = 'platform_login_unavailable'
                    return
                import qrcode
                stream = BytesIO()
                qrcode.make(url).save(stream, format='PNG')
                attempt.qr_id, attempt.qr_code = qr_id, code
                attempt.qr_image = 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')
            elif attempt.qr_id and self._matches(attempt, response.request):
                if parsed.path == _USERINFO:
                    status = data.get('codeStatus')
                    if type(status) is not int or status not in {0, 1, 2, 3}:
                        attempt.error = 'platform_login_unavailable'
                    elif status == 3:
                        attempt.state = 'expired'
                    elif status in {1, 2}:
                        attempt.state = 'scanned'
                        attempt.confirmed = status == 2
                elif parsed.path == _STATUS:
                    if payload.get('success') is True and payload.get('code') == 0:
                        attempt.authenticated = True
                    else:
                        attempt.error = 'platform_login_unavailable'
        except Exception:
            attempt.error = 'platform_login_unavailable'

    def _refresh(self, attempt):
        if attempt.context is None:
            return
        if monotonic() >= attempt.deadline or attempt.state == 'expired':
            attempt.state = 'expired'
            self._dispose(attempt)
        elif attempt.error:
            self._dispose(attempt)
        elif self._verification_visible(attempt.page):
            attempt.error = 'platform_login_verification_required'
            self._dispose(attempt)

    def _maintain(self):
        now = monotonic()
        for login_id, attempt in list(self._attempts.items()):
            try:
                if attempt.page is not None:
                    # Sync Playwright 必须在所属线程内让出控制，平台自己的轮询才继续运行。
                    attempt.page.wait_for_timeout(25)
                self._refresh(attempt)
            except Exception:
                attempt.error = 'platform_login_unavailable'
                self._dispose(attempt)
            if now >= attempt.deadline + 60:
                del self._attempts[login_id]
        if self._browser is not None and not any(item.context is not None for item in self._attempts.values()):
            self._shutdown_browser()

    @staticmethod
    def _dispose(attempt):
        context, attempt.context = attempt.context, None
        public_requests, attempt.public_requests = attempt.public_requests, None
        attempt.page = None
        attempt.qr_image = attempt.qr_id = attempt.qr_code = ''
        for resource, method in ((context, 'close'), (public_requests, 'dispose')):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    # 关闭或崩溃后的上下文无需重试；浏览器仍由最终清理负责。
                    pass
        if attempt.slot:
            attempt.slot = False
            _CAPACITY.release()

    def _shutdown_browser(self):
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        for resource, method in ((browser, 'close'), (playwright, 'stop')):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    # 驱动或浏览器已经退出时清理保持幂等，不记录原始错误。
                    pass
