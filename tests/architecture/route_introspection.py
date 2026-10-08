from __future__ import annotations

from collections.abc import Iterable, Iterator


def registered_route_paths(application: object) -> tuple[str, ...]:
    """Expand FastAPI routes without collapsing duplicate path owners.

    FastAPI 0.139 defers included routers behind private wrapper objects.  The
    public OpenAPI projection flattens them but also merges duplicate paths,
    which would make architecture ownership tests weaker.  This test helper
    recursively expands the wrapper while preserving every registered path.
    """

    routes = getattr(application, "routes", None)
    if not isinstance(routes, Iterable):
        raise TypeError("application routes are unavailable")
    return tuple(_route_paths(routes, prefix=""))


def _route_paths(routes: Iterable[object], *, prefix: str) -> Iterator[str]:
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            yield f"{prefix}{path}"
            continue
        original_router = getattr(route, "original_router", None)
        nested_routes = getattr(original_router, "routes", None)
        if not isinstance(nested_routes, Iterable):
            continue
        include_context = getattr(route, "include_context", None)
        include_prefix = getattr(include_context, "prefix", "")
        if not isinstance(include_prefix, str):
            raise TypeError("included router prefix is invalid")
        yield from _route_paths(nested_routes, prefix=f"{prefix}{include_prefix}")
