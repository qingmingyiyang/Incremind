from __future__ import annotations

from typing import Annotated, cast
from pathlib import Path

from fastapi import Depends, Request

from backend.api.bootstrap import ApiContainer, build_api_container
from backend.api.runtime_root_config import resolve_application_runtime_root

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "runtime"


def build_default_container() -> ApiContainer:
    return build_api_container(resolve_application_runtime_root(DEFAULT_ROOT))


def get_container(request: Request) -> ApiContainer:
    return cast(ApiContainer, request.app.state.container)


ApiContainerDep = Annotated[ApiContainer, Depends(get_container)]
