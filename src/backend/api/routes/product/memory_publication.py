"""Memory publication ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.api.memory_publication_effect_runtime import (
    plan_memory_publication,
    read_memory_publication_receipt,
)

from core.aggregate_repository_factory import (
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
    STRUCTURED_DATABASE_NAME,
)
from core.memory_core import SharedTrustAuditActivationError, require_shared_trust_audit_activation
from core.memory_core.publication_trust_audit_uow import MemoryPublicationTrustAuditUnitOfWorkError
from core.project_skill_core import ProjectSkillPublicationCompositeError
from core.storage_provider import (
    JsonObjectStore,
    RebuildStorageSettings,
    SQLiteStructuredRecordStore,
)

from . import http as product_http


async def _sqlite_memory_publication(
    request: Request, runtime_root: Path, settings: RebuildStorageSettings, object_id: str
) -> JSONResponse:
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "Memory publication rejected", "reason": "memory publication requires confirm=true"}, product_http._no_store_headers())
    reason = product_http._optional_body_str(body, "reason")
    if reason is None:
        return product_http._json_response(400, {"detail": "Memory publication rejected", "reason": "reason is required"}, product_http._no_store_headers())
    path = request.url.path
    layer = next((name for prefix, name in (("/api/rebuild/staging-atoms/", "atom"), ("/api/rebuild/staging-scenarios/", "scenario"), ("/api/rebuild/staging-series-memory/", "series_memory")) if path.startswith(prefix)), None)
    try:
        action = "memory_rollback" if layer is None else "memory_publish"
        operation_id = plan_memory_publication(
            request.app.state.effect_runtime,
            namespace_id=settings.namespace_id,
            action=action,
            object_id=object_id,
            reason=reason,
            layer=layer,
        )
        request.app.state.effect_runtime.dispatch_operation(operation_id, now=int(time.time()))
        result = read_memory_publication_receipt(runtime_root, operation_id)
        if layer is None:
            return product_http._json_response(200, {**result, "rollback_ref": f"crp://{settings.namespace_id}/memory-publications/{result['publication_id']}/rollback", "memory_publication_state": "rolled_back_not_published"}, product_http._no_store_headers())
        return product_http._json_response(200, {**result, "published_object_id": object_id, "published_ref": f"crp://{settings.namespace_id}/memory/{layer}/{object_id}.json", "rollback_ref": f"crp://{settings.namespace_id}/memory-publications/{result['publication_id']}/rollback", "memory_publication_state": "published_with_rollback_ref"}, product_http._no_store_headers())
    except MemoryPublicationTrustAuditUnitOfWorkError as error:
        return product_http._json_response(409, {"detail": "Memory publication rejected", "reason": str(error)}, product_http._no_store_headers())


async def _project_skill_memory_publication(
    request: Request,
    runtime_root: Path,
    store: JsonObjectStore,
    settings: RebuildStorageSettings,
    object_id: str,
) -> JSONResponse:
    body = await product_http._json_body(request)
    if not isinstance(body, Mapping) or body.get("confirm") is not True:
        return product_http._json_response(400, {"detail": "Project Skill publication rejected", "reason": "memory publication requires confirm=true"}, product_http._no_store_headers())
    reason = product_http._optional_body_str(body, "reason")
    if reason is None:
        return product_http._json_response(400, {"detail": "Project Skill publication rejected", "reason": "reason is required"}, product_http._no_store_headers())
    resolution = AggregateRepositoryFactory(runtime_root=runtime_root, namespace_id=settings.namespace_id, json_store=store).project_skill_repository_resolution()
    if resolution.authority_identity != "sqlite:structured-records-v1":
        return product_http._json_response(409, {"detail": "Project Skill publication rejected", "reason": "Project Skill SQLite authority is not active"}, product_http._no_store_headers())
    database = runtime_root / ".rebuild-data" / STRUCTURED_DATABASE_NAME
    try:
        require_shared_trust_audit_activation(
            SQLiteStructuredRecordStore(database),
            namespace_id=settings.namespace_id,
            target_identity=resolution.authority_identity,
        )
    except SharedTrustAuditActivationError as error:
        return product_http._json_response(409, {"detail": "Project Skill publication rejected", "reason": str(error)}, product_http._no_store_headers())
    try:
        if request.url.path.startswith("/api/rebuild/staging-project-skills/"):
            if SQLiteStructuredRecordStore(database).read("staging_project_skills", object_id) is None:
                raise ProjectSkillPublicationCompositeError("Project Skill staging draft not found")
            action = "project_skill_publish"
            publication_revision = None
            skill_revision = None
        else:
            publication_revision = product_http._optional_body_int(body, "expected_publication_revision")
            skill_revision = product_http._optional_body_int(body, "expected_project_skill_revision")
            if publication_revision is None or skill_revision is None:
                raise ProjectSkillPublicationCompositeError("expected publication and Project Skill revisions are required")
            action = "project_skill_rollback"
        operation_id = plan_memory_publication(
            request.app.state.effect_runtime,
            namespace_id=settings.namespace_id,
            action=action,
            object_id=object_id,
            reason=reason,
            expected_publication_revision=publication_revision,
            expected_project_skill_revision=skill_revision,
        )
        request.app.state.effect_runtime.dispatch_operation(operation_id, now=int(time.time()))
        result = read_memory_publication_receipt(runtime_root, operation_id)
        return product_http._json_response(200, {**result, "memory_publication_state": ("published_with_rollback_ref" if result["status"] == "published" else "rolled_back_not_published")}, product_http._no_store_headers())
    except (ProjectSkillPublicationCompositeError, AggregateRepositoryFactoryError) as error:
        return product_http._json_response(409, {"detail": "Project Skill publication rejected", "reason": str(error)}, product_http._no_store_headers())
