"""Transactional ownership of a workspace item's processing run."""

from __future__ import annotations

import math
import os
import time
from dataclasses import replace
from collections.abc import Callable, Mapping

from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
from core.storage_provider.source_retrieval_index import project_original


_LEASE_KEYS = (
    "processing_instance_id", "processing_run_id", "processing_lease_expires_at",
    "processing_heartbeat_at", "processing_pid", "processing_consent",
)
_HEARTBEATS = "workspace_processing_heartbeats"


class ProcessingLeaseConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProcessingLease:
    def __init__(
        self, records: SQLiteStructuredRecordStore, collection: str, instance_id: str,
        clock: Callable[[], float] = time.time, ttl_seconds: float = 60,
    ) -> None:
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("instance_id is required")
        if not isinstance(ttl_seconds, (int, float)) or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.records = records
        self.collection = collection
        self.instance_id = instance_id
        self.clock = clock
        self.ttl_seconds = float(ttl_seconds)

    def claim(
        self, item_id: str, project_id: str, expected_revision: int, run_id: str,
        consent: dict | None, changes: Mapping[str, object],
    ) -> dict:
        if not isinstance(run_id, str) or not run_id:
            raise ProcessingLeaseConflict("processing_run_changed")
        with self.records.begin() as tx:
            row = self._row(tx, item_id, project_id)
            if row.revision != expected_revision:
                raise ProcessingLeaseConflict("record_revision_conflict")
            if row.payload.get("status") not in {"staged", "failed"}:
                raise ProcessingLeaseConflict("invalid_item_state")
            now = self._now()
            receipts = row.payload.get("remote_processing_receipts")
            receipts = list(receipts) if isinstance(receipts, list) else []
            if consent is not None:
                receipts.append(consent)
            payload = {
                **row.payload, **changes, "status": "processing", "error": None,
                "remote_processing_receipts": receipts,
                "processing_instance_id": self.instance_id,
                "processing_run_id": run_id,
                "processing_lease_expires_at": now + self.ttl_seconds,
                "processing_heartbeat_at": now,
                "processing_pid": os.getpid(),
                "processing_consent": consent,
            }
            result = self._put(tx, item_id, payload, row.revision)
            tx.commit()
            return self._result(result)

    def guard(self, item_id: str, project_id: str, run_id: str) -> dict:
        with self.records.begin() as tx:
            row = self._owned(tx, item_id, project_id, run_id)
            result = self._result(row)
            tx.commit()
            return result

    def apply(self, item_id: str, project_id: str, run_id: str, changes: Mapping[str, object]) -> dict:
        with self.records.begin() as tx:
            row = self._owned(tx, item_id, project_id, run_id)
            payload = {**row.payload, **changes}
            if payload.get("status") != "processing":
                payload = self._clear(payload)
            else:
                # A caller's partial changes cannot replace the lease identity.
                for key in _LEASE_KEYS:
                    payload[key] = row.payload.get(key)
            result = self._put(tx, item_id, payload, row.revision)
            tx.commit()
            return self._result(result)

    def heartbeat(self, item_id: str, project_id: str, run_id: str) -> bool:
        with self.records.begin() as tx:
            try:
                row = self._owned(tx, item_id, project_id, run_id)
            except ProcessingLeaseConflict:
                return False
            now = self._now()
            payload = {**row.payload, "processing_heartbeat_at": now,
                       "processing_lease_expires_at": now + self.ttl_seconds}
            heartbeat = tx.read(_HEARTBEATS, item_id)
            tx.put(_HEARTBEATS, item_id, {key: payload[key] for key in _LEASE_KEYS
                                        if key != "processing_consent"},
                   expected_revision=heartbeat.revision if heartbeat else 0)
            tx.commit()
            return True

    def interrupt(self, item_id: str, project_id: str, run_id: str) -> bool:
        with self.records.begin() as tx:
            try:
                row = self._owned(tx, item_id, project_id, run_id)
            except ProcessingLeaseConflict:
                return False
            payload = self._clear({**row.payload, "status": "failed", "error": "processing_interrupted"})
            self._put(tx, item_id, payload, row.revision)
            tx.commit()
            return True

    def recover_expired(self) -> int:
        recovered = 0
        now = self._now()
        candidates = self.records.list_matching(self.collection, status="processing")
        for candidate in candidates:
            if self._valid(self._with_heartbeat(self.records, candidate).payload, now):
                continue
            # Discovery is read-only; each short write transaction rechecks a
            # candidate so a concurrent renewal/reclaim is never interrupted.
            with self.records.begin() as tx:
                row = tx.read(self.collection, candidate.object_id)
                if (row is None or row.revision != candidate.revision
                        or row.payload.get("status") != "processing"
                        or self._valid(self._with_heartbeat(tx, row).payload, self._now())):
                    continue
                payload = self._clear({**row.payload, "status": "failed", "error": "processing_interrupted"})
                self._put(tx, row.object_id, payload, row.revision)
                recovered += 1
                tx.commit()
        return recovered

    def _put(self, tx, identity, payload, revision):
        previous = tx.read(self.collection, identity) if self.collection == 'workspace_items' else None
        row = tx.put(self.collection, identity, payload, expected_revision=revision)
        if self.collection == 'workspace_items':
            project_original(tx, identity, payload['project_id'], row.revision,
                             str(payload.get('source_text') or ''), document_id=payload.get('document_id'),
                             previous_document_id=previous.payload.get('document_id') if previous else None)
        return row

    def _row(self, tx, item_id: str, project_id: str):
        row = tx.read(self.collection, item_id)
        if row is None or row.payload.get("project_id") != project_id:
            raise ProcessingLeaseConflict("item_not_found")
        return self._with_heartbeat(tx, row)

    @staticmethod
    def _with_heartbeat(reader, row):
        heartbeat = reader.read(_HEARTBEATS, row.object_id)
        if heartbeat and row.payload.get("status") == "processing" and all(
            heartbeat.payload.get(key) == row.payload.get(key)
            for key in ("processing_instance_id", "processing_run_id", "processing_pid")
        ):
            # Renewal changes execution ownership time, never source revision.
            return replace(row, payload={**row.payload, **heartbeat.payload})
        return row

    def _owned(self, tx, item_id: str, project_id: str, run_id: str):
        row = self._row(tx, item_id, project_id)
        if (row.payload.get("status") != "processing"
                or row.payload.get("processing_instance_id") != self.instance_id
                or row.payload.get("processing_run_id") != run_id):
            raise ProcessingLeaseConflict("processing_run_changed")
        if not self._valid(row.payload, self._now()):
            raise ProcessingLeaseConflict("processing_lease_expired")
        return row

    def _valid(self, payload: Mapping[str, object], now: float) -> bool:
        expiry = payload.get("processing_lease_expires_at")
        heartbeat = payload.get("processing_heartbeat_at")
        return (
            isinstance(payload.get("processing_instance_id"), str)
            and bool(payload["processing_instance_id"])
            and isinstance(payload.get("processing_run_id"), str)
            and bool(payload["processing_run_id"])
            and type(expiry) in (int, float) and math.isfinite(expiry) and expiry > now
            and type(heartbeat) in (int, float) and math.isfinite(heartbeat)
            and heartbeat <= now and heartbeat < expiry
            and expiry <= heartbeat + self.ttl_seconds + 0.000001
        )

    def _now(self) -> float:
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("clock must return finite time")
        return now

    @staticmethod
    def _clear(payload: dict) -> dict:
        for key in _LEASE_KEYS:
            payload[key] = None
        return payload

    @staticmethod
    def _result(row) -> dict:
        return {**row.payload, "revision": row.revision}
