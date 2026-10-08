"""Scoped candidate aliases and the common Insight read model."""
from backend.recognition import WorkScope
from ..source_egress import recognition_service

from .projects import scene_of
from backend.recognition.experience_origins import read_experience_origin

_UNFROZEN = object()


def source_experiences(reader, scope, experience_ids, recognition_ids=(), *,
        experience_revisions=_UNFROZEN, recognition_revisions=_UNFROZEN):
    """Flatten the existing same-scope evidence chain for an explicit copy."""
    if hasattr(reader, '_connect'):
        with reader.begin() as tx:
            return source_experiences(tx, scope, experience_ids, recognition_ids,
                experience_revisions=experience_revisions, recognition_revisions=recognition_revisions)
    service = recognition_service(reader)
    if experience_revisions is _UNFROZEN and recognition_revisions is _UNFROZEN:
        service._assert_sources_active(reader, scope, experience_ids, recognition_ids)
    else:
        service.assert_frozen_sources_in_uow(reader, scope, experience_ids, recognition_ids,
            experience_revisions, recognition_revisions)
    experiences, visited, pending = set(experience_ids), set(), list(recognition_ids)
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        visited.add(identity)
        if len(visited) > 256:
            raise ValueError('insight_source_graph_too_large')
        row = reader.read('recognitions', identity)
        if not _in_scope(row, scope):
            raise ValueError('insight_source_unavailable')
        experiences.update(row.payload.get('source_experience_ids', []))
        pending.extend(row.payload.get('source_recognition_ids', []))
    rows = [reader.read('recognition_experiences', identity) for identity in sorted(experiences)]
    if not rows or any(not _in_scope(row, scope) for row in rows):
        raise ValueError('insight_source_unavailable')
    return rows


def origin_documents(reader, scope, experiences):
    """Expose precise original owners only for actual retained copy links."""
    from backend.recognition import WorkScope
    result = set()
    for row in experiences:
        visited = set()
        while (origin := read_experience_origin(reader, row)) is not None:
            if row.object_id in visited or len(visited) >= 256:
                raise ValueError('insight_source_graph_too_large')
            visited.add(row.object_id)
            marker, row = origin
            own = WorkScope(scope.user_id, marker.payload['source_project_id'])
            result.update((own.project_id, identity) for identity in source_documents(reader, own, [row.object_id]))
    return [{'project_id': project, 'document_id': identity} for project, identity in sorted(result)]


def _in_scope(row, scope):
    return row is not None and row.payload.get("scope") == {
        "user_id": scope.user_id, "project_id": scope.project_id}


def resolve_insight(reader, scope, insight_id):
    """Resolve published candidate aliases using stored identity, never names."""
    candidate = reader.read("recognition_candidates", insight_id)
    if _in_scope(candidate, scope):
        if candidate.payload.get("state") != "published":
            return candidate
        target = candidate.payload.get("recognition_id")
        if not isinstance(target, str):
            return None
        recognition = reader.read("recognitions", target)
        return recognition if _in_scope(recognition, scope) else None
    recognition = reader.read("recognitions", insight_id)
    return recognition if _in_scope(recognition, scope) else None


def source_documents(reader, scope, experience_ids, recognition_ids=()):
    """Collect scoped document evidence through the existing recognition chain."""
    documents, visited, pending = set(), set(), list(recognition_ids)
    experiences = set(experience_ids)
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        visited.add(identity)
        if len(visited) > 256:
            raise ValueError("insight_source_graph_too_large")
        row = reader.read("recognitions", identity)
        if not _in_scope(row, scope):
            continue
        experiences.update(row.payload.get("source_experience_ids", []))
        pending.extend(row.payload.get("source_recognition_ids", []))
    for identity in experiences:
        experience = reader.read("recognition_experiences", identity)
        if not _in_scope(experience, scope):
            continue
        for ref in experience.payload.get("provenance", {}).get("source_refs", []):
            document = reader.read("documents", ref.get("id")) if ref.get("type") == "document" else None
            if document and document.payload.get("project_id") == scope.project_id:
                documents.add(ref["id"])
    return documents


def insight_view(reader, scope, insight_id, *, service=None):
    """Build the architecture's Insight DTO; inactive history is not displayed.

    Store readers can construct the domain service. Transaction readers must
    pass the already assembled service for its qualified recognition lookup.
    """
    row = resolve_insight(reader, scope, insight_id)
    if row is None:
        return None
    payload = row.payload
    if reader.read("v2_candidate_merges", row.object_id):
        return None
    candidate = reader.read("recognition_candidates", row.object_id)
    kind = "candidate" if _in_scope(candidate, scope) else "recognition"
    aliases = []
    state = payload.get("state")
    recall_state, recall_by = 'normal', 'user'
    if kind == 'candidate' and state == 'pending' and reader.read('v2_candidate_fade', row.object_id):
        state, recall_state, recall_by = 'forgotten', 'forgotten', 'auto'
    if kind == "recognition":
        aliases = sorted(item.object_id for item in reader.list("recognition_candidates")
            if _in_scope(item, scope) and item.payload.get("state") == "published"
            and item.payload.get("recognition_id") == row.object_id)
        qualified = (service or recognition_service(reader)).get_recognition(
            scope=scope, recognition_id=row.object_id)
        state = qualified.effective_state if qualified else None
        preference = reader.read("recognition_recall_preferences", row.object_id)
        if preference and preference.payload.get('project_id') == scope.project_id and preference.payload.get('user_id') == scope.user_id:
            recall_state = preference.payload['state']
            recall_by = preference.payload.get('by', 'user')
        if (preference and preference.payload.get("project_id") == scope.project_id
                and preference.payload.get("user_id") == scope.user_id
                and preference.payload.get("state") == "forgotten"):
            state = "forgotten"
    if state not in {"pending", "active", "stale", "forgotten"}:
        return None
    document_ids = set()
    experiences = payload.get("source_experience_ids", [])
    sources = payload.get("source_recognition_ids", [])
    document_ids = source_documents(reader, scope, experiences, sources)
    assignment = scene_of(reader, "recognition" if kind == "recognition" else "candidate", row.object_id)
    if assignment is None:
        for alias in aliases:
            assignment = scene_of(reader, "candidate", alias)
            if assignment is not None:
                break
    if assignment is None:
        for document_id in sorted(document_ids):
            assignment = scene_of(reader, "document", document_id)
            if assignment is not None:
                break
    related = {}
    for collection in ("recognition_relations", "recognition_relation_proposals"):
        for relation in reader.list(collection):
            edge = relation.payload
            if not _in_scope(relation, scope) or (collection.endswith("proposals") and edge.get("state") != "approved"):
                continue
            left, right = edge.get("from_id"), edge.get("to_id")
            target = right if left == row.object_id else left if right == row.object_id else None
            if isinstance(target, str):
                other = resolve_insight(reader, scope, target)
                if other is not None:
                    related[other.object_id] = {"id": other.object_id, "text": other.payload["content"]}
    originals = []
    for marker in reader.list("v2_candidate_merges"):
        if marker.payload.get("candidate_id") not in {row.object_id, *aliases}:
            continue
        parent = reader.read("recognition_candidates", marker.object_id)
        if _in_scope(parent, scope):
            originals.append({"id": parent.object_id, "text": parent.payload["content"], "revision": parent.revision})
    extra = {}
    hint = next((marker for identity in [row.object_id, *aliases]
        if (marker := reader.read('v2_candidate_hints', identity)) is not None
        and marker.payload.get('project_id') == scope.project_id), None)
    if hint:
        extra['hint'] = {key: hint.payload[key] for key in ('relation', 'target_id', 'scope_hint')}
        extra['hint']['target'] = None
        target = reader.read('recognitions', hint.payload.get('target_id')) if hint.payload.get('target_id') else None
        qualified_target = None
        if target and target.payload.get('scope') in [{'user_id': scope.user_id, 'project_id': project}
                for project in {scope.project_id, 'me'}]:
            target_service = service or recognition_service(reader)
            target_scope = WorkScope(**target.payload['scope'])
            qualified_target = (target_service.get_recognition(scope=target_scope, recognition_id=target.object_id)
                if hasattr(reader, '_connect') else target_service._qualified_recognition(reader, target))
        if (qualified_target and qualified_target.authorized and qualified_target.id == target.object_id
                and qualified_target.scope == target_scope and qualified_target.revision == target.revision):
            extra['hint']['target'] = {'id': target.object_id, 'project_id': target.payload['scope']['project_id'],
                'text': target.payload['content'], 'conditions': list(target.payload.get('conditions', [])),
                'revision': target.revision}
        if extra['hint']['scope_hint']:
            from ..source_egress import SourceEgressService
            from .transaction_records import TransactionRecords
            try:
                rows = source_experiences(reader, scope, experiences, sources,
                    experience_revisions=payload.get('source_experience_revisions'),
                    recognition_revisions=payload.get('source_recognition_revisions'))
                authority = SourceEgressService(reader if hasattr(reader, '_connect') else TransactionRecords(reader))
                authority.require(authority.snapshot(scope, [{'type': 'experience', 'id': source.object_id,
                    'revision': source.revision} for source in rows]), 'generation')
            except ValueError:
                extra['hint']['scope_hint'] = None
    try:
        origins = origin_documents(reader, scope, source_experiences(reader, scope, experiences, sources,
            experience_revisions=payload.get('source_experience_revisions'),
            recognition_revisions=payload.get('source_recognition_revisions')))
        if origins:
            extra['source_documents'] = origins
    except ValueError:
        pass  # Invalid source history cannot grant a cross-project drill target.
    if kind == 'candidate' and state == 'pending':
        from .source_sections import comment_source_for_candidate
        comment = comment_source_for_candidate(reader, scope, row)
        if comment is not None:
            extra['comment_source'] = comment
    return {**extra, **({"merged_from": originals} if originals else {}), "id": row.object_id, "aliases": aliases, "kind": kind,
        "text": payload["content"], "conditions": list(payload.get("conditions", [])),
        "state": state, "recall_state": recall_state, "recall_by": recall_by,
        "scene": assignment["scene"] if assignment and assignment.get("project_id") == scope.project_id else None,
        "source_count": len(set(experiences)) + len(set(sources)),
        "document_ids": sorted(document_ids), "related": [related[key] for key in sorted(related)],
        "revision": row.revision, "pattern": bool(reader.read("v2_insight_patterns", row.object_id)
            or any(reader.read("v2_insight_patterns", alias) for alias in aliases))}
