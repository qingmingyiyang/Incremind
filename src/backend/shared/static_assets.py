from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

FRONTEND_ASSET_MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
}


def _resolve_dist_path(dist_dir: Path, relative_path: str) -> Path | None:
    """Resolve an untrusted frontend path and keep it inside ``dist_dir``."""

    try:
        candidate = (dist_dir / relative_path).resolve(strict=False)
        candidate.relative_to(dist_dir)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> FileResponse:
        response = super().file_response(*args, **kwargs)
        return _with_no_cache(response)


def _with_no_cache(response: FileResponse) -> FileResponse:
    suffix = Path(response.path).suffix.lower()
    media_type = FRONTEND_ASSET_MEDIA_TYPES.get(suffix)
    if media_type is not None:
        response.media_type = media_type
        response.headers["Content-Type"] = media_type

    response.headers["Cache-Control"] = "no-store"
    return response


def mount_frontend_dist(app: FastAPI, root_dir: Path) -> None:
    dist_dir = (root_dir / "src" / "frontend" / "dist").resolve(strict=False)
    index_path = _resolve_dist_path(dist_dir, "index.html")
    assets_dir = _resolve_dist_path(dist_dir, "assets")

    if not dist_dir.is_dir() or index_path is None or not index_path.is_file():
        return

    if assets_dir is not None and assets_dir.is_dir():
        app.mount(
            "/assets",
            NoCacheStaticFiles(directory=str(assets_dir)),
            name="frontend-assets",
        )

    @app.get("/", include_in_schema=False)
    def serve_frontend_index() -> FileResponse:
        return _with_no_cache(FileResponse(index_path))

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend_path(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        candidate = _resolve_dist_path(dist_dir, full_path)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Not Found")

        if candidate.is_file():
            return _with_no_cache(FileResponse(candidate))

        if candidate.exists():
            raise HTTPException(status_code=404, detail="Not Found")

        return _with_no_cache(FileResponse(index_path))
