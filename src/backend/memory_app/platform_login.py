"""平台签发的短期二维码；只在扫码确认与 CAS 保存后报告已保存。"""
from __future__ import annotations

import base64
from io import BytesIO
import json
import re
from threading import RLock
from time import monotonic
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import uuid4

import qrcode

from backend.security.network_adapter import SafeJsonHttpAdapter, NetworkBoundaryError
from .platform_sessions import PlatformSessionError, parse_cookies


_BASE = 'https://passport.bilibili.com/x/passport-login/web/qrcode/'
_COOKIE_KEYS = {'SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5', 'b_nut'}


class PlatformLogin:
    def __init__(self, sessions, network=None):
        self.sessions = sessions
        self.network = network or SafeJsonHttpAdapter()
        self.pending = {}
        self.lock = RLock()
        self.browser = None

    def capability(self, platform):
        if platform == 'bilibili':
            return {'available': True, 'reason': None}
        try:
            from .platform_browser_login import browser_login_capability
            return browser_login_capability()
        except ImportError:
            return {'available': False, 'reason': 'platform_browser_not_installed'}

    def _browser(self):
        with self.lock:
            if self.browser is None:
                try:
                    from .platform_browser_login import XiaohongshuBrowserLogin
                except ImportError:
                    raise PlatformSessionError('platform_browser_not_installed') from None
                self.browser = XiaohongshuBrowserLogin(self.sessions)
            return self.browser

    def close(self):
        with self.lock:
            self.pending.clear()
            browser, self.browser = self.browser, None
        if browser is not None:
            browser.close()

    def _request(self, suffix):
        try:
            response = self.network.request('GET', _BASE + suffix,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
                body=None, timeout_seconds=15, max_response_bytes=64 * 1024)
            if response.status != 200:
                raise PlatformSessionError('platform_login_unavailable')
            value = json.loads(response.body)
            if not isinstance(value, dict) or value.get('code') != 0 or not isinstance(value.get('data'), dict):
                raise PlatformSessionError('platform_login_unavailable')
            return value['data']
        except (NetworkBoundaryError, ValueError, TypeError, KeyError, RecursionError):
            raise PlatformSessionError('platform_login_unavailable') from None

    def start(self, platform, owner):
        if platform == 'xiaohongshu':
            result = self._browser().start(owner)
            return {**result, 'login_id': 'xhs-' + result['login_id']}
        if platform != 'bilibili':
            raise PlatformSessionError('platform_login_not_integrated')
        revision = self.sessions.status(platform)['revision']
        data = self._request('generate')
        key, url = data.get('qrcode_key'), data.get('url')
        try:
            if not isinstance(url, str) or len(url) > 2048:
                raise ValueError()
            parsed = urlsplit(url)
            if (not isinstance(key, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', key)
                    or parsed.scheme != 'https' or parsed.hostname != 'account.bilibili.com'
                    or parsed.path != '/h5/account-h5/auth/scan-web' or parsed.port not in (None, 443)
                    or parsed.username is not None or parsed.password is not None or parsed.fragment
                    or dict(parse_qsl(parsed.query, max_num_fields=10)).get('qrcode_key') != key):
                raise ValueError()
        except (TypeError, ValueError):
            raise PlatformSessionError('platform_login_unavailable') from None
        output = BytesIO()
        qrcode.make(url, box_size=6, border=4).save(output, format='PNG')
        login_id = uuid4().hex
        with self.lock:
            now = monotonic()
            self.pending = {identity: row for identity, row in self.pending.items()
                if row['expires'] > now and row['owner'] != owner}
            if len(self.pending) >= 16:
                raise PlatformSessionError('platform_login_busy')
            self.pending[login_id] = dict(platform=platform, owner=owner, key=key,
                revision=revision, expires=now + 180, next_poll=now + 2, polls=0)
        return dict(login_id=login_id, platform=platform, state='waiting', expires_in=180,
            poll_after=3, qr_image='data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii'))

    def poll(self, login_id, owner):
        if login_id.startswith('xhs-'):
            return self._browser().poll(login_id[4:], owner)
        with self.lock:
            row = self.pending.get(login_id)
            if row is None or row['owner'] != owner:
                raise PlatformSessionError('platform_login_missing')
            now = monotonic()
            if row['expires'] <= now or row['polls'] >= 60:
                self.pending.pop(login_id)
                return dict(state='expired')
            if row['next_poll'] > now:
                return dict(state='waiting', poll_after=3)
            row['next_poll'], row['polls'] = now + 3, row['polls'] + 1
            data = self._request('poll?' + urlencode({'qrcode_key': row['key']}))
            state = {86101: 'waiting', 86090: 'scanned', 86038: 'expired'}.get(data.get('code'))
            if state is not None:
                if state == 'expired':
                    self.pending.pop(login_id)
                return dict(state=state, poll_after=3)
            if data.get('code') != 0:
                self.pending.pop(login_id)
                raise PlatformSessionError('platform_login_unavailable')
            # 成功 URL 只在内存解析；不请求跨域 URL、不存 refresh_token、不返回 Cookie。
            try:
                success_url = data.get('url')
                if not isinstance(success_url, str) or len(success_url) > 8192:
                    raise ValueError()
                parsed = urlsplit(success_url)
                if (parsed.scheme != 'https' or parsed.hostname not in {'passport.biligame.com', 'passport.bilibili.com'}
                        or parsed.path != '/crossDomain' or parsed.username is not None
                        or parsed.password is not None or parsed.port not in (None, 443)):
                    raise ValueError()
                entries = [(name, quote(value, safe="!#$%&'()*+-./:<=>?@[]^_`{|}~"))
                    for name, value in parse_qsl(parsed.query, max_num_fields=24) if name in _COOKIE_KEYS]
                if len({name for name, _value in entries}) != len(entries) or not {'SESSDATA', 'bili_jct', 'DedeUserID'}.issubset(name for name, _value in entries):
                    raise ValueError()
                cookies = parse_cookies('bilibili', '; '.join(name + '=' + value for name, value in entries))
            except (ValueError, TypeError):
                self.pending.pop(login_id)
                raise PlatformSessionError('platform_login_unavailable') from None
            self.pending.pop(login_id)
            saved = self.sessions.save('bilibili', cookies, row['revision'])
            return dict(state='saved', session=saved)

    def cancel(self, login_id, owner):
        if login_id.startswith('xhs-'):
            return self._browser().cancel(login_id[4:], owner)
        with self.lock:
            row = self.pending.get(login_id)
            if row is not None and row['owner'] == owner:
                self.pending.pop(login_id)
        return dict(state='cancelled')
