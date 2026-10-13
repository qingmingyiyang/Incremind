"""平台登录态领域边界：秘密进入原 SecretStore，SQLite 只保存修订与状态。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from time import time
from urllib.parse import urlsplit
from uuid import uuid4

from backend.security.network_adapter import SafeTextNetworkAdapter
from core.storage_provider import SQLiteUnitOfWorkConflict


COLLECTION = 'v2_platform_sessions'
PLATFORMS = {
    'bilibili': ('bilibili.com', ('www.bilibili.com', 'api.bilibili.com')),
    'xiaohongshu': ('xiaohongshu.com', ('xiaohongshu.com', 'www.xiaohongshu.com')),
}
MAX_COOKIE_BYTES = 64 * 1024
_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_VALUE = re.compile(r'[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]*\Z')


class PlatformSessionError(ValueError):
    """只含固定安全错误码，禁止携带原始输入。"""


def platform_config(platform):
    if platform not in PLATFORMS:
        raise PlatformSessionError('platform_unsupported')
    return PLATFORMS[platform]


def parse_cookies(platform, content, *, now=None):
    domain, _hosts = platform_config(platform)
    now = time() if now is None else now
    if not isinstance(content, str) or not content.strip():
        raise PlatformSessionError('platform_cookie_size_invalid')
    try:
        if len(content.encode('utf-8')) > MAX_COOKIE_BYTES:
            raise PlatformSessionError('platform_cookie_size_invalid')
    except UnicodeError:
        raise PlatformSessionError('platform_cookie_invalid') from None
    content = content.strip().lstrip('\ufeff')
    if content.startswith(('[', '{')):
        try:
            raw = json.loads(content)
        except (ValueError, TypeError, RecursionError):
            raise PlatformSessionError('platform_cookie_invalid') from None
        raw = raw.get('cookies') if isinstance(raw, dict) else raw
        if not isinstance(raw, list):
            raise PlatformSessionError('platform_cookie_invalid')
    elif '\t' in content or content.startswith('#'):
        raw = []
        for line in content.splitlines():
            if line.startswith('#HttpOnly_'):
                line = line[len('#HttpOnly_'):]
            elif not line.strip() or line.startswith('#'):
                continue
            fields = line.split('\t')
            if len(fields) != 7 or fields[1] not in {'TRUE', 'FALSE'} or fields[3] not in {'TRUE', 'FALSE'}:
                raise PlatformSessionError('platform_cookie_invalid')
            raw.append(dict(domain=fields[0], hostOnly=fields[1] == 'FALSE', path=fields[2],
                expirationDate=fields[4], name=fields[5], value=fields[6]))
    else:
        if any(char in content for char in '\r\n\x00'):
            raise PlatformSessionError('platform_cookie_invalid')
        if content.lower().startswith('cookie:'):
            content = content[7:].strip()
        raw = []
        for part in content.split(';'):
            name, separator, value = part.strip().partition('=')
            if not separator:
                raise PlatformSessionError('platform_cookie_invalid')
            raw.append(dict(name=name, value=value, domain='.' + domain, path='/'))
    if not 1 <= len(raw) <= 200:
        raise PlatformSessionError('platform_cookie_invalid')
    cookies, identities = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise PlatformSessionError('platform_cookie_invalid')
        name, value = item.get('name'), item.get('value')
        if (not isinstance(name, str) or not _NAME.fullmatch(name) or len(name) > 128
                or not isinstance(value, str) or not value or not _VALUE.fullmatch(value) or len(value) > 8192):
            raise PlatformSessionError('platform_cookie_invalid')
        cookie_domain = item.get('domain')
        if not isinstance(cookie_domain, str):
            raise PlatformSessionError('platform_cookie_domain_invalid')
        cookie_domain = cookie_domain.lower()
        bare = cookie_domain.lstrip('.')
        if bare not in {domain, *PLATFORMS[platform][1]} or cookie_domain not in {bare, '.' + bare}:
            raise PlatformSessionError('platform_cookie_domain_invalid')
        path = item.get('path', '/')
        if 'hostOnly' in item and type(item['hostOnly']) is not bool:
            raise PlatformSessionError('platform_cookie_invalid')
        if not isinstance(path, str) or not path.startswith('/') or len(path) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in path):
            raise PlatformSessionError('platform_cookie_invalid')
        expiry = item.get('expirationDate', item.get('expires', 0))
        try:
            expiry = float(expiry)
        except (TypeError, ValueError):
            raise PlatformSessionError('platform_cookie_expiry_invalid') from None
        if isinstance(item.get('expirationDate', item.get('expires', 0)), bool) or not math.isfinite(expiry) or expiry < -1 or expiry > 253402300799:
            raise PlatformSessionError('platform_cookie_expiry_invalid')
        expiry = expiry if expiry > 0 else None
        if expiry is not None and expiry <= now:
            continue
        identity = (name, bare, path)
        if identity in identities:
            raise PlatformSessionError('platform_cookie_invalid')
        identities.add(identity)
        cookies.append(dict(name=name, value=value, domain=bare, path=path, expires=expiry,
            host_only=item.get('hostOnly', not cookie_domain.startswith('.')) is True))
    if not cookies:
        raise PlatformSessionError('platform_cookie_expired')
    return cookies


def _assert_not_reflected(text, cookies, header):
    # 页面、字幕和评论都会进入原件；完整 Cookie 或单个秘密值反射时直接拒绝。
    from urllib.parse import quote
    forbidden = [header]
    for cookie in cookies:
        value = cookie['value']
        forbidden.append(cookie['name'] + '=' + value)
        # DedeUserID 是页面公开 UP 主/评论用户 ID；仍拦截 name=value 形式。
        if len(value) >= 8 and cookie['name'] != 'DedeUserID':
            forbidden.extend((value, quote(value, safe=''), json.dumps(value)[1:-1]))
    if any(value and value in text for value in forbidden):
        raise PlatformSessionError('platform_credential_reflected')


class PlatformSessions:
    def __init__(self, records, secrets):
        self.records, self.secrets = records, secrets

    def status(self, platform):
        platform_config(platform)
        row = self.records.read(COLLECTION, platform)
        payload = row.payload if row else {}
        cookies = self._cookies(row)
        return dict(platform=platform, revision=row.revision if row else 0,
            saved=bool(cookies), saved_at=payload.get('saved_at'),
            expires_at=min((c['expires'] for c in cookies if c['expires']), default=None),
            authentication='not_checked', cookie_count=len(cookies),
            qr_available=platform == 'bilibili',
            qr_unavailable_reason=None if platform == 'bilibili' else 'platform_login_not_integrated')

    def _cookies(self, row):
        if row is None or not row.payload.get('secret_ref'):
            return []
        secret = self.secrets.get_snapshot(row.payload['secret_ref']).value
        if not secret:
            return []
        try:
            cookies = json.loads(secret)
        except (ValueError, TypeError):
            raise PlatformSessionError('platform_session_unavailable') from None
        return [cookie for cookie in cookies if not cookie['expires'] or cookie['expires'] > time()]

    def replace(self, platform, content, expected_revision):
        cookies = parse_cookies(platform, content)
        return self.save(platform, cookies, expected_revision)

    def save(self, platform, cookies, expected_revision):
        platform_config(platform)
        if type(expected_revision) is not int or expected_revision < 0:
            raise PlatformSessionError('platform_revision_invalid')
        secret_ref = 'platform-session-' + uuid4().hex
        previous = None
        try:
            with self.records.begin() as tx:
                row = tx.read(COLLECTION, platform)
                if (row.revision if row else 0) != expected_revision:
                    raise SQLiteUnitOfWorkConflict('platform_session_revision_conflict')
                previous = row.payload.get('secret_ref') if row else None
                self.secrets.set(secret_ref, json.dumps(cookies, ensure_ascii=True))
                tx.put(COLLECTION, platform, dict(secret_ref=secret_ref,
                    saved_at=datetime.now(timezone.utc).isoformat()), expected_revision=expected_revision)
                tx.commit()
        except Exception:
            self.secrets.delete(secret_ref)
            raise
        if previous:
            self.secrets.delete(previous)
        return self.status(platform)

    def delete(self, platform, expected_revision):
        platform_config(platform)
        if type(expected_revision) is not int or expected_revision < 0:
            raise PlatformSessionError('platform_revision_invalid')
        with self.records.begin() as tx:
            row = tx.read(COLLECTION, platform)
            previous = row.payload.get('secret_ref') if row else None
            tx.put(COLLECTION, platform, dict(secret_ref=None, saved_at=None), expected_revision=expected_revision)
            tx.commit()
        if previous:
            self.secrets.delete(previous)
        return self.status(platform)

    def request_cookies(self, platform, url):
        _domain, hosts = platform_config(platform)
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            if (parsed.scheme != 'https' or host not in hosts or parsed.port not in (None, 443)
                    or parsed.username is not None or parsed.password is not None):
                return []
        except ValueError:
            return []
        path = parsed.path or '/'
        cookies = self._cookies(self.records.read(COLLECTION, platform))
        return sorted((c for c in cookies if (host == c['domain'] or not c['host_only'] and host.endswith('.' + c['domain']))
            and (path == c['path'] or path.startswith(c['path'] if c['path'].endswith('/') else c['path'] + '/'))),
            key=lambda c: -len(c['path']))

    def network(self, platform, public_network):
        return _SessionTextNetwork(self, platform, public_network)


class _SessionTextNetwork:
    def __init__(self, sessions, platform, public_network):
        self.sessions, self.platform, self.public = sessions, platform, public_network
        self.used = []

    def fetch_text(self, url):
        cookies = self.sessions.request_cookies(self.platform, url)
        if not cookies:
            text = self.public.fetch_text(url)
        else:
            def request_headers():
                # DNS 校验后、实际构造请求前重新读取，避免等待期间使用已替换的值。
                fresh = self.sessions.request_cookies(self.platform, url)
                self.used.extend(fresh)
                header = '; '.join(c['name'] + '=' + c['value'] for c in fresh)
                return {'Cookie': header} if header else {}
            # 固定主站/API、禁重定向；字幕 CDN 和短链使用无凭据公开适配器。
            text = SafeTextNetworkAdapter(allowed_hosts=PLATFORMS[self.platform][1], max_redirects=0,
                max_response_bytes=4 * 1024 * 1024, timeout_seconds=20,
                _request_headers=request_headers).fetch_text(url)
        self.assert_safe(text)
        return text

    def assert_safe(self, text):
        _assert_not_reflected(text, self.used, '; '.join(c['name'] + '=' + c['value'] for c in self.used))
