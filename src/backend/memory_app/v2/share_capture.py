"""手机分享接入，复用工作台原件、回执和幂等执行。"""
import asyncio
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from core.storage_provider.connection_scope import with_connection_scope
from ..workspace_contracts import _now, _project
from ..workspace_media_url import media_platform
from .turn_execution import _REQUEST_KEY, REQUESTS
from .intent import parse_scope_tag

CAPTURES = 'v2_share_captures'
IMAGES = {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}
FILES = IMAGES | {'.txt', '.md', '.pdf', '.docx', '.mp3', '.wav', '.m4a', '.ogg', '.flac', '.webm', '.mp4', '.mov', '.mkv', '.avi', '.m4v'}


def share_url(text, *, shared=False):
    """保留签名查询参数，清理分享文案与句末标点。"""
    for match in re.finditer(r'https?://[^\s<>\x00-\x1f\u3000-\uffff]+', text):
        candidate = match.group().rstrip(".,;!?)]}\"'")
        try:
            parsed = urlsplit(candidate)
            if parsed.username or parsed.password or not parsed.hostname:
                continue
        except ValueError:
            continue
        if shared or match.start() == 0 or media_platform(candidate):
            return candidate
    return None


def install_share_capture(router, *, records, workspace, executions, instance, project_for_tag):
    locks = {}

    def save(key, changes):
        with records.begin() as tx:
            row = tx.read(CAPTURES, key)
            tx.put(CAPTURES, key, {**row.payload, **changes, 'updated_at': _now()}, expected_revision=row.revision)
            tx.commit()

    @router.post('/share')
    @with_connection_scope
    async def receive(request: Request):
        key = request.headers.get('idempotency-key', '')
        if not _REQUEST_KEY.fullmatch(key) or len(key) > 80:
            raise HTTPException(400, 'invalid_idempotency_key')
        async with request.form(max_files=16, max_fields=8) as form:
            if set(form).difference({'project_id', 'title', 'text', 'url', 'files'}):
                raise HTTPException(400, 'invalid_share_fields')
            project = _project(form.get('project_id', 'default'))
            values = [form.get(name, '') for name in ('title', 'text', 'url')]
            if any(not isinstance(value, str) for value in values):
                raise HTTPException(400, 'invalid_text')
            text = '\n'.join(value.strip() for value in values if value.strip())
            if len(text) > 60000:
                raise HTTPException(400, 'invalid_text')
            # Cookie 与设备钥匙只走设置入口，分享不能把凭据保存成原件。
            if re.search(r'(?i)(?:authorization\s*:|bearer\s+[\w.-]{16,}|(?:^|\n)\s*(?:cookie|set-cookie)\s*:|# Netscape HTTP Cookie File)', text):
                raise HTTPException(400, 'share_credentials_rejected')
            for match in re.finditer(r'https?://[^\s<>]+', text):
                try:
                    target = urlsplit(match.group())
                    credentials = target.username or target.password or any(name.lower() in
                        {'authorization', 'cookie', 'api_key', 'access_token', 'refresh_token', 'device_key'}
                        for name, _ in parse_qsl(target.query))
                except ValueError:
                    credentials = True
                if credentials:
                    raise HTTPException(400, 'share_credentials_rejected')
            url = share_url(text, shared=True)
            tag, scene, _ = parse_scope_tag(text)
            if tag:
                project = project_for_tag(tag)
            text = (('#' + tag + ('/' + scene if scene else '') + ' ' if tag else '') + url) if url else text
            files = form.getlist('files')
            for file in files:
                if (not isinstance(file, UploadFile) or Path(file.filename or '').suffix.lower() not in FILES
                        or re.fullmatch(r'(?i)(cookies?(?:[-_.]export)?\.(?:txt|json)|credentials?\.json|authorization\.txt|\.env(?:\..*)?)', Path(file.filename or '').name)):
                    raise HTTPException(415, 'unsupported_share_file')
                # 文本附件也不能绕过凭据限制。
                if Path(file.filename).suffix.lower() in {'.txt', '.md'}:
                    sample_bytes = await file.read(12 * 1024 * 1024 + 1)
                    if len(sample_bytes) > 12 * 1024 * 1024:
                        raise HTTPException(413, 'file_size_invalid')
                    sample = sample_bytes.decode('utf-8', errors='replace')
                    await file.seek(0)
                    if re.search(r'(?i)(# Netscape HTTP Cookie File|authorization\s*:|(?:^|\n)\s*(?:cookie|set-cookie)\s*:)', sample):
                        raise HTTPException(400, 'share_credentials_rejected')
            if not files and not text:
                raise HTTPException(400, 'empty_share')
            signature = {'project_id': project, 'text': text,
                         'files': [[f.filename, f.size, f.content_type] for f in files]}
            async with locks.setdefault(key, asyncio.Lock()):
                with records.begin() as tx:
                    row = tx.read(CAPTURES, key)
                    if row is None:
                        row = tx.put(CAPTURES, key, {'signature': signature, 'project_id': project,
                            'items': [], 'results': [], 'uploading': False, 'instance': instance,
                            'updated_at': _now()}, expected_revision=0)
                    elif row.payload['signature'] != signature:
                        raise HTTPException(409, 'idempotency_key_conflict')
                    tx.commit()
                state = row.payload
                if state.get('complete'):
                    return {'results': state['results']}
                # 进程在文件落盘与旁路记录之间退出时，不盲目重传制造重复原件。
                if state['uploading']:
                    raise HTTPException(409, 'share_upload_interrupted')
                groups = ([files] if files and all(Path(f.filename).suffix.lower() in IMAGES for f in files)
                          else [[file] for file in files])
                items = list(state['items'])
                for group in groups[len(items):]:
                    save(key, {'uploading': True})
                    try:
                        item = await (workspace.intake.add_images(project, group) if len(group) > 1
                                      else workspace.intake.add_file(project, group[0]))
                    except Exception:
                        save(key, {'uploading': False})
                        raise
                    items.append(item['id'])
                    save(key, {'items': items, 'uploading': False})
                results = list(state['results'])
                for index in range(len(results), max(1, len(items))):
                    body = {'project_id': project, 'intent': 'remember', 'text': text if index == 0 else ''}
                    if items:
                        body['item_id'] = items[index]
                    turn_key = 'share-' + key + '-' + str(index)
                    prior = records.read(REQUESTS, turn_key)
                    if prior is not None and prior.payload['state'] in {'failed', 'interrupted'}:
                        raise HTTPException(409, 'share_turn_interrupted')
                    try:
                        result = await executions.run(body, turn_key)
                    except Exception:
                        prior = records.read(REQUESTS, turn_key)
                        if prior is not None and prior.payload['state'] in {'failed', 'interrupted'}:
                            raise HTTPException(409, 'share_turn_interrupted') from None
                        raise
                    actual = records.read('v2_turns', result['turn']['id'])
                    results.append({**result, 'project_id': actual.payload['project_id']})
                    save(key, {'results': results})
                save(key, {'complete': True})
                return {'results': results}
