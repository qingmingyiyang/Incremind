"""Durable Electron-owned PDF derivations of verified HTML Document Deliveries."""

from __future__ import annotations

import os
import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from core.effect_log import (
    EffectClass, EffectIntent, EffectLog, EffectRunner, EffectState,
    shared_effect_runner,
)
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from .document_delivery import DocumentDeliveryConflict, DocumentDeliveryError, DocumentDeliveryService

_OPERATIONS = "document_pdf_operations"
_RECEIPTS = "document_pdf_receipts"
_PUBLICATIONS = "document_pdf_publication_facts"
_PROFILES = {
"builtin.a4-document": {
    "profile_id": "builtin.a4-document",
    "revision": 1,
    "renderer_id": "electron.webcontents.print-to-pdf",
    "renderer_revision": 1,
    "print_options": {
        "pageSize": "A4", "landscape": False, "printBackground": True,
        "preferCSSPageSize": False, "displayHeaderFooter": False,
        "margins": {"top": 0.5, "bottom": 0.5, "left": 0.55, "right": 0.55},
    },
    "electron_major": 43,
},
"builtin.slide-document": {
    "profile_id": "builtin.slide-document",
    "revision": 1,
    "renderer_id": "electron.webcontents.print-to-pdf",
    "renderer_revision": 1,
    "print_options": {
        "pageSize": "A4", "landscape": True,
        "printBackground": True, "preferCSSPageSize": True, "displayHeaderFooter": False,
        "margins": {"top": 0, "bottom": 0, "left": 0, "right": 0},
    },
    "electron_major": 43,
},
}
_NAMESPACE = UUID("d4b677b0-9a4c-47be-a06c-0e13390b4e4f")
_EFFECT_LEASE_SECONDS = 300
_MAX_PDF_BYTES = 32 * 1024 * 1024


class DocumentPdfDeliveryError(ValueError):
    pass


class DocumentPdfDeliveryConflict(DocumentPdfDeliveryError):
    pass


class DocumentPdfDeliveryService:
    """Persist only PDF operation facts; source HTML remains the Delivery authority."""

    def __init__(self, records: SQLiteStructuredRecordStore, deliveries: DocumentDeliveryService,
                 runtime_root: Path, *, namespace_id: str = "default",
                 now: Callable[[], datetime] | None = None,
                 effect_log: EffectLog | None = None,
                 effect_runner: EffectRunner | None = None,
                 effect_owner: str = "document-pdf-electron") -> None:
        self.records, self.deliveries, self.runtime_root, self.namespace_id = records, deliveries, runtime_root, namespace_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.effects = (
            effect_runner.log if effect_runner is not None
            else effect_log or EffectLog(records.database_path)
        )
        self.effect_owner = effect_owner
        self.runner = effect_runner or shared_effect_runner(
            self.effects.database,
            owner_role=effect_owner,
            lease_seconds=_EFFECT_LEASE_SECONDS,
        )
        # A claim token is a process-local Electron capability, not a durable
        # execution lease. Core Effect remains the only winner and clock.
        self._claims: dict[str, tuple[str, dict[str, object]]] = {}

    def prepare(self, html_delivery_id: str, profile_id: str) -> dict[str, object]:
        profile = _PROFILES.get(profile_id)
        if profile is None:
            raise DocumentPdfDeliveryError("PDF profile is unsupported")
        try:
            delivery = self.deliveries.projection(html_delivery_id)
            if profile_id == "builtin.a4-document":
                file_name, print_source = self.deliveries.artifact(html_delivery_id, "html")
            else:
                file_name, _pptx = self.deliveries.artifact(html_delivery_id, "pptx")
                print_source = self.deliveries.slide_html(html_delivery_id)
        except (DocumentDeliveryError, DocumentDeliveryConflict) as exc:
            required = "verified HTML delivery" if profile_id == "builtin.a4-document" else "verified PPTX delivery"
            raise DocumentPdfDeliveryConflict(f"{required} is required") from exc
        if delivery is None or delivery.get("status") != "completed" or not print_source:
            raise DocumentPdfDeliveryConflict("verified document delivery is required")
        identity = f"{html_delivery_id}:{profile_id}:{profile['revision']}"
        operation_id = f"pdf-{uuid5(_NAMESPACE, identity)}"
        source = {
            "html_delivery_id": html_delivery_id,
            "html_receipt_ref": delivery.get("receipt_ref"),
            "html_file_name": file_name,
            "document_id": delivery.get("document_id"),
            "document_revision": delivery.get("document_revision"),
        }
        payload = {"schema_version": "1.0.0", "id": operation_id, "status": "waiting_for_electron",
                   "source": source, "profile": dict(profile), "claim": None,
                   "artifact": None, "receipt_ref": None}
        try:
            with self.records.begin() as uow:
                current = uow.read(_OPERATIONS, operation_id)
                effect, _created = self.effects.plan_in_connection(
                    uow.connection, self._intent(operation_id, source, profile),
                    now=self._epoch(),
                )
                if current is None:
                    uow.put(_OPERATIONS, operation_id, payload, expected_revision=0)
                    uow.commit()
                    return self._projection(payload, replayed=False)
                self._assert_identity(current.payload, source, profile)
                existing = dict(current.payload)
                receipt = uow.read(_RECEIPTS, operation_id)
                if receipt is not None and effect.state is EffectState.PLANNED:
                    effect = self.runner.begin_planned(
                        operation_id, now=self._epoch(), connection=uow.connection,
                    )
                    self.runner.settle_ok(
                        effect, connection=uow.connection,
                        receipt_ref=self._receipt_ref(operation_id),
                        receipt_kind="document-pdf-delivery-receipt", now=self._epoch(),
                    )
                uow.commit()
            return self._projection(self._resolved(operation_id, existing), replayed=True)
        except SQLiteUnitOfWorkConflict as exc:
            raise DocumentPdfDeliveryConflict("PDF operation concurrency conflict") from exc

    def projection(self, operation_id: str) -> dict[str, object] | None:
        record = self.records.read(_OPERATIONS, operation_id)
        if record is None:
            return None
        return self._projection(self._resolved(operation_id, record.payload), replayed=True)

    def source_delivery_id(self, operation_id: str) -> str | None:
        """Return the frozen source identity before revealing PDF state or bytes."""
        record = self.records.read(_OPERATIONS, operation_id)
        if record is None:
            return None
        source = record.payload.get("source")
        if not isinstance(source, Mapping) or not isinstance(source.get("html_delivery_id"), str):
            raise DocumentPdfDeliveryError("PDF source is invalid")
        return source["html_delivery_id"]

    def waiting(self, limit: int = 8) -> list[dict[str, object]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
            raise DocumentPdfDeliveryError("waiting limit is invalid")
        result = []
        for record in self.records.list(_OPERATIONS):
            payload = self._resolved(record.object_id, record.payload)
            if payload.get("status") == "waiting_for_electron":
                result.append(self._projection(payload, replayed=True))
        return result[:limit]

    def claim(self, operation_id: str, executor: Mapping[str, object]) -> dict[str, object]:
        try:
            with self.records.begin() as uow:
                record = uow.read(_OPERATIONS, operation_id)
                if record is None: raise DocumentPdfDeliveryError("PDF operation not found")
                payload = self._resolved_with_receipt(
                    operation_id, record.payload, uow.read(_RECEIPTS, operation_id),
                )
                self._executor(executor, payload.get("profile"))
                if payload.get("status") == "completed":
                    uow.commit()
                    return self._projection(payload, replayed=True)
                if payload.get("status") != "waiting_for_electron":
                    raise DocumentPdfDeliveryConflict("PDF operation is already claimed")
                token = uuid4().hex
                effect = self.runner.begin_planned(
                    operation_id, now=self._epoch(), connection=uow.connection,
                )
                if effect.state is not EffectState.INFLIGHT or effect.lease_owner != self.runner.owner_id:
                    raise DocumentPdfDeliveryConflict("PDF Effect is already claimed")
                uow.commit()
                self._claims[operation_id] = (token, dict(executor))
                payload["status"] = "claimed"
                result = self._projection(payload, replayed=False); result["claim_token"] = token
                return result
        except SQLiteUnitOfWorkConflict as exc: raise DocumentPdfDeliveryConflict("PDF operation concurrency conflict") from exc

    def print_input(self, operation_id: str, claim_token: str) -> dict[str, object]:
        payload = self._claimed(operation_id, claim_token)
        source = payload["source"]
        try:
            if payload["profile"]["profile_id"] == "builtin.a4-document":
                _name, html = self.deliveries.artifact(str(source["html_delivery_id"]), "html")
                print_html = html.decode("utf-8")
            else:
                print_html = self.deliveries.slide_html(str(source["html_delivery_id"]))
        except (DocumentDeliveryError, DocumentDeliveryConflict) as exc:
            raise DocumentPdfDeliveryConflict("frozen print input is unavailable") from exc
        return {"operation_id": operation_id, "profile": dict(payload["profile"]), "html": print_html}

    def complete(self, operation_id: str, claim_token: str, *, electron_version: str,
                 chrome_version: str, pdf_bytes: bytes) -> dict[str, object]:
        existing = self.records.read(_OPERATIONS, operation_id)
        if existing is not None:
            resolved = self._resolved(operation_id, existing.payload)
            if resolved.get("status") == "completed":
                return self._projection(resolved, replayed=True)
        payload = self._claimed(operation_id, claim_token)
        executor = {"electron_version": electron_version, "chrome_version": chrome_version}
        self._executor(executor, payload.get("profile"))
        if self._claims[operation_id][1] != executor:
            raise DocumentPdfDeliveryConflict("PDF renderer attestation drifted from claim")
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-") or not 5 <= len(pdf_bytes) <= _MAX_PDF_BYTES:
            raise DocumentPdfDeliveryError("PDF bytes are invalid")
        artifact = {"file_name": self._file_name(payload), "relative_path": f"{operation_id}/artifact.pdf",
                    "output_ref": f"crp://{self.namespace_id}/document-pdf-delivery/{operation_id}/artifact.pdf",
                    "byte_count": len(pdf_bytes), "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                    "verified": True,
                    "attestation": {"electron_version": electron_version, "chrome_version": chrome_version}}
        publication = self._publication(payload, artifact)
        try:
            with self.records.begin() as uow:
                retained = uow.read(_PUBLICATIONS, operation_id)
                if retained is None:
                    uow.put(_PUBLICATIONS, operation_id, publication, expected_revision=0)
                elif retained.payload != publication:
                    raise DocumentPdfDeliveryConflict("PDF publication fact drifted")
                uow.commit()
        except SQLiteUnitOfWorkConflict as exc:
            raise DocumentPdfDeliveryConflict("PDF publication fact conflict") from exc
        self._publish(self._path(artifact["relative_path"]), pdf_bytes)
        try:
            with self.records.begin() as uow:
                record = uow.read(_OPERATIONS, operation_id)
                if record is None: raise DocumentPdfDeliveryError("PDF operation not found")
                retained = uow.read(_RECEIPTS, operation_id)
                if retained is not None:
                    uow.commit()
                    completed = self._resolved_with_receipt(
                        operation_id, record.payload, retained,
                    )
                    return self._projection(completed, replayed=True)
                current = self._completed_payload(record.payload, artifact)
                receipt = self._receipt(current)
                uow.put(_RECEIPTS, operation_id, receipt, expected_revision=0)
                effect = self.effects.get_in_connection(uow.connection, operation_id)
                self.runner.settle_ok(
                    effect, connection=uow.connection,
                    receipt_ref=str(current["receipt_ref"]),
                    receipt_kind="document-pdf-delivery-receipt", now=self._epoch(),
                )
                uow.commit()
                self._claims.pop(operation_id, None)
                return self._projection(current, replayed=False)
        except SQLiteUnitOfWorkConflict as exc: raise DocumentPdfDeliveryConflict("PDF operation concurrency conflict") from exc

    def artifact(self, operation_id: str) -> tuple[str, bytes]:
        record = self.records.read(_OPERATIONS, operation_id)
        if record is None: raise DocumentPdfDeliveryError("PDF operation not found")
        payload = self._resolved(operation_id, record.payload)
        self._verify_completed(payload)
        artifact = payload["artifact"]
        path = self._path(str(artifact["relative_path"]))
        data = path.read_bytes() if path.is_file() else b""
        if (
            len(data) != artifact["byte_count"]
            or hashlib.sha256(data).hexdigest() != artifact.get("sha256")
            or not data.startswith(b"%PDF-")
        ):
            raise DocumentPdfDeliveryConflict("PDF artifact is unavailable")
        return str(artifact["file_name"]), data

    def verify_effect(self, operation_id: str) -> tuple[EffectState, str | None]:
        """Domain verification invoked only by Core Reaper for an expired Effect."""
        try:
            with self.records.begin() as uow:
                current = uow.read(_OPERATIONS, operation_id)
                if current is None:
                    uow.commit()
                    return EffectState.UNKNOWN, "document_pdf_operation_missing"
                receipt_record = uow.read(_RECEIPTS, operation_id)
                payload = self._resolved_with_receipt(
                    operation_id, current.payload, receipt_record,
                )
                if receipt_record is not None:
                    self.artifact_verify_only(payload)
                    uow.commit()
                    return EffectState.SETTLED_OK, self._receipt_ref(operation_id)
                publication = uow.read(_PUBLICATIONS, operation_id)
                artifact = None if publication is None else publication.payload.get("artifact")
                published = isinstance(artifact, Mapping) and self._published_artifact_matches(artifact)
                if published:
                    completed = self._completed_payload(payload, artifact)
                    receipt = self._receipt(completed)
                    uow.put(_RECEIPTS, operation_id, receipt, expected_revision=0)
                    uow.commit()
                    self._claims.pop(operation_id, None)
                    return EffectState.SETTLED_OK, self._receipt_ref(operation_id)
                uow.commit()
                self._claims.pop(operation_id, None)
                return EffectState.PLANNED, None
        except SQLiteUnitOfWorkConflict as exc:
            raise DocumentPdfDeliveryConflict("PDF verification concurrency conflict") from exc

    def _claimed(self, operation_id: str, token: str) -> Mapping[str, object]:
        record = self.records.read(_OPERATIONS, operation_id)
        if record is None: raise DocumentPdfDeliveryError("PDF operation not found")
        claim = self._claims.get(operation_id)
        if claim is None or claim[0] != token:
            raise DocumentPdfDeliveryConflict("PDF claim is invalid")
        effect = self.effects.get(operation_id)
        if effect.state is not EffectState.INFLIGHT or effect.lease_owner != self.runner.owner_id or effect.lease_expires_at is None or effect.lease_expires_at <= self._epoch():
            raise DocumentPdfDeliveryConflict("PDF Effect lease has expired")
        return record.payload
    def _epoch(self) -> int:
        return int(self._time().timestamp())
    def _time(self):
        value = self._now()
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    def _executor(self, value, profile):
        if not isinstance(value, Mapping) or set(value) != {"electron_version", "chrome_version"} or not all(isinstance(value.get(k), str) and value[k] for k in value): raise DocumentPdfDeliveryError("PDF executor attestation is invalid")
        try: electron_major = int(str(value["electron_version"]).split(".", 1)[0])
        except (TypeError, ValueError): raise DocumentPdfDeliveryError("PDF executor attestation is invalid")
        if not isinstance(profile, Mapping) or electron_major != profile.get("electron_major"):
            raise DocumentPdfDeliveryConflict("PDF renderer version is incompatible with frozen profile")
    def _intent(self, operation_id, source, profile):
        return EffectIntent(
            session_id=f"document-pdf:{source['document_id']}", root_id=str(source["html_delivery_id"]),
            step_key="electron-print-to-pdf", kind="document_pdf_delivery",
            effect_class=EffectClass.QUERYABLE,
            intent_ref=f"crp://{self.namespace_id}/document-pdf-intents/{operation_id}",
            gate_decision_id="document-delivery-gate:v1",
            rev_set={
                "document_revision": source["document_revision"],
                "profile_revision": profile["revision"],
                "renderer_revision": profile["renderer_revision"],
            },
            payload={"source": dict(source), "profile": dict(profile)},
            operation_id_override=operation_id,
        )
    def _assert_identity(self, payload, source, profile):
        if payload.get("source") != source or payload.get("profile") != profile: raise DocumentPdfDeliveryConflict("PDF operation identity conflicts")
    def _verify_completed(self, payload):
        if payload.get("status") != "completed": return
        receipt = self.records.read(_RECEIPTS, str(payload.get("id")))
        if receipt is None or receipt.payload != self._receipt(payload): raise DocumentPdfDeliveryConflict("PDF receipt is unavailable")
        self.artifact_verify_only(payload)

    def _receipt_ref(self, operation_id):
        return f"crp://{self.namespace_id}/document-pdf-receipts/{operation_id}"

    def _completed_payload(self, intent, artifact):
        payload = dict(intent)
        payload.update({
            "status": "completed", "claim": None, "artifact": dict(artifact),
            "receipt_ref": self._receipt_ref(str(intent["id"])),
        })
        return payload

    def _publication(self, intent, artifact):
        return {
            "schema_version": "1.0.0", "operation_id": intent["id"],
            "source": dict(intent["source"]), "profile": dict(intent["profile"]),
            "artifact": dict(artifact),
        }

    def _resolved(self, operation_id, intent):
        return self._resolved_with_receipt(
            operation_id, intent, self.records.read(_RECEIPTS, operation_id),
        )

    def _resolved_with_receipt(self, operation_id, intent, receipt_record):
        if intent.get("status") == "completed":
            if receipt_record is None or receipt_record.payload != self._receipt(intent):
                raise DocumentPdfDeliveryConflict("PDF receipt is unavailable")
            self.artifact_verify_only(intent)
            return dict(intent)
        if receipt_record is None:
            payload = dict(intent)
            if operation_id in self._claims:
                payload["status"] = "claimed"
            return payload
        receipt = receipt_record.payload
        completed = self._completed_payload(intent, receipt.get("artifact", {}))
        if receipt != self._receipt(completed):
            raise DocumentPdfDeliveryConflict("PDF receipt identity drifted")
        self.artifact_verify_only(completed)
        return completed
    def artifact_verify_only(self, payload):
        artifact = payload.get("artifact")
        if not isinstance(artifact, Mapping): raise DocumentPdfDeliveryConflict("PDF artifact is unavailable")
        path = self._path(str(artifact.get("relative_path", "")))
        if not path.is_file(): raise DocumentPdfDeliveryConflict("PDF artifact is unavailable")
        data = path.read_bytes()
        if path.stat().st_size != artifact.get("byte_count") or hashlib.sha256(data).hexdigest() != artifact.get("sha256") or not data.startswith(b"%PDF-"): raise DocumentPdfDeliveryConflict("PDF artifact is unavailable")
    def _receipt(self, payload): return {"schema_version": "1.0.0", "id": payload["id"], "operation_id": payload["id"], "status": "completed", "source": dict(payload["source"]), "profile": dict(payload["profile"]), "artifact": dict(payload["artifact"])}
    def _projection(self, payload, *, replayed):
        return {"operation_id": payload["id"], "status": payload["status"], "html_delivery_id": payload["source"]["html_delivery_id"], "profile": dict(payload["profile"]), "artifact": None if payload.get("artifact") is None else {k: payload["artifact"][k] for k in ("file_name", "byte_count", "output_ref", "verified")}, "receipt_ref": payload.get("receipt_ref"), "replayed": replayed}
    def _file_name(self, payload): return f"document-{payload['id']}.pdf"
    def _published_artifact_matches(self, artifact):
        try:
            path = self._path(str(artifact["relative_path"]))
            if not path.is_file() or path.stat().st_size != artifact["byte_count"]: return False
            data = path.read_bytes()
            return data.startswith(b"%PDF-") and hashlib.sha256(data).hexdigest() == artifact["sha256"]
        except (KeyError, OSError, TypeError, DocumentPdfDeliveryError): return False
    def _path(self, relative):
        root = (self.runtime_root / "exports" / "document-pdf-delivery").resolve(); path = (root / relative).resolve()
        if root not in path.parents: raise DocumentPdfDeliveryError("PDF artifact path escaped root")
        return path
    def _publish(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            if path.read_bytes() == data: return
            raise DocumentPdfDeliveryConflict("PDF artifact bytes drifted")
        temporary = path.with_name(f".{os.getpid()}-{uuid4().hex[:8]}.part")
        try:
            with temporary.open("xb") as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally: temporary.unlink(missing_ok=True)
