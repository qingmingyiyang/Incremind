from fastapi import APIRouter, FastAPI

from tests.architecture.route_introspection import registered_route_paths


def test_included_router_introspection_preserves_prefixes_and_duplicate_owners() -> None:
    first = APIRouter(prefix="/api")
    second = APIRouter(prefix="/api")
    first.add_api_route("/shared", lambda: None, methods=["GET"])
    second.add_api_route("/shared", lambda: None, methods=["POST"])
    application = FastAPI()
    application.include_router(first, prefix="/v1")
    application.include_router(second, prefix="/v1")

    paths = registered_route_paths(application)

    assert paths.count("/v1/api/shared") == 2
