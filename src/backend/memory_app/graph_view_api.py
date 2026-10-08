"""Explicit layout persistence; these routes never publish memory changes."""

from fastapi import Request

from backend.recognition import RecognitionError
from .graph_views import GraphViewService


def install_graph_view_routes(router, records, *, mutate, read_body, scope_for):
    views = GraphViewService(records)

    @router.get("/graph-views")
    async def listing(project_id: str = "default"):
        return {"items": views.list(scope_for(project_id))}

    @router.get("/graph-views/{view_id}")
    async def get(view_id: str, project_id: str = "default"):
        return {"view": views.get(scope_for(project_id), view_id)}

    @router.put("/graph-views/{view_id}")
    async def save(view_id: str, request: Request):
        body = await read_body(request)
        allowed = {"project_id", "expected_revision", "node_ids", "positions",
                   "collapsed_ids", "hidden_ids", "selected_ids", "focus_id"}
        if set(body).difference(allowed):
            raise RecognitionError("graph view contains unsupported fields")
        required = allowed.difference({"project_id", "focus_id"})
        if not required.issubset(body):
            raise RecognitionError("graph view requires a complete layout")
        return {"view": mutate(
            views.upsert, scope_for(body.get("project_id")), view_id,
            body["expected_revision"], node_ids=body["node_ids"],
            positions=body["positions"], collapsed_ids=body["collapsed_ids"],
            hidden_ids=body["hidden_ids"], selected_ids=body["selected_ids"],
            focus_id=body.get("focus_id"),
        )}
