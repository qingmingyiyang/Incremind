"""Compatibility imports for the former product route module.

New code imports a domain from ``backend.api.routes.product`` directly.
HTTP paths retain /api/rebuild for existing clients.
"""
from backend.api.routes.product import router
from backend.api.routes.product.job_lifecycle import (
    recover_rebuild_job_lifecycle,
    shutdown_rebuild_job_lifecycle,
)
from backend.api.routes.product.repositories import resolve_rebuild_repository_root

__all__ = [
    "router",
    "recover_rebuild_job_lifecycle",
    "shutdown_rebuild_job_lifecycle",
    "resolve_rebuild_repository_root",
]
