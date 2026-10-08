from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.app import _body, _recognition_error, _scope
from backend.memory_app.graph_view_api import install_graph_view_routes
from backend.recognition import RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def test_layout_routes_persist_validate_and_do_not_change_memory(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "memory.sqlite3")
    service = RecognitionService(records)
    scope = WorkScope("local-user", "a")
    eid = service.stage_experience(scope=scope, content="Evidence")
    app = FastAPI()
    app.add_exception_handler(RecognitionError, _recognition_error)
    router = APIRouter()
    writes = []

    def mutate(fn, *args, **kwargs):
        writes.append(fn.__name__)
        return fn(*args, **kwargs)

    install_graph_view_routes(router, records, mutate=mutate, read_body=_body, scope_for=_scope)
    app.include_router(router)
    body = {"project_id": "a", "expected_revision": 0, "node_ids": [eid],
            "positions": {eid: {"x": 60, "y": 80}}, "collapsed_ids": [],
            "hidden_ids": [], "selected_ids": [], "focus_id": eid}
    with TestClient(app) as client:
        saved = client.put("/graph-views/view-one", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["view"]["revision"] == 1
        assert client.get("/graph-views/view-one?project_id=a").json() == saved.json()
        assert len(client.get("/graph-views?project_id=a").json()["items"]) == 1
        assert client.get("/graph-views?project_id=b").json()["items"] == []
        assert client.put("/graph-views/view-one", json=body).status_code == 409
        assert client.put("/graph-views/view-one", json={**body, "content": "forged"}).status_code == 422
        assert client.put("/graph-views/view-one", json={"project_id": "a"}).status_code == 422
    assert writes == ["upsert", "upsert"]
    assert records.read("recognition_experiences", eid).revision == 1
