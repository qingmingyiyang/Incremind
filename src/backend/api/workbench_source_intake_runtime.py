from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.api.job_runtime import build_rebuild_job_repository
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.product_core.ports import ObjectStorePort
from core.product_core.workbench_source_intake import (
    CaptureWorkbenchBookmarkCollection,
    CaptureWorkbenchFileSource,
    CaptureWorkbenchImageSource,
    CaptureWorkbenchLinkSource,
    CaptureWorkbenchTextSource,
)


@dataclass(frozen=True, slots=True)
class WorkbenchSourceIntakeServices:
    text: CaptureWorkbenchTextSource
    link: CaptureWorkbenchLinkSource
    bookmark_collection: CaptureWorkbenchBookmarkCollection
    file: CaptureWorkbenchFileSource
    image: CaptureWorkbenchImageSource


def build_workbench_source_intake_services(
    runtime_root: Path,
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
) -> WorkbenchSourceIntakeServices:
    """Compose basic Source intake services from the active Source and Job authorities."""

    source_registrar = ObjectStoreSourceRegistrar(object_store, namespace_id=namespace_id)
    job_repository = build_rebuild_job_repository(runtime_root, object_store)
    common = {
        "source_registrar": source_registrar,
        "job_repository": job_repository,
        "namespace_id": namespace_id,
    }
    return WorkbenchSourceIntakeServices(
        text=CaptureWorkbenchTextSource(**common),
        link=CaptureWorkbenchLinkSource(**common),
        bookmark_collection=CaptureWorkbenchBookmarkCollection(**common),
        file=CaptureWorkbenchFileSource(**common),
        image=CaptureWorkbenchImageSource(**common),
    )
