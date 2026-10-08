"""Application use cases for the rebuilt product.

The historic public surface remains available, but it is loaded only when a
caller imports a symbol from this package. Runtime code should import its
concrete submodule so sidecar startup does not eagerly import every product
feature.
"""

from __future__ import annotations

from importlib import import_module
from threading import Lock
from types import ModuleType
from typing import Any


_exports_module: ModuleType | None = None
_exports_lock = Lock()


def _load_exports() -> ModuleType:
    global _exports_module
    if _exports_module is None:
        with _exports_lock:
            if _exports_module is None:
                _exports_module = import_module("._exports", __name__)
    return _exports_module


def __getattr__(name: str) -> Any:
    try:
        value = getattr(_load_exports(), name)
    except AttributeError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_load_exports())))
