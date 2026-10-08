"""Durable delivery of one frozen canonical Document revision.

The Delivery record is an operation projection, not another Document authority.
It stores only frozen identities and artifact metadata; Markdown remains owned by
the canonical Document repository and is read by exact revision during recovery.
"""

from __future__ import annotations

import os
import re
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from core.effect_log import (
    EffectClass, EffectIntent, EffectLog, EffectRunner, EffectState,
    shared_effect_runner,
)
from core.storage_provider import (
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
)

from .document_html_render import DocumentHtmlRenderError, DocumentHtmlRenderer
from .document_docx_render import DocumentDocxRenderError, DocumentDocxRenderer
from .document_pptx_render import DocumentPptxRenderError, DocumentPptxRenderer


_DELIVERIES = "document_deliveries"
_RECEIPTS = "document_delivery_receipts"
_FORMATS = ("markdown", "html", "docx", "pptx")
_DEFAULT_FORMATS = ("markdown", "html")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DELIVERY_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_STORE_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,79}$")
_STYLE_DELIVERY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,23}$")
_DELIVERY_NAMESPACE = UUID("55f08bf9-62aa-4f5b-8ae6-9bb5e4719ed9")
_STYLE_SNAPSHOT = {
    "profile_id": "builtin.porcelain-document",
    "revision": 1,
    "renderer_id": "rebuild.document-html",
    "renderer_revision": 1,
    "delivery_key": "porcelain-html-v1",
}


class DocumentDeliveryError(ValueError):
    """Raised when a delivery violates its frozen contract."""


class DocumentDeliveryConflict(DocumentDeliveryError):
    """Raised when current or persisted authority conflicts with the request."""


class DocumentRepositoryPort(Protocol):
    def read(self, document_id: str) -> Mapping[str, object] | None: ...
    def revision(self, document_id: str, revision: int) -> Mapping[str, object] | None: ...
    def markdown(self, document_id: str, *, revision: int | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DocumentDeliveryStyleRegistry:
    """Resolve immutable renderer revisions retained for durable recovery."""

    current_snapshot: Mapping[str, object] = field(
        default_factory=lambda: dict(_STYLE_SNAPSHOT)
    )
    renderers: Mapping[tuple[str, int, str, int], DocumentHtmlRenderer] = field(
        default_factory=lambda: {_style_key(_STYLE_SNAPSHOT): DocumentHtmlRenderer()}
    )
    delivery_keys: Mapping[tuple[str, int, str, int], str] = field(
        default_factory=lambda: {
            _style_key(_STYLE_SNAPSHOT): _text(_STYLE_SNAPSHOT, "delivery_key")
        }
    )
    current_docx_renderer: tuple[str, int] = ("rebuild.document-docx", 1)
    docx_renderers: Mapping[
        tuple[str, int, str, int, str, int], DocumentDocxRenderer
    ] = field(
        default_factory=lambda: {
            (*_style_key(_STYLE_SNAPSHOT), "rebuild.document-docx", 1):
                DocumentDocxRenderer()
        }
    )
    docx_delivery_keys: Mapping[tuple[str, int, str, int, str, int], str] = field(
        default_factory=lambda: {
            (*_style_key(_STYLE_SNAPSHOT), "rebuild.document-docx", 1): "docx-v1"
        }
    )
    current_pptx_renderer: tuple[str, int] = ("rebuild.document-pptx", 2)
    pptx_renderers: Mapping[
        tuple[str, int, str, int, str, int], DocumentPptxRenderer
    ] = field(default_factory=lambda: {
        (*_style_key(_STYLE_SNAPSHOT), "rebuild.document-pptx", 1): DocumentPptxRenderer(),
        (*_style_key(_STYLE_SNAPSHOT), "rebuild.document-pptx", 2): DocumentPptxRenderer(),
    })
    pptx_delivery_keys: Mapping[tuple[str, int, str, int, str, int], str] = field(
        default_factory=lambda: {
        (*_style_key(_STYLE_SNAPSHOT), "rebuild.document-pptx", 1): "pptx-v1",
        (*_style_key(_STYLE_SNAPSHOT), "rebuild.document-pptx", 2): "pptx-v2",
        }
    )

    def __post_init__(self) -> None:
        if set(self.renderers) != set(self.delivery_keys):
            raise DocumentDeliveryError("document delivery style registry is incomplete")
        aliases = []
        for snapshot_key, alias in self.delivery_keys.items():
            if len(snapshot_key) != 4:
                raise DocumentDeliveryError("document delivery style registry key is invalid")
            if not _STYLE_DELIVERY_KEY.fullmatch(alias):
                raise DocumentDeliveryError("document delivery style key is invalid")
            aliases.append(alias)
        if len(aliases) != len(set(aliases)):
            raise DocumentDeliveryError("document delivery style keys must be unique")
        if set(self.docx_renderers) != set(self.docx_delivery_keys):
            raise DocumentDeliveryError("document delivery DOCX registry is incomplete")
        if any(len(key) != 6 for key in self.docx_renderers):
            raise DocumentDeliveryError("document delivery DOCX registry key is invalid")
        docx_aliases = tuple(self.docx_delivery_keys.values())
        if (
            any(not _STYLE_DELIVERY_KEY.fullmatch(alias) for alias in docx_aliases)
            or len(docx_aliases) != len(set(docx_aliases))
        ):
            raise DocumentDeliveryError("document delivery DOCX keys must be unique")
        docx_id, docx_revision = self.current_docx_renderer
        _identity(docx_id, "current DOCX renderer_id")
        _positive_int(docx_revision, "current DOCX renderer revision")
        if set(self.pptx_renderers) != set(self.pptx_delivery_keys):
            raise DocumentDeliveryError("document delivery PPTX registry is incomplete")
        if any(len(key) != 6 for key in self.pptx_renderers):
            raise DocumentDeliveryError("document delivery PPTX registry key is invalid")
        pptx_aliases = tuple(self.pptx_delivery_keys.values())
        if (
            any(not _STYLE_DELIVERY_KEY.fullmatch(alias) for alias in pptx_aliases)
            or len(pptx_aliases) != len(set(pptx_aliases))
        ):
            raise DocumentDeliveryError("document delivery PPTX keys must be unique")
        pptx_id, pptx_revision = self.current_pptx_renderer
        _identity(pptx_id, "current PPTX renderer_id")
        _positive_int(pptx_revision, "current PPTX renderer revision")
        self.resolve(self.current_snapshot)

    def current(self, *, include_docx: bool = False, include_pptx: bool = False) -> dict[str, object]:
        snapshot = dict(self.current_snapshot)
        self.resolve(snapshot)
        if include_docx:
            docx_id, docx_revision = self.current_docx_renderer
            if (*_style_key(snapshot), docx_id, docx_revision) not in self.docx_renderers:
                raise DocumentDeliveryConflict(
                    "current document delivery DOCX renderer is unavailable"
                )
            snapshot.update({
                "docx_renderer_id": docx_id,
                "docx_renderer_revision": docx_revision,
                "docx_delivery_key": self.docx_delivery_keys[
                    (*_style_key(snapshot), docx_id, docx_revision)
                ],
            })
        if include_pptx:
            pptx_id, pptx_revision = self.current_pptx_renderer
            key = (*_style_key(snapshot), pptx_id, pptx_revision)
            if key not in self.pptx_renderers:
                raise DocumentDeliveryConflict(
                    "current document delivery PPTX renderer is unavailable"
                )
            snapshot.update({
                "pptx_renderer_id": pptx_id,
                "pptx_renderer_revision": pptx_revision,
                "pptx_delivery_key": self.pptx_delivery_keys[key],
            })
        return snapshot

    def resolve(self, snapshot: Mapping[str, object]) -> DocumentHtmlRenderer:
        _validate_style_snapshot(snapshot)
        renderer = self.renderers.get(_style_key(snapshot))
        if renderer is None:
            raise DocumentDeliveryConflict(
                "frozen document delivery renderer revision is unavailable"
            )
        if snapshot.get("delivery_key") != self.delivery_keys.get(_style_key(snapshot)):
            raise DocumentDeliveryConflict("frozen document delivery style key drifted")
        return renderer

    def resolve_docx(self, snapshot: Mapping[str, object]) -> DocumentDocxRenderer:
        _validate_style_snapshot(snapshot)
        key = (
            *_style_key(snapshot),
            _text(snapshot, "docx_renderer_id"),
            _positive_int(
                snapshot.get("docx_renderer_revision"), "DOCX renderer revision"
            ),
        )
        if snapshot.get("docx_delivery_key") != self.docx_delivery_keys.get(key):
            raise DocumentDeliveryConflict("frozen document delivery DOCX key drifted")
        renderer = self.docx_renderers.get(key)
        if renderer is None:
            raise DocumentDeliveryConflict(
                "frozen document delivery DOCX renderer is unavailable"
            )
        return renderer

    def resolve_pptx(self, snapshot: Mapping[str, object]) -> DocumentPptxRenderer:
        _validate_style_snapshot(snapshot)
        key = (
            *_style_key(snapshot),
            _text(snapshot, "pptx_renderer_id"),
            _positive_int(snapshot.get("pptx_renderer_revision"), "PPTX renderer revision"),
        )
        if snapshot.get("pptx_delivery_key") != self.pptx_delivery_keys.get(key):
            raise DocumentDeliveryConflict("frozen document delivery PPTX key drifted")
        renderer = self.pptx_renderers.get(key)
        if renderer is None:
            raise DocumentDeliveryConflict(
                "frozen document delivery PPTX renderer is unavailable"
            )
        return renderer


@dataclass(frozen=True, slots=True)
class DocumentDeliveryService:
    records: SQLiteStructuredRecordStore
    documents: DocumentRepositoryPort
    runtime_root: Path
    namespace_id: str = "default"
    style_registry: DocumentDeliveryStyleRegistry = field(
        default_factory=DocumentDeliveryStyleRegistry
    )
    effect_log: EffectLog | None = None
    effect_runner: EffectRunner | None = None
    effect_owner: str = "document-delivery-renderer"

    def __post_init__(self) -> None:
        log = (
            self.effect_runner.log
            if self.effect_runner is not None
            else self.effect_log or EffectLog(self.records.database_path)
        )
        object.__setattr__(self, "effect_log", log)
        if self.effect_runner is None:
            object.__setattr__(
                self, "effect_runner",
                shared_effect_runner(
                    log.database, owner_role=self.effect_owner, lease_seconds=300,
                ),
            )

    def create_or_resume(
        self,
        *,
        document_id: str,
        expected_document_revision: int,
        formats: Sequence[str] = _DEFAULT_FORMATS,
    ) -> Mapping[str, object]:
        clean_id = _identity(document_id, "document_id")
        clean_revision = _positive_int(expected_document_revision, "expected_document_revision")
        clean_formats = _formats(formats)
        current_style = self.style_registry.current(
            include_docx="docx" in clean_formats,
            include_pptx="pptx" in clean_formats,
        )
        delivery_id = _delivery_id(clean_id, clean_revision, clean_formats, current_style)
        existing = self.records.read(_DELIVERIES, delivery_id)
        if existing is None:
            legacy_id = _legacy_delivery_id(
                clean_id, clean_revision, clean_formats, current_style
            )
            legacy = (
                self.records.read(_DELIVERIES, legacy_id)
                if legacy_id is not None else None
            )
            if legacy is not None:
                self._assert_identity(
                    legacy.payload, clean_id, clean_revision, clean_formats
                )
                self._ensure_effect(legacy.object_id, legacy.payload)
                return self.resume(legacy.object_id)
        if existing is None:
            frozen = self._freeze(
                clean_id, clean_revision, clean_formats, delivery_id, current_style
            )
            try:
                with self.records.begin() as uow:
                    self._effects.plan_in_connection(
                        uow.connection, self._intent(delivery_id, frozen), now=int(time.time()),
                    )
                    uow.put(_DELIVERIES, delivery_id, frozen, expected_revision=0)
                    uow.commit()
            except SQLiteUnitOfWorkConflict:
                winner = self.records.read(_DELIVERIES, delivery_id)
                if winner is None:
                    raise DocumentDeliveryConflict(
                        "document delivery creation winner disappeared"
                    )
                self._assert_identity(
                    winner.payload, clean_id, clean_revision, clean_formats
                )
        else:
            self._assert_identity(existing.payload, clean_id, clean_revision, clean_formats)
            self._ensure_effect(delivery_id, existing.payload)
        return self.resume(delivery_id)

    def resume(self, delivery_id: str) -> Mapping[str, object]:
        clean_delivery_id = _delivery_identity(delivery_id)
        record = self.records.read(_DELIVERIES, clean_delivery_id)
        if record is None:
            raise DocumentDeliveryError("document delivery not found")
        payload = self._resolved_payload(clean_delivery_id, record.payload)
        if payload["status"] == "completed":
            self._verify_artifacts(payload)
            return _public_projection(payload, replayed=True)

        now = int(time.time())
        handled = False

        def handler(effect) -> str:
            nonlocal handled
            handled = True
            return self.handle_effect(effect)

        outcome = self._runner.execute_planned(
            clean_delivery_id,
            handler,
            now=now,
            receipt_kind="document-delivery-receipt",
        )
        winner = self.records.read(_DELIVERIES, clean_delivery_id)
        if winner is None:
            raise DocumentDeliveryConflict("document delivery Effect winner disappeared")
        if outcome.state is EffectState.SETTLED_OK:
            resolved = self._resolved_payload(clean_delivery_id, winner.payload)
            self._verify_artifacts(resolved)
        else:
            resolved = self._resolved_payload(clean_delivery_id, winner.payload)
        return _public_projection(resolved, replayed=not handled)

    def handle_effect(self, effect) -> str:
        """Execute one Core-claimed delivery and persist its immutable Receipt."""

        clean_delivery_id = _delivery_identity(effect.operation_id)
        if effect.kind != "document_delivery":
            raise DocumentDeliveryConflict("document delivery Effect kind drifted")
        record = self.records.read(_DELIVERIES, clean_delivery_id)
        if record is None:
            raise DocumentDeliveryError("document delivery not found")
        payload = self._resolved_payload(clean_delivery_id, record.payload)
        if payload["status"] == "completed":
            self._verify_artifacts(payload)
            return str(payload["receipt_ref"])

        document = _mapping(payload, "document_snapshot")
        document_id = _text(document, "document_id")
        revision = _positive_int(document.get("revision"), "document revision")
        markdown = self.documents.markdown(document_id, revision=revision)
        revision_record = self.documents.revision(document_id, revision)
        if markdown is None or revision_record is None:
            raise DocumentDeliveryConflict("frozen document revision is unavailable")
        if revision_record.get("new_content_hash") != document.get("content_hash"):
            raise DocumentDeliveryConflict("frozen document revision content hash drifted")

        artifact_bytes = self._render(payload, markdown)
        artifacts = []
        for format_name in _string_list(payload.get("formats"), "formats"):
            data = artifact_bytes[format_name]
            relative_path = _relative_artifact_path(payload, format_name)
            path = self._artifact_path(relative_path)
            _publish_exact(path, data)
            artifacts.append({
                "format": format_name,
                "file_name": _artifact_file_name(payload, format_name),
                "relative_path": relative_path,
                "byte_count": len(data),
                "output_ref": (
                    f"crp://{self.namespace_id}/document-deliveries/"
                    f"{clean_delivery_id}/{format_name}"
                ),
                "verified": True,
            })

        receipt_ref = (
            f"crp://{self.namespace_id}/document-delivery-receipts/{clean_delivery_id}"
        )
        completed = dict(payload)
        completed.update({
            "status": "completed",
            "checkpoint": None,
            "artifacts": artifacts,
            "receipt_ref": receipt_ref,
        })
        receipt = _receipt_payload(completed)
        try:
            with self.records.begin() as uow:
                uow.put(
                    _RECEIPTS,
                    clean_delivery_id,
                    receipt,
                    expected_revision=0,
                )
                uow.commit()
        except SQLiteUnitOfWorkConflict:
            winner = self._resolved_payload(clean_delivery_id, record.payload)
            self._verify_artifacts(winner)
            return str(winner["receipt_ref"])
        self._verify_receipt(completed)
        return receipt_ref

    def verify_effect(self, delivery_id: str) -> tuple[EffectState, str | None]:
        """Read retained delivery facts for the Core Reaper."""
        clean_delivery_id = _delivery_identity(delivery_id)
        record = self.records.read(_DELIVERIES, clean_delivery_id)
        if record is None:
            return EffectState.UNKNOWN, "document_delivery_missing"
        payload = self._resolved_payload(clean_delivery_id, record.payload)
        if payload.get("status") == "completed":
            self._verify_artifacts(payload)
            return EffectState.SETTLED_OK, str(payload["receipt_ref"])
        return EffectState.PLANNED, None

    @property
    def _effects(self) -> EffectLog:
        assert self.effect_log is not None
        return self.effect_log

    @property
    def _runner(self) -> EffectRunner:
        assert self.effect_runner is not None
        return self.effect_runner

    def _ensure_effect(self, delivery_id: str, payload: Mapping[str, object]) -> None:
        now = int(time.time())
        completed = self._resolved_payload(delivery_id, payload)
        with self.records.begin() as uow:
            effect, _created = self._effects.plan_in_connection(
                uow.connection, self._intent(delivery_id, payload), now=now,
            )
            if completed.get("status") == "completed" and effect.state is EffectState.PLANNED:
                effect = self._runner.begin_planned(
                    delivery_id, now=now, connection=uow.connection,
                )
                self._runner.settle_ok(
                    effect, connection=uow.connection,
                    receipt_ref=str(completed["receipt_ref"]),
                    receipt_kind="document-delivery-receipt", now=now,
                )
            uow.commit()

    def _intent(self, delivery_id: str, payload: Mapping[str, object]) -> EffectIntent:
        snapshot = _mapping(payload, "document_snapshot")
        style = _mapping(payload, "style_snapshot")
        return EffectIntent(
            session_id=f"document-delivery:{snapshot['document_id']}",
            root_id=str(snapshot["document_id"]), step_key="render-and-publish",
            kind="document_delivery", effect_class=EffectClass.IDEMPOTENT,
            intent_ref=f"crp://{self.namespace_id}/document-delivery-intents/{delivery_id}",
            gate_decision_id="document-delivery-gate:v1",
            rev_set={
                "document_revision": snapshot["revision"],
                "style_revision": style["revision"],
                "renderer_revision": style["renderer_revision"],
            },
            payload={
                "document_snapshot": dict(snapshot), "style_snapshot": dict(style),
                "formats": list(payload["formats"]),
            },
            operation_id_override=delivery_id,
        )

    def projection(self, delivery_id: str) -> Mapping[str, object] | None:
        clean_delivery_id = _delivery_identity(delivery_id)
        record = self.records.read(_DELIVERIES, clean_delivery_id)
        if record is None:
            return None
        payload = self._resolved_payload(clean_delivery_id, record.payload)
        if payload.get("status") == "completed":
            self._verify_artifacts(payload)
        return _public_projection(payload, replayed=True)

    def document_scope(self, delivery_id: str) -> Mapping[str, object] | None:
        """Read the frozen owner before resolving or serving any artifact."""
        record = self.records.read(_DELIVERIES, _delivery_identity(delivery_id))
        if record is None:
            return None
        snapshot = record.payload.get("document_snapshot")
        if not isinstance(snapshot, Mapping):
            raise DocumentDeliveryError("document delivery snapshot is invalid")
        return {
            "document_id": snapshot.get("document_id"),
            "project_id": snapshot.get("project_id"),
        }

    def artifact(self, delivery_id: str, format_name: str) -> tuple[str, bytes]:
        record = self.records.read(_DELIVERIES, _delivery_identity(delivery_id))
        if record is None:
            raise DocumentDeliveryError("document delivery not found")
        payload = self._resolved_payload(_delivery_identity(delivery_id), record.payload)
        if payload.get("status") != "completed":
            raise DocumentDeliveryConflict("document delivery is not completed")
        self._verify_artifacts(payload)
        for artifact in _mapping_list(payload.get("artifacts"), "artifacts"):
            if artifact.get("format") == format_name and artifact.get("verified") is True:
                path = self._artifact_path(_text(artifact, "relative_path"))
                data = path.read_bytes() if path.is_file() else None
                if data is None or len(data) != artifact.get("byte_count"):
                    raise DocumentDeliveryConflict("document delivery artifact is unavailable")
                return _text(artifact, "file_name"), data
        raise DocumentDeliveryError("document delivery artifact format is unavailable")

    def slide_html(self, delivery_id: str) -> str:
        """Regenerate safe inline slide print input from one completed frozen PPTX delivery."""

        record = self.records.read(_DELIVERIES, _delivery_identity(delivery_id))
        if record is None:
            raise DocumentDeliveryError("document delivery not found")
        payload = self._resolved_payload(_delivery_identity(delivery_id), record.payload)
        if payload.get("status") != "completed" or "pptx" not in payload.get("formats", ()):
            raise DocumentDeliveryConflict("verified PPTX delivery is required")
        if payload.get("pptx_layout_plan") is None:
            raise DocumentDeliveryConflict("frozen PPTX layout plan is required")
        self._verify_artifacts(payload)
        snapshot = _mapping(payload, "document_snapshot")
        markdown = self.documents.markdown(
            _text(snapshot, "document_id"),
            revision=_positive_int(snapshot.get("revision"), "document revision"),
        )
        if markdown is None:
            raise DocumentDeliveryConflict("frozen document revision is unavailable")
        try:
            return self.style_registry.resolve_pptx(
            _mapping(payload, "style_snapshot")
        ).render(
                {
                    "id": snapshot["document_id"],
                    "revision": snapshot["revision"],
                    "title": snapshot["title"],
                    "status": "delivered",
                },
                markdown,
                layout_plan=self._pptx_layout_plan(payload, markdown),
            ).slide_html
        except DocumentPptxRenderError as exc:
            raise DocumentDeliveryError(str(exc)) from exc

    def _freeze(
        self,
        document_id: str,
        revision: int,
        formats: tuple[str, ...],
        delivery_id: str,
        style_snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        current = self.documents.read(document_id)
        if current is None:
            raise DocumentDeliveryError("document not found")
        if current.get("revision") != revision:
            raise DocumentDeliveryConflict("document revision conflict")
        markdown = self.documents.markdown(document_id, revision=revision)
        revision_record = self.documents.revision(document_id, revision)
        if markdown is None or revision_record is None:
            raise DocumentDeliveryConflict("document revision is incomplete")
        content_hash = revision_record.get("new_content_hash")
        if not isinstance(content_hash, str) or content_hash != current.get("content_hash"):
            raise DocumentDeliveryConflict("document revision authority drifted")
        title = current.get("title")
        if not isinstance(title, str) or not title.strip():
            raise DocumentDeliveryError("document title is invalid")
        frozen = {
            "schema_version": "1.0.0",
            "id": delivery_id,
            "status": "prepared",
            "document_snapshot": {
                "document_id": document_id,
                "revision": revision,
                "content_hash": content_hash,
                "title": title.strip(),
                "project_id": current.get("project_id"),
                "source_snapshot": current.get("source_snapshot"),
                "source_refs": current.get("source_refs", []),
            },
            "style_snapshot": dict(style_snapshot),
            "formats": list(formats),
            "artifacts": [],
            "checkpoint": {"next_step": "render_artifacts"},
            "receipt_ref": None,
        }
        if "pptx" in formats and style_snapshot.get("pptx_renderer_revision", 1) >= 2:
            try:
                renderer = self.style_registry.resolve_pptx(style_snapshot)
                plan_payload = renderer.plan(
                    {"id": document_id, "revision": revision, "title": title.strip(), "status": "delivered"},
                    markdown,
                ).payload()
                plan_data = json.dumps(
                    plan_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                relative_path = (
                    f"{delivery_id}/pptx-{style_snapshot['pptx_delivery_key']}/"
                    "layout-plan.json"
                )
                _publish_exact(self._artifact_path(relative_path), plan_data)
                frozen["pptx_layout_plan"] = {
                    "schema_version": "1.0.0",
                    "relative_path": relative_path,
                    "byte_count": len(plan_data),
                }
            except (AttributeError, DocumentPptxRenderError) as exc:
                raise DocumentDeliveryError(str(exc)) from exc
        return frozen

    def _assert_identity(
        self,
        payload: Mapping[str, object],
        document_id: str,
        revision: int,
        formats: Sequence[str],
    ) -> None:
        _validate_record(payload)
        snapshot = _mapping(payload, "document_snapshot")
        if (
            snapshot.get("document_id") != document_id
            or snapshot.get("revision") != revision
            or tuple(payload.get("formats", ())) != tuple(formats)
            or payload.get("style_snapshot") != self.style_registry.current(
                include_docx="docx" in formats,
                include_pptx="pptx" in formats,
            )
        ):
            raise DocumentDeliveryConflict("document delivery identity conflicts")

    def _render(self, payload: Mapping[str, object], markdown: str) -> dict[str, bytes]:
        snapshot = _mapping(payload, "document_snapshot")
        result = {"markdown": markdown.encode("utf-8")}
        if "html" in payload.get("formats", ()):
            try:
                renderer = self.style_registry.resolve(
                    _mapping(payload, "style_snapshot")
                )
                html = renderer.render(
                    {
                        "id": snapshot["document_id"],
                        "revision": snapshot["revision"],
                        "title": snapshot["title"],
                        "status": "delivered",
                    },
                    markdown,
                ).html
            except DocumentHtmlRenderError as exc:
                raise DocumentDeliveryError(str(exc)) from exc
            result["html"] = html.encode("utf-8")
        if "docx" in payload.get("formats", ()):
            try:
                docx = self.style_registry.resolve_docx(
                    _mapping(payload, "style_snapshot")
                ).render(
                    {
                        "id": snapshot["document_id"],
                        "revision": snapshot["revision"],
                        "title": snapshot["title"],
                        "status": "delivered",
                    },
                    markdown,
                ).content
            except DocumentDocxRenderError as exc:
                raise DocumentDeliveryError(str(exc)) from exc
            result["docx"] = docx
        if "pptx" in payload.get("formats", ()):
            try:
                pptx = self.style_registry.resolve_pptx(
                    _mapping(payload, "style_snapshot")
                ).render(
                    {
                        "id": snapshot["document_id"],
                        "revision": snapshot["revision"],
                        "title": snapshot["title"],
                        "status": "delivered",
                    },
                    markdown,
                    layout_plan=self._pptx_layout_plan(payload, markdown),
                ).content
            except DocumentPptxRenderError as exc:
                raise DocumentDeliveryError(str(exc)) from exc
            result["pptx"] = pptx
        return result

    def _pptx_layout_plan(self, payload: Mapping[str, object], markdown: str):
        descriptor = payload.get("pptx_layout_plan")
        if descriptor is None:
            return None
        if not isinstance(descriptor, Mapping):
            raise DocumentDeliveryConflict("frozen PPTX layout plan is invalid")
        try:
            from .document_pptx_render import SlideLayoutPlan
            path = self._artifact_path(_text(descriptor, "relative_path"))
            data = path.read_bytes() if path.is_file() else b""
            if len(data) != descriptor.get("byte_count"):
                raise DocumentDeliveryConflict("frozen PPTX layout plan is unavailable")
            decoded = json.loads(data.decode("utf-8"))
            if not isinstance(decoded, Mapping):
                raise DocumentDeliveryConflict("frozen PPTX layout plan is invalid")
            plan = SlideLayoutPlan.from_payload(decoded)
            snapshot = _mapping(payload, "document_snapshot")
            expected = self.style_registry.resolve_pptx(
                _mapping(payload, "style_snapshot")
            ).plan(
                {
                    "id": snapshot["document_id"],
                    "revision": snapshot["revision"],
                    "title": snapshot["title"],
                    "status": "delivered",
                },
                markdown,
            )
            if plan.payload() != expected.payload():
                raise DocumentDeliveryConflict("frozen PPTX layout plan drifted")
            return plan
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, DocumentPptxRenderError) as exc:
            raise DocumentDeliveryConflict(str(exc)) from exc

    def _verify_artifacts(self, payload: Mapping[str, object]) -> None:
        artifacts = _mapping_list(payload.get("artifacts"), "artifacts")
        if len(artifacts) != len(payload.get("formats", ())):
            raise DocumentDeliveryConflict("document delivery artifact set is incomplete")
        snapshot = _mapping(payload, "document_snapshot")
        document_id = _text(snapshot, "document_id")
        revision = _positive_int(snapshot.get("revision"), "document revision")
        markdown = self.documents.markdown(document_id, revision=revision)
        revision_record = self.documents.revision(document_id, revision)
        if markdown is None or revision_record is None:
            raise DocumentDeliveryConflict("frozen document revision is unavailable")
        if revision_record.get("new_content_hash") != snapshot.get("content_hash"):
            raise DocumentDeliveryConflict("frozen document revision content hash drifted")
        expected = self._render(payload, markdown)
        for artifact in artifacts:
            if artifact.get("verified") is not True:
                raise DocumentDeliveryConflict("document delivery artifact is unverified")
            path = self._artifact_path(_text(artifact, "relative_path"))
            format_name = _text(artifact, "format")
            if (
                format_name not in expected
                or not path.is_file()
                or path.read_bytes() != expected[format_name]
                or path.stat().st_size != artifact.get("byte_count")
            ):
                raise DocumentDeliveryConflict("document delivery artifact is unavailable")

    def _verify_receipt(self, payload: Mapping[str, object]) -> None:
        receipt_ref = payload.get("receipt_ref")
        if not isinstance(receipt_ref, str) or not receipt_ref.startswith("crp://"):
            raise DocumentDeliveryConflict("document delivery receipt reference is invalid")
        receipt = self.records.read(_RECEIPTS, _text(payload, "id"))
        if receipt is None or receipt.payload != _receipt_payload(payload):
            raise DocumentDeliveryConflict("document delivery receipt is unavailable")

    def _resolved_payload(
        self,
        delivery_id: str,
        intent_payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Project terminal data from immutable Receipt, with legacy read compatibility."""

        _validate_record(intent_payload)
        if intent_payload.get("status") == "completed":
            legacy = dict(intent_payload)
            self._verify_receipt(legacy)
            return legacy
        receipt = self.records.read(_RECEIPTS, delivery_id)
        if receipt is None:
            return dict(intent_payload)
        candidate = dict(intent_payload)
        receipt_payload = receipt.payload
        if (
            receipt_payload.get("delivery_id") != delivery_id
            or receipt_payload.get("document_snapshot") != intent_payload.get("document_snapshot")
            or receipt_payload.get("style_snapshot") != intent_payload.get("style_snapshot")
            or receipt_payload.get("formats") != intent_payload.get("formats")
        ):
            raise DocumentDeliveryConflict("document delivery receipt identity drifted")
        candidate.update({
            "status": "completed",
            "checkpoint": None,
            "artifacts": [
                dict(item)
                for item in _mapping_list(receipt_payload.get("artifacts"), "artifacts")
            ],
            "receipt_ref": (
                f"crp://{self.namespace_id}/document-delivery-receipts/{delivery_id}"
            ),
        })
        if "pptx_layout_plan" in receipt_payload:
            candidate["pptx_layout_plan"] = dict(
                _mapping(receipt_payload, "pptx_layout_plan")
            )
        self._verify_receipt(candidate)
        return candidate

    def _artifact_path(self, relative_path: str) -> Path:
        root = (self.runtime_root / "exports" / "document-delivery").resolve()
        path = (root / relative_path).resolve()
        if path == root or root not in path.parents:
            raise DocumentDeliveryError("document delivery artifact path escaped root")
        return path


def _delivery_id(
    document_id: str,
    revision: int,
    formats: Sequence[str],
    style_snapshot: Mapping[str, object],
) -> str:
    identity = json.dumps(
        {
            "document_id": document_id,
            "document_revision": revision,
            "formats": list(formats),
            "style_snapshot": dict(style_snapshot),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"dd-{uuid5(_DELIVERY_NAMESPACE, identity)}"


def _legacy_delivery_id(
    document_id: str,
    revision: int,
    formats: Sequence[str],
    style_snapshot: Mapping[str, object],
) -> str | None:
    """Resolve the last pre-UUID HTML/Markdown identity for zero-copy replay."""

    if "docx" in formats or "pptx" in formats:
        return None
    format_codes = {"markdown": "m", "html": "h"}
    candidate = (
        f"dd-{document_id}-r{revision}-"
        f"f{''.join(format_codes[item] for item in formats)}-"
        f"s{_text(style_snapshot, 'delivery_key')}"
    )
    try:
        return _delivery_identity(candidate)
    except DocumentDeliveryError:
        return None


def _relative_artifact_path(payload: Mapping[str, object], format_name: str) -> str:
    snapshot = _mapping(payload, "document_snapshot")
    style = _mapping(payload, "style_snapshot")
    extension = {"markdown": "md", "html": "html", "docx": "docx", "pptx": "pptx"}[format_name]
    format_segment = ""
    if format_name == "docx":
        format_segment = f"docx-{style['docx_delivery_key']}/"
    elif format_name == "pptx":
        format_segment = f"pptx-{style['pptx_delivery_key']}/"
    return (
        f"{_text(payload, 'id')}/{format_segment}"
        f"artifact.{extension}"
    )


def _artifact_file_name(payload: Mapping[str, object], format_name: str) -> str:
    snapshot = _mapping(payload, "document_snapshot")
    extension = {"markdown": "md", "html": "html", "docx": "docx", "pptx": "pptx"}[format_name]
    document_id = _text(snapshot, "document_id")
    safe_id = document_id if _STORE_FILE_NAME.fullmatch(document_id) else "document"
    return f"{safe_id}-r{snapshot['revision']}.{extension}"


def _publish_exact(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        existing = path.read_bytes()
        if existing == data:
            return
        raise DocumentDeliveryConflict("document delivery artifact bytes drifted")
    temporary = path.with_name(f".{os.getpid()}-{uuid4().hex[:8]}.part")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _public_projection(payload: Mapping[str, object], *, replayed: bool) -> dict[str, object]:
    snapshot = _mapping(payload, "document_snapshot")
    artifacts = [
        {
            "format": artifact["format"],
            "file_name": artifact["file_name"],
            "byte_count": artifact["byte_count"],
            "output_ref": artifact["output_ref"],
            "verified": artifact["verified"],
        }
        for artifact in _mapping_list(payload.get("artifacts"), "artifacts")
    ]
    return {
        "schema_version": payload["schema_version"],
        "delivery_id": payload["id"],
        "status": payload["status"],
        "document_id": snapshot["document_id"],
        "document_revision": snapshot["revision"],
        "content_hash": snapshot["content_hash"],
        "style_snapshot": dict(_mapping(payload, "style_snapshot")),
        "formats": list(payload["formats"]),
        "artifacts": artifacts,
        "receipt_ref": payload["receipt_ref"],
        "replayed": replayed,
    }


def _validate_record(payload: Mapping[str, object]) -> None:
    fields = {
        "schema_version", "id", "status", "document_snapshot",
        "style_snapshot", "formats", "artifacts", "checkpoint", "receipt_ref",
    }
    if set(payload) != fields and set(payload) != fields | {"pptx_layout_plan"}:
        raise DocumentDeliveryConflict("document delivery record fields drifted")
    if payload.get("schema_version") != "1.0.0" or payload.get("status") not in {"prepared", "completed"}:
        raise DocumentDeliveryConflict("document delivery record is invalid")
    _delivery_identity(payload.get("id"))
    snapshot = _mapping(payload, "document_snapshot")
    if set(snapshot) != {
        "document_id", "revision", "content_hash", "title",
        "project_id", "source_snapshot", "source_refs",
    }:
        raise DocumentDeliveryConflict("document delivery snapshot fields drifted")
    _identity(snapshot.get("document_id"), "document_id")
    _positive_int(snapshot.get("revision"), "document revision")
    if not isinstance(snapshot.get("content_hash"), str) or not snapshot["content_hash"].startswith("sha256:"):
        raise DocumentDeliveryConflict("document delivery content hash is invalid")
    if not isinstance(snapshot.get("title"), str) or not snapshot["title"]:
        raise DocumentDeliveryConflict("document delivery title is invalid")
    _validate_style_snapshot(_mapping(payload, "style_snapshot"))
    _formats(payload.get("formats"))
    if "pptx_layout_plan" in payload:
        style = _mapping(payload, "style_snapshot")
        if (
            "pptx" not in payload["formats"]
            or style.get("pptx_renderer_revision") != 2
        ):
            raise DocumentDeliveryConflict("document delivery PPTX layout plan is invalid")
        descriptor = _mapping(payload, "pptx_layout_plan")
        if set(descriptor) != {"schema_version", "relative_path", "byte_count"}:
            raise DocumentDeliveryConflict("document delivery PPTX layout plan fields drifted")
        if descriptor.get("schema_version") != "1.0.0":
            raise DocumentDeliveryConflict("document delivery PPTX layout plan version is invalid")
        relative_path = descriptor.get("relative_path")
        byte_count = descriptor.get("byte_count")
        if (
            not isinstance(relative_path, str)
            or not relative_path.endswith("/layout-plan.json")
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or not 1 <= byte_count <= 1024 * 1024
        ):
            raise DocumentDeliveryConflict("document delivery PPTX layout plan descriptor is invalid")
    _mapping_list(payload.get("artifacts"), "artifacts")
    if payload["status"] == "prepared":
        if payload.get("checkpoint") != {"next_step": "render_artifacts"} or payload.get("receipt_ref") is not None:
            raise DocumentDeliveryConflict("prepared document delivery checkpoint is invalid")
    elif payload.get("checkpoint") is not None or not isinstance(payload.get("receipt_ref"), str):
        raise DocumentDeliveryConflict("completed document delivery receipt is invalid")


def _receipt_payload(payload: Mapping[str, object]) -> dict[str, object]:
    receipt = {
        "schema_version": "1.0.0",
        "id": _text(payload, "id"),
        "delivery_id": _text(payload, "id"),
        "status": "completed",
        "document_snapshot": dict(_mapping(payload, "document_snapshot")),
        "style_snapshot": dict(_mapping(payload, "style_snapshot")),
        "formats": list(_string_list(payload.get("formats"), "formats")),
        "artifacts": [dict(item) for item in _mapping_list(payload.get("artifacts"), "artifacts")],
    }
    if "pptx_layout_plan" in payload:
        receipt["pptx_layout_plan"] = dict(_mapping(payload, "pptx_layout_plan"))
    return receipt


def _style_key(snapshot: Mapping[str, object]) -> tuple[str, int, str, int]:
    return (
        _text(snapshot, "profile_id"),
        _positive_int(snapshot.get("revision"), "style revision"),
        _text(snapshot, "renderer_id"),
        _positive_int(snapshot.get("renderer_revision"), "renderer revision"),
    )


def _validate_style_snapshot(snapshot: Mapping[str, object]) -> None:
    base_fields = {
        "profile_id", "revision", "renderer_id", "renderer_revision", "delivery_key",
    }
    docx_fields = {
        "docx_renderer_id", "docx_renderer_revision", "docx_delivery_key",
    }
    pptx_fields = {
        "pptx_renderer_id", "pptx_renderer_revision", "pptx_delivery_key",
    }
    allowed = {
        frozenset(base_fields),
        frozenset(base_fields | docx_fields),
        frozenset(base_fields | pptx_fields),
        frozenset(base_fields | docx_fields | pptx_fields),
    }
    if frozenset(snapshot) not in allowed:
        raise DocumentDeliveryConflict("document delivery style snapshot fields drifted")
    profile_id, _style_revision, renderer_id, _renderer_revision = _style_key(snapshot)
    _identity(profile_id, "style profile_id")
    _identity(renderer_id, "style renderer_id")
    if not _STYLE_DELIVERY_KEY.fullmatch(_text(snapshot, "delivery_key")):
        raise DocumentDeliveryConflict("document delivery style key is invalid")
    if docx_fields & set(snapshot):
        _identity(snapshot.get("docx_renderer_id"), "DOCX renderer_id")
        _positive_int(snapshot.get("docx_renderer_revision"), "DOCX renderer revision")
        if not _STYLE_DELIVERY_KEY.fullmatch(_text(snapshot, "docx_delivery_key")):
            raise DocumentDeliveryConflict("document delivery DOCX key is invalid")
    if pptx_fields & set(snapshot):
        _identity(snapshot.get("pptx_renderer_id"), "PPTX renderer_id")
        _positive_int(snapshot.get("pptx_renderer_revision"), "PPTX renderer revision")
        if not _STYLE_DELIVERY_KEY.fullmatch(_text(snapshot, "pptx_delivery_key")):
            raise DocumentDeliveryConflict("document delivery PPTX key is invalid")


def _formats(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DocumentDeliveryError("formats must be a list")
    formats = tuple(value)
    if not formats or len(formats) != len(set(formats)) or any(item not in _FORMATS for item in formats):
        raise DocumentDeliveryError("formats must contain unique supported formats")
    return tuple(item for item in _FORMATS if item in formats)


def _identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise DocumentDeliveryError(f"{name} is invalid")
    return value


def _delivery_identity(value: object) -> str:
    if not isinstance(value, str) or not _DELIVERY_IDENTITY.fullmatch(value):
        raise DocumentDeliveryError("delivery_id is invalid")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DocumentDeliveryError(f"{name} must be a positive integer")
    return value


def _mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise DocumentDeliveryConflict(f"document delivery {key} is invalid")
    return value


def _mapping_list(value: object, name: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise DocumentDeliveryConflict(f"document delivery {name} is invalid")
    return list(value)


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DocumentDeliveryConflict(f"document delivery {name} is invalid")
    return list(value)


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DocumentDeliveryConflict(f"document delivery {key} is invalid")
    return value
