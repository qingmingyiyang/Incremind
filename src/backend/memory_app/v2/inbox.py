"""Explicit inbox filing reuses recognition writes without automatic publication."""
from fastapi import HTTPException, Request
from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..recall_state import preference
from .recall_preferences import set_preference
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.search_and_recall.evidence_windows import query_terms
from ..workspace_contracts import _json, _project
from .insights import resolve_insight, insight_view
from .projects import assign_scene
from .transaction_records import TransactionRecords
from ..source_egress import SourceEgressService, recognition_service


def _target(reader, identity):
    identity = _project(identity)
    row = reader.read('v2_projects', identity)
    if row is None or identity == 'inbox':
        raise HTTPException(400, 'invalid_target_project')
    return row


def install_inbox_routes(router, *, records, service, read):
    @router.post('/inbox/insight/{insight_id}/file')
    async def file_insight(insight_id: str, request: Request):
        body = await _json(request)
        if set(body).difference({'source_project_id', 'target_project_id', 'scene', 'expected_revision', 'confirm'}) or not {'target_project_id', 'expected_revision'} <= set(body):
            raise HTTPException(400, 'invalid_inbox_fields')
        revision, scene = body['expected_revision'], body.get('scene')
        if type(revision) is not int or revision < 1:
            raise HTTPException(400, 'invalid_expected_revision')
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            raise HTTPException(400, 'invalid_scene')
        source_project = _project(body.get('source_project_id', 'inbox'))
        confirm = body.get('confirm', False)
        if type(confirm) is not bool:
            raise HTTPException(400, 'invalid_inbox_fields')
        scope = WorkScope('local-user', source_project)
        try:
            with records.begin() as tx:
                target = _target(tx, body['target_project_id'])
                if scene is not None and scene not in target.payload['scenes']:
                    raise HTTPException(400, 'invalid_scene')
                original = resolve_insight(tx, scope, _project(insight_id))
                if original is None:
                    raise HTTPException(400, 'invalid_inbox_insight')
                if source_project != 'inbox':
                    hint = tx.read('v2_candidate_hints', original.object_id)
                    if (not confirm or hint is None or hint.payload.get('project_id') != source_project
                            or hint.payload.get('scope_hint') != target.object_id or target.object_id == source_project):
                        raise HTTPException(400, 'invalid_insight_destination')
                filed = tx.read('v2_inbox_filings', original.object_id)
                if original.revision != revision or (filed and filed.payload['source_revision'] == revision):
                    raise HTTPException(409, 'insight_revision_conflict')
                candidate = tx.read('recognition_candidates', original.object_id)
                if candidate is not None:
                    if candidate.payload['state'] != 'pending':
                        raise HTTPException(409, 'insight_not_pending')
                elif original.payload['state'] != 'active' or preference(tx, scope, original.object_id)['recall_state'] == 'forgotten':
                    raise HTTPException(409, 'insight_not_active')
                enlisted = TransactionRecords(tx)
                writer = recognition_service(enlisted, cache_invalidation=service.cache_invalidation,
                    product_draft_validator=service.product_draft_validator)
                destination = WorkScope('local-user', target.object_id)
                from .insights import source_experiences
                source_rows = source_experiences(tx, scope, original.payload.get('source_experience_ids', []),
                    original.payload.get('source_recognition_ids', []),
                    experience_revisions=original.payload.get('source_experience_revisions'),
                    recognition_revisions=original.payload.get('source_recognition_revisions'))
                authority = SourceEgressService(enlisted)
                snapshot = authority.snapshot(scope, [{'type': 'experience', 'id': row.object_id,
                    'revision': row.revision} for row in source_rows])
                if any(not node['effective_purposes'] for node in snapshot['nodes']):
                    raise HTTPException(400, 'insight_private_source')
                authority.require(snapshot, 'generation')
                experiences = []
                for row in source_rows:
                    experience = writer.stage_experience(scope=destination, content=row.payload['content'],
                        copy_from={'project_id': source_project, 'experience_id': row.object_id, 'revision': row.revision})
                    experiences.append(experience)
                new = writer.propose(scope=destination, content=original.payload['content'],
                    source_experience_ids=experiences, conditions=original.payload.get('conditions', []))
                if scene is not None:
                    assign_scene(enlisted, 'candidate', new.id, target.object_id, scene)
                if candidate is not None:
                    writer.reject_candidate(scope=scope, candidate_id=original.object_id,
                        expected_revision=revision, reviewer='local-user')
                else:
                    current = preference(tx, scope, original.object_id)
                    set_preference(enlisted, scope, original.object_id, recognition_revision=revision,
                        preference_revision=current['recall_preference_revision'], state='forgotten')
                tx.put('v2_inbox_filings', original.object_id,
                    {'source_revision': revision, 'source_project_id': source_project,
                     'target_project_id': target.object_id, 'candidate_id': new.id},
                    expected_revision=filed.revision if filed else 0)
                from .policies import version
                if version('place') != '@1':
                    from .placement import record_choice
                    assignment = tx.read('v2_scene_assignments_candidate', original.object_id)
                    record_choice(tx, original.object_id, source_project, target.object_id, scene,
                        document_revision=revision, assignment_revision=assignment.revision if assignment else 0,
                        title=original.payload['content'], summary='', kind='candidate',
                        action_id=new.id, action='file')
                if confirm:
                    new = writer.publish(scope=destination, candidate_id=new.id,
                        expected_revision=new.revision, reviewer='local-user')
                tx.commit()
            if confirm:
                from .usage import initialize_usage
                from .stats import record_activity
                initialize_usage(records, 'insight', new.id, destination.project_id)
                record_activity(records, 'confirm', destination.project_id, new.id,
                    event_id='confirm-' + new.id)
            return insight_view(records, destination, new.id, service=service)
        except (RecognitionConflict, SQLiteUnitOfWorkConflict):
            raise HTTPException(409, 'insight_revision_conflict') from None
        except RecognitionError:
            raise HTTPException(400, 'insight_invalid') from None

    @router.get('/inbox/suggestions')
    def suggestions(target_project_id: str):
        target = _target(records, target_project_id)
        scenes = {name: {term for term, _ in query_terms(name)} for name in target.payload['scenes']}
        for row in read.insights(target.object_id):
            if row['state'] == 'active' and row['scene'] in scenes:
                scenes[row['scene']].update(term for term, _ in query_terms(row['text']))
        result = []
        for row in read.insights('inbox'):
            if row['state'] not in {'pending', 'active'}:
                continue
            terms = query_terms(row['text'])
            scores = {name: sum(weight for term, weight in terms if term in vocabulary) for name, vocabulary in scenes.items()}
            best = max(scores.values(), default=0)
            winners = [name for name, score in scores.items() if score == best and score > 0]
            result.append({'id': row['id'], 'scene': winners[0] if len(winners) == 1 else None})
        return {'items': result}
