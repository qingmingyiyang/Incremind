from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.product_core.document_pdf_delivery import (
    DocumentPdfDeliveryConflict,
    DocumentPdfDeliveryError,
    DocumentPdfDeliveryService,
)
from core.effect_log import EffectReaper, EffectState
from tests.backend.unit.rebuild.test_document_delivery import _parts


def _service(tmp_path: Path, clock: list[datetime]):
    deliveries, records, _documents, document = _parts(tmp_path)
    html = deliveries.create_or_resume(
        document_id=str(document["id"]), expected_document_revision=1, formats=["html"]
    )
    service = DocumentPdfDeliveryService(
        records, deliveries, tmp_path, now=lambda: clock[0]
    )
    return service, records, str(html["delivery_id"])


def _slide_service(tmp_path: Path, clock: list[datetime]):
    deliveries, records, _documents, document = _parts(tmp_path)
    pptx = deliveries.create_or_resume(
        document_id=str(document["id"]), expected_document_revision=1, formats=["pptx"]
    )
    service = DocumentPdfDeliveryService(records, deliveries, tmp_path, now=lambda: clock[0])
    return service, records, str(pptx["delivery_id"])


def _executor() -> dict[str, str]:
    return {"electron_version": "43.1.0", "chrome_version": "142.0.0"}


def test_pdf_prepare_claim_complete_and_replay_preserves_source_authority(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    service, records, html_delivery_id = _service(tmp_path, clock)

    prepared = service.prepare(html_delivery_id, "builtin.a4-document")
    assert prepared["status"] == "waiting_for_electron"
    assert "Exact body." not in str(records.read("document_pdf_operations", prepared["operation_id"]).payload)
    claim = service.claim(str(prepared["operation_id"]), _executor())
    assert claim["claim_token"]
    print_input = service.print_input(str(prepared["operation_id"]), str(claim["claim_token"]))
    assert print_input["html"].startswith("<!DOCTYPE html>")
    completed = service.complete(str(prepared["operation_id"]), str(claim["claim_token"]),
                                 electron_version="43.1.0", chrome_version="142.0.0", pdf_bytes=b"%PDF-1.7\nbody")
    assert completed["status"] == "completed" and completed["artifact"]["verified"] is True
    assert service.complete(str(prepared["operation_id"]), "replayed", electron_version="x", chrome_version="x", pdf_bytes=b"not-used")["replayed"] is True
    assert service.prepare(html_delivery_id, "builtin.a4-document")["replayed"] is True
    name, pdf = service.artifact(str(prepared["operation_id"]))
    assert name.endswith(".pdf") and pdf.startswith(b"%PDF-")
    assert records.read("document_pdf_receipts", str(prepared["operation_id"])) is not None


def test_pdf_claim_expires_and_startup_recovery_returns_waiting(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    service, _records, html_delivery_id = _service(tmp_path, clock)
    prepared = service.prepare(html_delivery_id, "builtin.a4-document")
    service.claim(str(prepared["operation_id"]), _executor())
    clock[0] += timedelta(minutes=6)
    recovered = EffectReaper(service.effects).recover_expired(
        now=int(clock[0].timestamp()),
        probes={"document_pdf_delivery": lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={"document_pdf_delivery": lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert [(item.operation_id, item.state) for item in recovered] == [
        (prepared["operation_id"], EffectState.PLANNED)
    ]
    assert service.projection(str(prepared["operation_id"]))["status"] == "waiting_for_electron"


def test_pdf_startup_finalizes_file_published_after_durable_checkpoint(tmp_path: Path, monkeypatch) -> None:
    clock = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    service, records, html_delivery_id = _service(tmp_path, clock)
    operation_id = str(service.prepare(html_delivery_id, "builtin.a4-document")["operation_id"])
    claim = service.claim(operation_id, _executor())
    real_publish = service._publish

    def crash_after_publish(path, data):
        real_publish(path, data)
        raise BaseException("simulated process exit")

    monkeypatch.setattr(service, "_publish", crash_after_publish)
    with pytest.raises(BaseException, match="simulated process exit"):
        service.complete(operation_id, str(claim["claim_token"]), electron_version="43.1.0",
                         chrome_version="142.0.0", pdf_bytes=b"%PDF-published-before-sqlite-terminal")
    publishing = records.read("document_pdf_operations", operation_id)
    assert publishing is not None and publishing.payload["status"] == "waiting_for_electron"
    publication = records.read("document_pdf_publication_facts", operation_id)
    assert publication is not None and publication.payload["artifact"]["verified"] is True
    assert records.read("document_pdf_receipts", operation_id) is None

    clock[0] += timedelta(minutes=6)
    recovered = EffectReaper(service.effects).recover_expired(
        now=int(clock[0].timestamp()),
        probes={"document_pdf_delivery": lambda effect: service.verify_effect(effect.operation_id)},
        verifiers={"document_pdf_delivery": lambda effect: service.verify_effect(effect.operation_id)},
    )
    assert [(item.operation_id, item.state) for item in recovered] == [
        (operation_id, EffectState.SETTLED_OK)
    ]
    assert service.projection(operation_id)["status"] == "completed"
    assert service.artifact(operation_id)[1] == b"%PDF-published-before-sqlite-terminal"


def test_pdf_rejects_invalid_claim_payload_and_unverified_bytes(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    service, _records, html_delivery_id = _service(tmp_path, clock)
    operation_id = str(service.prepare(html_delivery_id, "builtin.a4-document")["operation_id"])
    with pytest.raises(DocumentPdfDeliveryError, match="attestation"):
        service.claim(operation_id, {"electron_version": "x"})
    claim = service.claim(operation_id, _executor())
    with pytest.raises(DocumentPdfDeliveryError, match="PDF bytes"):
        service.complete(operation_id, str(claim["claim_token"]), electron_version="43.1.0", chrome_version="142.0.0", pdf_bytes=b"no")
    with pytest.raises(DocumentPdfDeliveryConflict, match="attestation"):
        service.complete(operation_id, str(claim["claim_token"]), electron_version="43.2.0", chrome_version="142.0.0", pdf_bytes=b"%PDF-valid")
    with pytest.raises(DocumentPdfDeliveryConflict, match="claim"):
        service.print_input(operation_id, "wrong")


def test_slide_pdf_profile_requires_pptx_and_uses_frozen_inline_slide_input(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    service, records, delivery_id = _slide_service(tmp_path, clock)

    prepared = service.prepare(delivery_id, "builtin.slide-document")
    a4_service, _a4_records, html_delivery_id = _service(tmp_path / "a4", clock)
    a4 = a4_service.prepare(html_delivery_id, "builtin.a4-document")
    assert prepared["operation_id"] != a4["operation_id"]
    claim = service.claim(str(prepared["operation_id"]), _executor())
    print_input = service.print_input(str(prepared["operation_id"]), str(claim["claim_token"]))
    assert print_input["profile"]["profile_id"] == "builtin.slide-document"
    assert print_input["profile"]["print_options"]["preferCSSPageSize"] is True
    assert print_input["html"].startswith("<!DOCTYPE html>")
    assert "Exact body." not in str(records.read("document_pdf_operations", prepared["operation_id"]).payload)


def test_pdf_receipt_rejects_same_length_artifact_drift(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 26, tzinfo=timezone.utc)]
    service, records, html_delivery_id = _service(tmp_path, clock)
    operation_id = str(service.prepare(html_delivery_id, "builtin.a4-document")["operation_id"])
    claim = service.claim(operation_id, _executor())
    original = b"%PDF-original"
    service.complete(
        operation_id, str(claim["claim_token"]), electron_version="43.1.0",
        chrome_version="142.0.0", pdf_bytes=original,
    )
    receipt = records.read("document_pdf_receipts", operation_id)
    assert receipt is not None
    artifact = receipt.payload["artifact"]
    path = service._path(str(artifact["relative_path"]))
    replacement = b"%PDF-tampered"
    assert len(replacement) == len(original)
    path.write_bytes(replacement)

    with pytest.raises(DocumentPdfDeliveryConflict, match="artifact is unavailable"):
        service.artifact(operation_id)
