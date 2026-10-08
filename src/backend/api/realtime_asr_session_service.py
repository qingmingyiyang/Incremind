from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from backend.api.qwen_realtime_asr import build_finish_task, parse_qwen_event
from core.product_core.realtime_asr_provider_settings import QWEN_REALTIME_ASR_SECRET_REF


_MAX_CLIENT_CHUNK_BYTES = 64 * 1024


class RealtimeSessionService:
    """Session evidence authority for bounded, non-replayable realtime ASR.

    Core's durable Effect scheduler is intentionally not used for microphone
    frames: once sent, audio is neither persisted nor replayed.  The existing
    object-store session snapshot is the execution fact; this service adds one
    immutable terminal receipt, so a completed/cancelled/unknown session has a
    stable read-back result without inventing a second scheduler.
    """

    _FACTS = "realtime_asr_session_snapshots"
    _RECEIPTS = "realtime_asr_session_receipts"

    def __init__(self, store: object) -> None:
        self._store = store

    def start(self, snapshot: Mapping[str, object]) -> None:
        self._store.write(self._FACTS, str(snapshot["id"]), dict(snapshot), expected_revision=None)

    def update(self, snapshot: dict[str, object], **changes: object) -> None:
        snapshot.update(changes)
        self._store.write(self._FACTS, str(snapshot["id"]), dict(snapshot), expected_revision=None)

    def settle(self, snapshot: dict[str, object], *, status: str, error_code: str = "") -> dict[str, object]:
        self.update(snapshot, status=status, error_code=error_code)
        receipt_id = f"receipt-{snapshot['id']}"
        receipt = {
            "schema_version": "1.0.0", "id": receipt_id,
            "session_id": snapshot["id"], "status": status,
            "error_code": error_code, "audio_bytes_sent": snapshot.get("audio_bytes_sent", 0),
            "secret_generation": snapshot.get("secret_generation", 0),
            "receipt_kind": "realtime-asr-session-terminal-v1",
        }
        existing = self._store.read(self._RECEIPTS, receipt_id)
        if existing is None:
            self._store.write(self._RECEIPTS, receipt_id, receipt, expected_revision=None)
        elif dict(existing) != receipt:
            raise RuntimeError("realtime_asr_terminal_receipt_drift")
        return receipt

    def receipt(self, session_id: str) -> Mapping[str, object] | None:
        return self._store.read(self._RECEIPTS, f"receipt-{session_id}")

    async def relay_until_terminal(self, websocket: object, upstream: object, *, task_id: str, max_audio_bytes: int, secret_generation: int, container: object, policy: object, manifest: object, vocabulary_bytes: int, timeout_seconds: int) -> tuple[str, int]:
        """Own the bounded, non-replayable microphone stream to its terminal fact."""
        try:
            return await asyncio.wait_for(
                self._relay(websocket, upstream, task_id=task_id, max_audio_bytes=max_audio_bytes,
                            secret_generation=secret_generation, container=container, policy=policy,
                            manifest=manifest, vocabulary_bytes=vocabulary_bytes),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            raise RealtimeSessionTimeout("stream_timeout") from error

    async def _relay(self, websocket: object, upstream: object, *, task_id: str, max_audio_bytes: int, secret_generation: int, container: object, policy: object, manifest: object, vocabulary_bytes: int) -> tuple[str, int]:
        client_task: asyncio.Task | None = asyncio.create_task(websocket.receive())
        upstream_task: asyncio.Task = asyncio.create_task(upstream.recv())
        bytes_sent = 0
        finishing = False
        try:
            while True:
                pending = [upstream_task] + ([client_task] if client_task is not None else [])
                done, _waiting = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if client_task is not None and client_task in done:
                    message = client_task.result()
                    if message.get("type") == "websocket.disconnect":
                        raise RealtimeClientDisconnected(int(message.get("code", 1000)))
                    audio = message.get("bytes")
                    text = message.get("text")
                    if isinstance(audio, bytes):
                        if finishing or not audio or len(audio) > _MAX_CLIENT_CHUNK_BYTES:
                            raise RuntimeError("client_audio_chunk_invalid")
                        bytes_sent += len(audio)
                        if bytes_sent > max_audio_bytes:
                            raise RuntimeError("client_audio_budget_exceeded")
                        if container.secret_store.get_generation(QWEN_REALTIME_ASR_SECRET_REF) != secret_generation:
                            raise RuntimeError("secret_generation_drifted")
                        policy.validate(
                            manifest,
                            purpose="realtime_transcription",
                            payload_categories=("microphone_pcm_audio", "session_vocabulary"),
                            payload_bytes=bytes_sent + vocabulary_bytes,
                        )
                        await upstream.send(audio)
                        client_task = asyncio.create_task(websocket.receive())
                    elif isinstance(text, str):
                        command = json.loads(text)
                        action = command.get("action") if isinstance(command, Mapping) else None
                        if action == "finish" and not finishing:
                            finishing = True
                            await upstream.send(json.dumps(build_finish_task(task_id=task_id)))
                            client_task = None
                        elif action == "cancel":
                            await websocket.send_json({"type": "cancelled"})
                            await websocket.close(code=1000)
                            return "cancelled", bytes_sent
                        else:
                            raise RuntimeError("client_command_invalid")
                    else:
                        raise RuntimeError("client_message_invalid")
                if upstream_task in done:
                    raw = upstream_task.result()
                    if not isinstance(raw, str):
                        raise RuntimeError("provider_event_invalid")
                    event = parse_qwen_event(json.loads(raw))
                    if event.kind in {"partial", "final"}:
                        await websocket.send_json({
                            "type": event.kind, "text": event.text,
                            "sentence_id": event.sentence_id,
                            "duration_seconds": event.duration_seconds,
                        })
                    elif event.kind == "failed":
                        raise RuntimeError("provider_task_failed")
                    elif event.kind == "finished":
                        await websocket.send_json({"type": "finished"})
                        await websocket.close(code=1000)
                        return "finished", bytes_sent
                    upstream_task = asyncio.create_task(upstream.recv())
        finally:
            for task in (client_task, upstream_task):
                if task is not None and not task.done():
                    task.cancel()


class RealtimeSessionTimeout(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code


class RealtimeClientDisconnected(RuntimeError):
    def __init__(self, code: int) -> None:
        self.code = code


__all__ = ("RealtimeClientDisconnected", "RealtimeSessionService", "RealtimeSessionTimeout")
