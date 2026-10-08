"""Local project navigation metadata, never a cross-project evidence query."""
from backend.recognition import RecognitionError
from core.storage_provider import SQLiteUnitOfWorkError
from .links import similarity
from .overviews import ScopeOverviews
from .policies import get, version


def selected_version():
    try:
        return version('elsewhere')
    except ValueError:
        return None


def suggest_elsewhere(query, project, question, *, coverage, has_hits, tagged=False,
                      policy_version=None, readable_project=None):
    selected = policy_version or selected_version()
    if selected is None:
        return None
    policy = get('elsewhere', version=selected)
    # Let the registered policy own eligibility as well as the calibrated scores.
    if not policy(question=question, project_id=project, coverage=coverage,
                  has_hits=has_hits, tagged=tagged, operation='eligible'):
        return None
    overviews = ScopeOverviews(query.records, query.documents, query.models)
    candidates = []
    try:
        for row in query.records.list('v2_projects'):
            if row.object_id in {'me', 'inbox'}:
                continue
            # Shared-project membership is not implemented in the current owner.
            # A future caller must supply its real read-authority predicate.
            if row.payload.get('shared') and (readable_project is None or not readable_project(row.object_id)):
                continue
            name = row.payload.get('name')
            scenes = row.payload.get('scenes')
            if not isinstance(name, str) or not isinstance(scenes, list) or any(not isinstance(s, str) for s in scenes):
                continue
            for scene in (None, *scenes):
                overview = overviews.current_metadata(row.object_id, scene)
                candidates.append({'project_id': row.object_id, 'scene': scene,
                    'texts': (name, scene or '', overview['text'] if overview else '')})
    except (RecognitionError, SQLiteUnitOfWorkError):
        # A damaged derived description cannot prevent the original answer.
        return None
    # No exact question/overview vector identity exists in the current cache.
    # Reuse its original lexical fallback; never generate or borrow vectors.
    return policy(question=question, project_id=project, coverage=coverage,
        has_hits=has_hits, tagged=tagged, candidates=candidates, score=similarity)
