"""Intake and model processing orchestration; no route registration."""

from __future__ import annotations

from core.storage_provider.source_retrieval_index import project_original

import asyncio
import json
import mimetypes
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context, ContextVar
from functools import partial, wraps
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from fastapi import File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool
from .model_config import ModelConfigurationError
from .processing_lease import ProcessingLeaseConflict
from backend.memory_app.workspace_media_url import media_platform
from core.product_core.local_document_text_extractor import BuiltinDocumentTextExtractor
from . import workspace_audio as audio
from . import workspace_generation as generation
from . import workspace_links as links
from .workspace_contracts import _COLLECTION, _MAX_FILE, _MAX_TEXT, _now, _project, _public, _text, _optional_title
from .privacy_policy import egress_allowed, is_private_project


_PROCESSING_HEARTBEAT_INTERVAL = 10
_PROCESSING_HEARTBEAT_RETRY_INTERVAL = 1
_PROCESSING_HEARTBEAT_MAX_RETRIES = 3
_PROCESSING_HEARTBEAT_DRAIN_TIMEOUT = 10
# Shared execution capacity, not request-local pools. Model/ASR work must not
# consume the workers needed to renew or interrupt its own lease.
_LEASE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workspace-lease")
_INTAKE_BUDGET = ContextVar('workspace_intake_budget', default=None)


def _admitted(operation):
    @wraps(operation)
    async def run(owner, *args, **kwargs):
        if owner.admission is None:
            return await operation(owner, *args, **kwargs)
        async with owner.admission() as check:
            token = _INTAKE_BUDGET.set(check)
            try:
                return await operation(owner, *args, **kwargs)
            finally:
                _INTAKE_BUDGET.reset(token)
    return run


def _check_intake_bytes(size):
    check = _INTAKE_BUDGET.get()
    if check is not None:
        check(size)


async def _run_lease_operation(operation, *args):
    return await asyncio.get_running_loop().run_in_executor(
        _LEASE_EXECUTOR, copy_context().run, operation, *args)


def _authorization_snapshot(models, asr_target):
    public = getattr(models, "public", None)
    configuration = public() if callable(public) else {}
    # ASR settings live in the existing cloud provider store rather than in
    # ModelConfiguration. Adapt that actual target to the shared privacy gate.
    return {**configuration, "asr": {"enabled": asr_target is not None}}


def _settings_revision(configuration):
    return {"generation": configuration.get("generation", {}).get("revision", 0),
            "mode": configuration.get("generation_mode", {}).get("revision", 0)}


def _sqlite_lock_contention(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    # Older adapters/test doubles may omit SQLite's structured error code.
    return str(exc).lower() in {"database is locked", "database table is locked", "database is busy"}


async def _read_existing_source(owner, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote):
    return item, item["source_text"]


async def _read_media_source(reader, owner, item, project_id, item_id, run_id, validate_remote):
    media = await run_in_threadpool(reader, item["media_request_url"], owner.runtime_root,
        project_id=project_id, item_id=item_id, run_id=run_id,
        validate_remote=lambda: validate_remote("asr"))
    source = media["source_text"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source_text_empty")
    raw = source
    source = source.strip()
    if len(source) > _MAX_TEXT:
        raise ValueError("source_text_too_large")
    sections = media.get('_source_sections')
    if sections is not None:
        from .v2.source_sections import trimmed_capture
        sections = trimmed_capture(raw, sections)
    item = owner.items.update(item_id, project_id, {"processing"}, expected_run_id=run_id,
        source_sections=sections,
        source_text=source, title=media["title"], url=media["canonical_url"],
        acquisition_method=media["acquisition_method"], content_kind=media["content_kind"],
        media_request_url=None)
    return item, source


async def _read_bilibili_source(owner, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote):
    from .workspace_bilibili_media import read_bilibili_media
    return await _read_media_source(partial(read_bilibili_media, with_sections=True), owner, item, project_id, item_id, run_id, validate_remote)


async def _read_xiaohongshu_source(owner, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote):
    from .workspace_xhs_media import read_xiaohongshu_media
    return await _read_media_source(read_xiaohongshu_media, owner, item, project_id, item_id, run_id, validate_remote)


async def _read_audio_source(owner, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote):
    if checkpoint is not None:
        return item, checkpoint["text"]
    try:
        original_identity = audio._audio_original_identity(Path(item["original_path"]))
        transcription_path = Path(item["original_path"])
        if item["input_kind"] == "video" and not transcription_path.resolve().is_relative_to(owner.root.resolve()):
            raise ValueError("audio_transcription_evidence_invalid")
        if item["input_kind"] == "video" and asr_target is None:
            transcription_path, _ = await run_in_threadpool(audio._cloud_audio_derivative,
                transcription_path, owner.runtime_root, run_id)
        output = await run_in_threadpool(audio._transcribe_output, transcription_path, owner.runtime_root,
            project_id, item_id, run_id, validate_remote=lambda: validate_remote("asr"))
        source = str(output["text"]).strip()
        checkpoint = audio._make_audio_transcription(item, owner.runtime_root, run_id, output, original_identity,
            store_kind="rebuild" if asr_target else "workspace_asr_internal")
        item = owner.items.update(item_id, project_id, {"processing"}, expected_run_id=run_id,
            source_text=source, audio_transcription=checkpoint)
        if audio._audio_original_identity(Path(item["original_path"])) != original_identity:
            raise ValueError("audio_transcription_evidence_invalid")
    except ValueError as exc:
        if str(exc) in {"asr_unavailable", "remote_processing_target_changed", "audio_transcription_evidence_invalid"}:
            raise
        raise ValueError("audio_transcription_failed") from None
    except HTTPException:
        raise
    except Exception:
        raise ValueError("audio_transcription_failed") from None
    return item, source


async def _read_image_source(owner, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote):
    from .uploaded_media import read_uploaded_image
    source = await run_in_threadpool(read_uploaded_image, owner, item, project_id, item_id, run_id)
    if not isinstance(source, str):
        raise ValueError('source_text_empty')
    if source:
        source = _text(source, "source_text")
    item = owner.items.update(item_id, project_id, {"processing"}, expected_run_id=run_id, source_text=source)
    return item, source


async def _read_xhs_images_source(owner, item, project_id, item_id, run_id, capture, binding, validate_remote):
    from backend.recognition import RecognitionError, RecognitionConflict
    from .uploaded_media import UploadedImageReference
    from .v2.image_read import read_uploaded_images, validate_image_request
    from .v2.source_sections import paired_input, build_xhs_capture
    from .workspace_xhs_media import _read_captured_note

    def validate():
        row = owner.items.item_for(item_id, project_id)
        if (row.payload.get('status') != 'processing' or row.payload.get('processing_run_id') != run_id
                or paired_input(owner.items.records, row) != binding):
            raise RecognitionConflict('comment_source_invalid')
        validate_remote('generation')
        return row

    validate()
    media = await run_in_threadpool(_read_captured_note, capture, owner.runtime_root,
        project_id=project_id, item_id=item_id, run_id=run_id, allow_nonvideo=True,
        validate_remote=lambda: validate_remote('asr'))
    validate()
    if not isinstance(media['source_text'], str) or not media['source_text'].strip():
        raise ValueError('source_text_empty')
    if len(media['source_text']) > _MAX_TEXT:
        raise ValueError('source_text_too_large')
    unavailable, ocr, spans = False, '', []
    try:
        ocr, spans = await run_in_threadpool(read_uploaded_images, owner, project_id, item_id, run_id,
            UploadedImageReference, with_ranges=True)
    except (RecognitionError, ProcessingLeaseConflict, HTTPException):
        raise
    except Exception:
        # Provider/command failure may lose OCR, never current authority checks.
        validate()
        identity = {'kind': 'media.image_read', 'project': project_id,
            'key': 'image:' + item_id + ':' + run_id, 'purpose': 'vision'}
        turn = next((saved for saved in owner.items.records.list('v2_memory_turn_keys')
            if saved.payload['identity'] == identity), None)
        if turn is not None and owner.items.records.read('v2_image_read_bindings', turn.object_id) is not None:
            validate_image_request(owner.items.records, owner.models, turn.payload['request'])
        unavailable = True
    row = validate()
    sections = build_xhs_capture(owner.items.records, row, binding, media, ocr, spans,
        run_id=run_id, maximum=_MAX_TEXT, unavailable=unavailable)
    item = owner.items.update(item_id, project_id, {'processing'}, expected_run_id=run_id,
        source_text=sections['source_text'], source_sections=sections,
        title=media['title'], platform='xiaohongshu', acquisition_method=media['acquisition_method'],
        content_kind=media['content_kind'])
    return item, sections['source_text']


SOURCE_READERS = MappingProxyType({
    "text": _read_existing_source, "file": _read_existing_source, "link": _read_existing_source,
    "image": _read_image_source, "video": _read_audio_source, "audio": _read_audio_source, "bilibili": _read_bilibili_source, "xiaohongshu": _read_xiaohongshu_source,
})


class WorkspaceIntake:
    def __init__(self, runtime_root, items, models, *, admission=None, job_submitter=None):
        self.runtime_root = runtime_root
        self.root = runtime_root / "workspace"
        self.items = items
        self.models = models
        self.admission = admission
        self.job_submitter = job_submitter

    def with_items(self, items, models):
        """Reuse intake behavior with an enlisted transactional item store."""
        return WorkspaceIntake(self.runtime_root, items, models, admission=self.admission,job_submitter=self.job_submitter)

    def read_images(self, project_id, item_id, run_id, reference_factory):
        from .v2.image_read import read_uploaded_images
        return read_uploaded_images(self, project_id, item_id, run_id, reference_factory)

    async def acquire_source(self, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote,
                             *, xhs_capture=None, xhs_binding=None):
        if xhs_binding is not None:
            return await _read_xhs_images_source(self, item, project_id, item_id, run_id,
                xhs_capture, xhs_binding, validate_remote)
        if item.get('source_text'):
            from .v2.source_sections import validated_xhs_text
            retained = validated_xhs_text(self.items.records, self.items.item_for(item_id, project_id))
            if retained is not None:
                return item, retained
        key = (item["platform"] if item.get("platform") in {"bilibili", "xiaohongshu"}
               and not item["source_text"] else item["input_kind"])
        reader = SOURCE_READERS.get(key, _read_existing_source)
        item, source = await reader(self, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote)
        if key in {"bilibili", "xiaohongshu"} and item["input_kind"] == "audio":
            return await SOURCE_READERS["audio"](self, item, project_id, item_id, run_id, asr_target, checkpoint, validate_remote)
        return item, source

    @_admitted
    async def add_text(self, body: dict, *, preserve_text: bool = False):
        text = _text(body.get("text"), "text")
        title = _optional_title(body.get("title")) or text.splitlines()[0][:80]
        text = body["text"] if preserve_text else text
        _check_intake_bytes(len(text.encode('utf8')))
        return self.items.create(_project(body.get("project_id", "default")), "text", title, text)

    async def bind_link_images(self, item_id, project_id, url, *, expected_revision):
        if (not isinstance(url, str) or len(url) > 2048 or url != url.strip()
                or media_platform(url) != 'xiaohongshu'):
            raise HTTPException(400, 'invalid_url')
        return self.items.bind_link_images(item_id, _project(project_id), url,
            expected_revision=expected_revision, runtime_root=self.runtime_root)

    @_admitted
    async def add_link(self, body: dict):
        project_id = _project(body.get("project_id", "default"))
        url = body.get("url")
        if not isinstance(url, str) or len(url) > 2048:
            raise HTTPException(400, "invalid_url")
        platform = media_platform(url)
        _check_intake_bytes(0)
        if platform:
            parsed = urlsplit(url)
            public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            return _public(self.items.create(project_id, "link", f"{platform} 视频", "", url=public_url,
                                  platform=platform, content_kind="video", media_request_url=url))
        # Fetch before creating an item so rejected addresses never become retryable work.
        content = await run_in_threadpool(links._fetch_url, url)
        _check_intake_bytes(len(content.encode('utf8')))
        return self.items.create(project_id, "link", urlsplit(url).hostname or url, content, url=url)

    @_admitted
    async def add_file(self, project_id: str = Form("default"), file: UploadFile = File(...)):
        project_id = _project(project_id)
        name = Path(file.filename or "upload").name[:180]
        suffix = Path(name).suffix.lower()
        from .uploaded_media import MAX_VIDEO_BYTES, VIDEO_SUFFIXES
        is_video = suffix in VIDEO_SUFFIXES or (suffix == ".webm" and file.content_type == "video/webm")
        maximum = MAX_VIDEO_BYTES if is_video else _MAX_FILE
        if not is_video and suffix not in {".txt", ".md", ".pdf", ".docx", ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
            raise HTTPException(415, "unsupported_file_type")
        item_id = "workspace-" + uuid4().hex
        path = self.root / (item_id + suffix)
        kind = "video" if is_video else "audio" if suffix in {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"} else "image" if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"} else "file"
        created = False
        try:
            size = 0
            with path.open("xb") as output:
                created = True
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > maximum:
                        raise HTTPException(413, "file_size_invalid")
                    _check_intake_bytes(len(chunk))
                    await run_in_threadpool(output.write, chunk)
            if not size:
                raise HTTPException(413, "file_size_invalid")
            if suffix in {".txt", ".md"}:
                content = _text(path.read_bytes().decode("utf-8-sig"), "source_text")
            elif kind in {"audio", "image", "video"}:
                content = ""
            else:
                content = _text(await run_in_threadpool(BuiltinDocumentTextExtractor().extract, path,
                                mimetypes.guess_type(name)[0] or "application/octet-stream"), "source_text")
            payload = dict(id=item_id, project_id=project_id, input_kind=kind, title=name,
                           source_text=content, status="staged", draft=None, error=None,
                           document_id=None, created_at=_now(), original_path=str(path), original_name=name)
            self.items.create_upload(payload)
            return _public(payload)
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise

    @_admitted
    async def add_images(self, project_id, files):
        """One ordered image group, with the existing file budget shared by all."""
        project_id = _project(project_id)
        if not isinstance(files, list) or not files:
            raise HTTPException(400, 'image_group_invalid')
        names = [Path(file.filename or 'upload').name[:180] for file in files]
        if any(Path(name).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'} for name in names):
            raise HTTPException(415, 'unsupported_file_type')
        item_id = 'workspace-' + uuid4().hex
        paths, images, total = [], [], 0
        try:
            for ordinal, (file, name) in enumerate(zip(files, names), 1):
                path = self.root / f'{item_id}-{ordinal}{Path(name).suffix.lower()}'
                size = 0
                with path.open('xb') as output:
                    paths.append(path)
                    while chunk := await file.read(1024 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > _MAX_FILE:
                            raise HTTPException(413, 'file_size_invalid')
                        _check_intake_bytes(len(chunk))
                        await run_in_threadpool(output.write, chunk)
                if not size:
                    raise HTTPException(413, 'file_size_invalid')
                images.append({'name': name, 'path': str(path)})
            payload = dict(id=item_id, project_id=project_id, input_kind='image', title=names[0],
                source_text='', status='staged', draft=None, error=None, document_id=None,
                created_at=_now(), original_path=str(paths[0]), original_name=names[0])
            self.items.create_upload(payload, images=images)
            return _public(payload)
        except BaseException:
            for path in paths:
                path.unlink(missing_ok=True)
            raise

    async def process(self, item_id: str, body: dict):
        if self.job_submitter is not None:
            return await self.job_submitter('intake',lambda:self._process(item_id,body))
        return await self._process(item_id,body)

    async def _process(self, item_id: str, body: dict):
        project_id = _project(body.get("project_id", "default"))
        self.items.processing_lease.recover_expired()
        current_row = self.items.item_for(item_id, project_id)
        current = current_row.payload
        if current["status"] not in {"staged", "failed"}:
            raise HTTPException(409, "invalid_item_state")
        audio_checkpoint = None
        if current["input_kind"] in {"audio", "video"} and current.get("audio_transcription") is not None:
            audio_checkpoint = audio._validated_audio_transcription(current, self.runtime_root)
        generation_target = generation._remote_generation_target(self.models)
        from .v2.source_sections import paired_input
        xhs_binding = paired_input(self.items.records, current_row)
        xhs_capture, xhs_error = None, None
        if xhs_binding is not None:
            # Keep the existing generation ceiling before any public capture.
            if generation_target and not egress_allowed(self.items.records, self.models, project_id, 'generation'):
                raise HTTPException(409, 'private_project_remote_blocked' if is_private_project(
                    self.items.records, project_id) else 'remote_disabled')
            from .workspace_xhs_media import _capture_public_note
            try:
                xhs_capture = await run_in_threadpool(_capture_public_note, xhs_binding['request_url'])
            except Exception as error:
                xhs_error = error
            fresh = self.items.item_for(item_id, project_id)
            if fresh.revision != current_row.revision or paired_input(self.items.records, fresh) != xhs_binding:
                raise HTTPException(409, 'item_revision_conflicted')
        asr_target = (
            audio._remote_asr_target(self.runtime_root)
            if (current["input_kind"] in {"audio", "video"} and audio_checkpoint is None)
            or (current.get("platform") in {"bilibili", "xiaohongshu"} and not current.get("source_text"))
            or (xhs_capture is not None and xhs_capture.content_kind == 'video')
            else None
        )
        configuration = _authorization_snapshot(self.models, asr_target)
        authorization_models = SimpleNamespace(public=lambda: configuration)
        for purpose, target in (("generation", generation_target), ("asr", asr_target)):
            if target and not egress_allowed(self.items.records, authorization_models, project_id, purpose):
                code = ("private_project_remote_blocked" if is_private_project(self.items.records, project_id)
                        else "remote_disabled")
                raise HTTPException(409, code)
        run_id = "workspace-run-" + uuid4().hex
        consent = (
            {"item_id": item_id, "project_id": project_id, "run_id": run_id,
             "consent_scope": "global_setting", "settings_revision": _settings_revision(configuration),
             "consented_at": _now(),
             "send_categories": (
                 (["model_instructions", "source_text"] if generation_target else [])
                 + (["audio_data", "audio_chunks", "provider_metadata"] if asr_target else [])
             ),
             "generation": generation_target, "asr": asr_target}
            if generation_target or asr_target else None
        )
        try:
            item = self.items.processing_lease.claim(item_id, project_id, current_row.revision, run_id,
                                          consent, {"status": "processing", "error": None})
        except ProcessingLeaseConflict as exc:
            raise HTTPException(409, exc.code) from None
        def validate_remote(kind: str) -> None:
            try:
                active = self.items.processing_lease.guard(item_id, project_id, run_id)
            except ProcessingLeaseConflict:
                raise ModelConfigurationError("remote_processing_target_changed") from None
            if (active.get("status") != "processing"
                    or active.get("processing_run_id") != run_id
                    or active.get("processing_consent") != consent):
                raise ModelConfigurationError("remote_processing_target_changed")
            actual = (
                generation._remote_generation_target(self.models) if kind == "generation"
                else audio._remote_asr_target(self.runtime_root)
            )
            expected = consent.get(kind) if isinstance(consent, dict) else None
            if actual != expected:
                raise ModelConfigurationError("remote_processing_target_changed")
            if actual is not None:
                current_configuration = _authorization_snapshot(self.models, actual if kind == "asr" else asr_target)
                current_models = SimpleNamespace(public=lambda: current_configuration)
                if (not egress_allowed(self.items.records, current_models, project_id, kind)
                        or _settings_revision(current_configuration) != consent["settings_revision"]):
                    raise ModelConfigurationError("remote_processing_target_changed")
                from .source_egress import SourceEgressService
                from .transaction_records import TransactionRecords
                from backend.recognition import WorkScope, RecognitionError
                try:
                    with self.items.records.begin() as reader:
                        row = reader.read("workspace_items",item_id)
                        authority = SourceEgressService(TransactionRecords(reader))
                        snapshot = authority.snapshot(WorkScope("local-user",project_id),[
                            {"type":"original_item","id":item_id,"revision":row.revision}])
                        authority.require(snapshot,"generation")
                except RecognitionError:
                    raise ModelConfigurationError("private_source_remote_blocked") from None

        started = time.monotonic()
        stop_heartbeat = asyncio.Event()

        async def keep_lease_alive() -> None:
            retries = 0
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=(
                        _PROCESSING_HEARTBEAT_RETRY_INTERVAL if retries else _PROCESSING_HEARTBEAT_INTERVAL
                    ))
                except asyncio.TimeoutError:
                    if stop_heartbeat.is_set():
                        return
                    try:
                        if not await _run_lease_operation(self.items.processing_lease.heartbeat, item_id, project_id, run_id):
                            return
                        retries = 0
                    except sqlite3.OperationalError as exc:
                        if not _sqlite_lock_contention(exc):
                            raise
                        if retries >= _PROCESSING_HEARTBEAT_MAX_RETRIES:
                            # No renewal occurred. Existing expiry/recovery and
                            # ownership checks still reject late results.
                            return
                        retries += 1

        heartbeat_task = asyncio.create_task(keep_lease_alive())
        try:
            if xhs_error is not None:
                raise xhs_error
            item, source = await self.acquire_source(
                item, project_id, item_id, run_id, asr_target, audio_checkpoint, validate_remote,
                xhs_capture=xhs_capture, xhs_binding=xhs_binding,
            )
            image_draft = None
            if not source:
                from .v2.image_read import image_only_draft
                if item['input_kind'] == 'image':
                    image_draft = image_only_draft(self.items.records, self.items.item_for(item_id, project_id))
                if image_draft is None:
                    raise ValueError("source_text_empty")
            if len(source) > _MAX_TEXT:
                raise ValueError("source_text_too_large")
            if image_draft is not None:
                stop_heartbeat.set()
                await asyncio.wait_for(heartbeat_task, timeout=_PROCESSING_HEARTBEAT_DRAIN_TIMEOUT)
                return _public(self.items.update(item_id, project_id, {'processing'}, expected_run_id=run_id,
                    source_text='', draft=image_draft, title=image_draft['title'], status='ready',
                    processing_seconds=round(time.monotonic() - started, 2)))
            local_model = generation._is_local_model(self.models)
            validate_remote("generation")
            completion_options = {
                "max_tokens": 768 if local_model else 7000,
                "validate_current": lambda: validate_remote("generation"),
            }
            from .v2.organize_turns import OrganizeTurns
            organize = OrganizeTurns(root=self.runtime_root, records=self.items.records,
                models=self.models, item_id=item_id, project_id=project_id, source=source,
                validate_current=completion_options["validate_current"],
                lease=self.items.processing_lease, run_id=run_id)
            if (item.get("platform") in {"bilibili", "xiaohongshu"} or item["input_kind"] == "video") and len(source) > 5000:
                draft, model_meta = await run_in_threadpool(
                    generation._complete_chunked_video_draft, source,
                    completion_options["validate_current"], local_model,
                    organize=organize,
                )
            else:
                def validate_output(response):
                    value = json.loads(generation._strip_fence(response))
                    if local_model:
                        value = generation._ground_local_draft(value, source)
                    return generation._draft(value, source)
                response, model_meta = await run_in_threadpool(organize.complete, [
                    {"role": "system", "content": generation._PROMPT},
                    {"role": "user", "content": source},
                ], **completion_options, validate_output=validate_output)
                result = json.loads(generation._strip_fence(response))
                if local_model:
                    result = generation._ground_local_draft(result, source)
                draft = generation._draft(result, source)
            # Stop future renewals and settle the current one before publishing.
            # A timeout fails this run; it never permits a late result to publish.
            stop_heartbeat.set()
            await asyncio.wait_for(heartbeat_task, timeout=_PROCESSING_HEARTBEAT_DRAIN_TIMEOUT)
            return _public(self.items.update(item_id, project_id, {"processing"}, expected_run_id=run_id,
                                  source_text=source, draft=draft,
                                  title=draft["title"], status="ready", processing_seconds=round(time.monotonic() - started, 2),
                                  model_usage=model_meta.get("usage", {}), model_name=model_meta.get("model", "")))
        except asyncio.CancelledError:
            await _run_lease_operation(self.items.processing_lease.interrupt, item_id, project_id, run_id)
            raise
        except Exception as exc:
            # Keep provider text and credentials out of the persisted/public error.
            safe_errors = {"source_text_empty", "source_text_too_large", "asr_unavailable", "audio_transcription_failed",
                           "remote_processing_target_changed", "private_source_remote_blocked", "audio_transcription_evidence_invalid",
                           "model_output_incomplete", "model_output_missing_content", "model_response_invalid",
                           "model_not_configured", "unsupported_media_url", "invalid_source",
                           "media_short_link_unavailable", "media_short_link_target_invalid",
                           "media_short_link_redirect_limit", "media_short_link_address_blocked",
                           "bilibili_metadata_unavailable", "bilibili_video_unavailable",
                           "bilibili_asr_unavailable", "bilibili_video_transcription_failed",
                           "xiaohongshu_page_unavailable", "xiaohongshu_metadata_unavailable",
                           "xiaohongshu_video_required", "xiaohongshu_video_unavailable",
                           "xiaohongshu_asr_unavailable", "xiaohongshu_video_transcription_failed",
                           "xiaohongshu_video_speech_unavailable"}
            code = str(exc) if isinstance(exc, (ValueError, ModelConfigurationError)) and str(exc) in safe_errors else "processing_failed"
            if isinstance(exc, ModelConfigurationError) and str(exc).startswith("model_request_failed"):
                code = "model_request_failed"
            self.items.update(item_id, project_id, {"processing"}, expected_run_id=run_id,
                   status="failed", error=code,
                   processing_seconds=round(time.monotonic() - started, 2))
            if code in {"remote_processing_target_changed", "audio_transcription_evidence_invalid"}:
                raise HTTPException(409, code)
            return self.items.public_row(self.items.item_for(item_id, project_id))
        finally:
            stop_heartbeat.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
