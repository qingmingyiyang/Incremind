"""Project-scoped, bounded projections of authoritative memory relationships."""
from backend.recognition import RecognitionConflict
from .recall_state import preference


def memory_graph(service, scope, *, focus=None, offset=0, limit=40):
    if offset < 0 or not 1 <= limit <= 100:
        raise RecognitionConflict("graph page is out of range")
    experiences = service.list_experiences(scope=scope, include_revoked=True)
    recognitions = service.list_recognitions(scope=scope, include_inactive=True)
    questions = service.list_questions(scope=scope, include_stale=True)
    nodes = []
    edges = []
    for item in experiences:
        nodes.append({"id": item.id, "type": "experience", "title": item.content[:80],
                      "content": item.content, "status": item.state, "revision": item.revision, "selectable": False})
    for item in recognitions:
        nodes.append({"id": item.id, "type": "recognition", "title": item.content[:80],
                      "content": item.content, "status": item.effective_state, "recorded_state": item.state,
                      "revision": item.revision, "evidence_eligible": item.evidence_eligible,
                      "evidence_reason": item.evidence_reason,
                      "selectable": item.authorized, **preference(service.records, scope, item.id)})
        for source in item.source_experience_ids:
            edges.append({"id": f"support:{source}:{item.id}", "from": source, "to": item.id, "type": "supports"})
        for source in item.source_recognition_ids:
            edges.append({"id": f"derive:{source}:{item.id}", "from": source, "to": item.id, "type": "derived_from"})
    for item in questions:
        nodes.append({"id": item.id, "type": "question", "title": item.question,
                      "content": item.content, "status": item.effective_state, "recorded_state": item.state,
                      "revision": item.revision, "evidence_eligible": item.evidence_eligible,
                      "evidence_reason": item.evidence_reason, "selectable": False})
        for source in item.recognition_ids:
            edges.append({"id": f"question:{source}:{item.id}", "from": source, "to": item.id, "type": "supports"})
    for record in service.records.list("recognition_relations"):
        if record.payload.get("project_id") == scope.project_id:
            edges.append({"id": record.object_id, "from": record.payload["from_id"],
                          "to": record.payload["to_id"], "type": record.payload["relation"]})
    allowed = {node["id"] for node in nodes}
    current = {item.id: item for item in recognitions if item.authorized}
    for record in service.records.list("recognition_relation_proposals"):
        value = record.payload
        if value.get("project_id") != scope.project_id or value.get("state") != "approved":
            continue
        source, target = current.get(value.get("from_id")), current.get(value.get("to_id"))
        if source is not None and target is not None and source.revision == value.get("from_revision") and target.revision == value.get("to_revision"):
            edges.append({"id": record.object_id, "from": source.id, "to": target.id,
                          "type": value["relation"], "status": "approved", "source": "manual",
                          "evidence": value["evidence"]})
    edges = [edge for edge in edges if edge["from"] in allowed and edge["to"] in allowed]
    if focus is not None:
        if focus not in allowed:
            raise RecognitionConflict("graph node is unavailable in this project")
        neighbors = {focus}
        for edge in edges:
            if focus in (edge["from"], edge["to"]):
                neighbors.update((edge["from"], edge["to"]))
        nodes = sorted((node for node in nodes if node["id"] in neighbors), key=lambda node: node["id"] != focus)
    total = len(nodes)
    page = nodes[offset:offset + limit]
    visible = {node["id"] for node in page}
    return {"nodes": page, "edges": [edge for edge in edges if edge["from"] in visible and edge["to"] in visible],
            "total": total, "offset": offset, "limit": limit, "focus": focus,
            "next_offset": offset + limit if offset + limit < total else None}
