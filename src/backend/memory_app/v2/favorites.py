"""Collection-only orchestration helpers; ordinary link intake stays unchanged."""
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit
from .transaction_records import TransactionRecords


def is_favorite_url(value):
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or p.username or p.password or p.fragment or p.port not in (None, 443):
            return False
        if p.hostname == 'space.bilibili.com' and re.fullmatch(r'/[0-9]+/favlist/?', p.path):
            return len(parse_qs(p.query).get('fid', [])) == 1 and parse_qs(p.query)['fid'][0].isdigit()
        return p.hostname in {'bilibili.com', 'www.bilibili.com'} and bool(re.fullmatch(r'/medialist/detail/ml[0-9]+/?', p.path))
    except ValueError:
        return False


def video_url(value):
    p = urlsplit(value)
    if p.scheme != 'https' or p.hostname not in {'bilibili.com', 'www.bilibili.com'} or p.username or p.password or p.port not in (None, 443) or not re.fullmatch(r'/video/BV[A-Za-z0-9]{10}/?', p.path):
        raise ValueError('favorites_video_invalid')
    return urlunsplit(('https', 'www.bilibili.com', p.path.rstrip('/'), '', ''))


def video_identity(value):
    if not isinstance(value, str):
        return None
    try:
        canonical = video_url(value)
        pages = parse_qs(urlsplit(value).query, keep_blank_values=True).get('p', ['1'])
        if len(pages) != 1 or not pages[0].isdigit() or int(pages[0]) < 1:
            return None
        return canonical, int(pages[0])
    except (ValueError, TypeError):
        return None


async def reuse_video(workspace, records, models, project, url):
    canonical = video_url(url)
    # The real writer serializes concurrent collection admission across app instances.
    with records.begin() as tx:
        matches = [row for row in tx.list('workspace_items') if row.payload.get('project_id') == project
                   and video_identity(row.payload.get('media_request_url') or row.payload.get('url')) == (canonical, 1)]
        if matches:
            matches.sort(key=lambda row: (row.payload.get('status') != 'confirmed', row.payload.get('created_at', ''), row.object_id))
            return matches[0].object_id
        borrowed = workspace.items.with_records(TransactionRecords(tx))
        intake = workspace.intake.with_items(borrowed, models)
        item = await intake.add_link({'project_id': project, 'url': canonical})
        tx.commit()
        return item['id']
