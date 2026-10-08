"""Best-effort anonymous comments through the existing public-text boundary.

Reuse yt-dlp's BilibiliBaseIE._get_comments and _get_all_children (Unlicense):
https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py
Verified against the installed 2026.06.09 implementation. Only _download_json
is replaced; no YoutubeDL, extractor initialization, cookies or login run here.
The limits bound capture resources, not model or permission decisions.
"""
import json
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

from yt_dlp.extractor.bilibili import BilibiliBaseIE


_MAX_PAGES = 10
_MAX_CANDIDATES = 2000
_KEEP_COMMENTS = 30
_SECTION = '\n\n## 评论区\n\n'


def _identity(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
        return str(int(value))
    return None


class _BoundaryCommentsIE(BilibiliBaseIE):
    """Keep the upstream traversal while owning every actual HTTP exchange."""
    def __init__(self, network, aid):
        super().__init__()
        self.network, self.aid = network, aid
        self.pages, self.finished, self.likes = 0, False, {}

    def _download_json(self, url, video_id, *, note=None, fatal=True):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, strict_parsing=True)
        if (self.pages >= _MAX_PAGES or video_id != self.aid
                or parsed.scheme != 'https' or parsed.netloc != 'api.bilibili.com'
                or parsed.path != '/x/v2/reply' or parsed.fragment
                or set(query) != {'pn', 'oid', 'type', 'jsonp', 'sort', '_'}
                or query['pn'] != [str(self.pages + 1)] or query['oid'] != [str(self.aid)]
                or query['type'] != ['1'] or query['sort'] != ['2'] or query['jsonp'] != ['jsonp']):
            raise ValueError('bilibili_comments_unavailable')
        self.pages += 1
        payload = json.loads(self.network.fetch_text(url))
        data = payload.get('data') if isinstance(payload, Mapping) else None
        if (not isinstance(payload, Mapping) or type(payload.get('code')) is not int
                or payload['code'] != 0 or not isinstance(data, Mapping) or 'replies' not in data
                or data['replies'] is not None and not isinstance(data['replies'], list)):
            raise ValueError('bilibili_comments_unavailable')
        replies = data['replies'] or []
        self.finished = not replies
        pending = list(reversed(replies))
        while pending:
            reply = pending.pop()
            if not isinstance(reply, Mapping):
                raise ValueError('bilibili_comments_unavailable')
            children = reply.get('replies')
            if children is not None and not isinstance(children, list):
                raise ValueError('bilibili_comments_unavailable')
            identity = _identity(reply.get('rpid'))
            if identity is not None and identity not in self.likes:
                if len(self.likes) >= _MAX_CANDIDATES:
                    raise ValueError('bilibili_comments_unavailable')
                # The upstream projection omits likes. Keep the first actual
                # reply's value, including invalid values, never invent zero.
                self.likes[identity] = reply.get('like')
            pending.extend(reversed(children or []))
        return payload


def capture_bilibili_comments(network, aid):
    """Return the top thirty only after bounded pagination really completes."""
    if type(aid) is not int or aid <= 0:
        return ()
    try:
        extractor = _BoundaryCommentsIE(network, aid)
        seen, comments = set(), []
        for row in extractor._get_comments(aid):
            identity = _identity(row.get('id'))
            if identity is None or identity in seen:
                continue
            seen.add(identity)
            likes, text = extractor.likes[identity], row.get('text')
            if (type(likes) is not int or likes < 0 or not isinstance(text, str)
                    or not text.strip() or '\x00' in text):
                continue
            comments.append({'id': identity, 'text': text.strip(), 'like_count': likes})
        if not extractor.finished:
            return ()
        return tuple(sorted(comments, key=lambda row: -row['like_count'])[:_KEEP_COMMENTS])
    except Exception:
        # Raw provider errors, comments and local paths must not enter logs.
        return ()


def append_bilibili_comments(source, network, aid, *, maximum):
    """Append a whole ranked prefix using only the original's remaining room."""
    return build_bilibili_comment_source(source, network, aid, maximum=maximum)['source_text']


def build_bilibili_comment_source(source, network, aid, *, maximum):
    """Record coordinates while writing the same actual complete entries."""
    if len(source) + len(_SECTION) >= maximum:
        return {'source_text': source, 'sections': None}
    entries, comments = [], []
    remaining = maximum - len(source) - len(_SECTION)
    cursor = len(source) + len(_SECTION)
    for row in capture_bilibili_comments(network, aid):
        text = row['text'].replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\n  ')
        entry = f"- {row['like_count']} 赞 · {text}"
        try:
            entry.encode('utf-8')
        except UnicodeEncodeError:
            # Escaped surrogates make the whole ranked comment section unavailable.
            return {'source_text': source, 'sections': None}
        cost = len(entry) + (2 if entries else 0)
        if cost > remaining:
            break
        start = cursor + (2 if entries else 0) + len(entry) - len(text)
        comments.append({'ordinal': len(comments) + 1, 'rpid': row['id'],
            'like_count': row['like_count'], 'start': start, 'end': start + len(text)})
        entries.append(entry)
        remaining -= cost
        cursor += cost
    combined = source + _SECTION + '\n\n'.join(entries) if entries else source
    sections = {'origin': 'bilibili', 'body': {'start': 0, 'end': len(source)},
        'comment_section': {'start': len(source), 'end': len(combined)}, 'comments': comments} if entries else None
    return {'source_text': combined, 'sections': sections}
