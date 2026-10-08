"""Historical rows and current guidance must not disagree at HTTP readers."""

from types import SimpleNamespace

from backend.memory_app.relations import RelationProposalService
from backend.recognition import WorkScope
from core.document_engine import SQLiteDocumentRepository
from tests.memory_app.test_api import _client
from tests.recognition.test_artifact_dependencies import _chain


def test_old_missed_cascade_is_unselectable_but_history_remains_readable(tmp_path):
    client, models = _client(tmp_path)
    with client:
        service = client.app.state.recognition_service
        scope = WorkScope("local-user", "project-a")
        env = SimpleNamespace(records=service.records, service=service, scope=scope,
            documents=SQLiteDocumentRepository(service.records, namespace_id="recognition"))
        chain = _chain(env)
        relations = RelationProposalService(service.records)
        proposal = relations.propose(scope, chain.recognitions[0].id, chain.recognitions[1].id,
                                     "supports", "Manually reviewed relationship")
        proposal = relations.review(scope, proposal["id"], proposal["revision"], "approved")
        rid = chain.recognitions[-1].id
        export_path = f"/api/recognition/recognitions/{rid}/markdown?project_id=project-a"
        original_export = client.get(export_path).text

        # Reproduce a database retained from the version that missed artifact
        # descendants; do not call the now-correct revoke/cascade implementation.
        original = service.records.read("recognition_experiences", chain.original)
        with service.records.begin() as tx:
            tx.put("recognition_experiences", original.object_id,
                   {**original.payload, "state": "revoked"}, expected_revision=original.revision)
            tx.commit()
        before = {kind: service.records.list(kind) for kind in (
            "recognitions", "recognition_questions", "recognition_versions", "recognition_tasks")}

        response = client.get("/api/recognition/workbench?project_id=project-a")
        assert response.status_code == 200, response.text
        result = response.json()
        invalid_ids = {chain.root.id, *(item.id for item in chain.recognitions)}
        assert not any(item["authorized"] for item in result["recognitions"] if item["id"] in invalid_ids)
        graph = client.get("/api/recognition/graph?project_id=project-a").json()
        historical_nodes = [item for item in graph["nodes"] if item["id"] in invalid_ids]
        assert len(historical_nodes) == len(invalid_ids)
        assert all(not item["selectable"] for item in historical_nodes)
        assert not any(edge["id"] == proposal["id"] for edge in graph["edges"])
        assert all(item["status"] == "stale" for item in result["mental_models"])
        assert client.get(export_path).text == original_export
        assert before == {kind: service.records.list(kind) for kind in before}
        assert models.calls == []
