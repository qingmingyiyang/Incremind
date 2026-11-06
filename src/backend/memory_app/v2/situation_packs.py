from datetime import datetime, timezone
import json

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow

from ..context_adapter import ContextSelectionError, format_recognition_content
from ..recall_state import is_recall_excluded
from ..source_egress import SourceEgressService
from .budget import text_tokens
from .insights import insight_view
from .policies import get, version
from .policies.situation_pack import PACK_VERSION, version_for_compose
from .policies.types import ScopeInput
from .privacy import egress_allowed, is_private_project, privacy_revision


COLLECTION = 'v2_situation_packs'
OBSERVATIONS = 'v2_situation_observations'


def enabled():
    return version_for_compose(version('compose')) is not None


def _clock():
    return datetime.now(timezone.utc)


def _scope_kind(query):
    return 'shared' if query.origin_admission is not None else 'personal'


def _privacy(query):
    return {'source': privacy_revision(query.records), 'actor': privacy_revision(query.receipt_records)}


def _allowed(query, project):
    return (not query.offline and not is_private_project(query.records, project)
            and (query.ask_target()['execution_location'] == 'local'
                 or egress_allowed(query.records, query.models, project, 'generation')))


def observe(query, project, scene, situation, turn_id):
    if not enabled() or not isinstance(situation, str) or not situation.strip() or not _allowed(query, project):
        return
    query.validate_retrieval_origin()
    fields = {'project_id': project, 'scope_kind': _scope_kind(query), 'scene': scene, 'situation': situation.strip()}
    with query.receipt_records.begin() as tx:
        previous = tx.read(OBSERVATIONS, turn_id)
        if previous is not None:
            if any(previous.payload[key] != value for key, value in fields.items()):
                raise RecognitionConflict('situation_observation_changed')
            return
        tx.put(OBSERVATIONS, turn_id, {**fields, 'observed_at': _clock().isoformat()}, expected_revision=0)
        tx.commit()


def _related(records, project, scene, scope_kind, situation, policy, now):
    observations = []
    for row in records.list_matching(OBSERVATIONS, project_id=project):
        payload = row.payload
        if (payload['scope_kind'] == scope_kind and payload['scene'] == scene
                and now - policy.window <= datetime.fromisoformat(payload['observed_at']) <= now
                and policy(payload['situation'], situation)):
            observations.append(row)
    return sorted(observations, key=lambda row: (row.payload['observed_at'], row.object_id))


def _member(query, project, scene, identity, revision, *, scope_policy=None):
    scope = WorkScope('local-user', project)
    if is_recall_excluded(query.records, scope, identity):
        return None
    view = insight_view(query.records, scope, identity, service=query.service)
    if view is None or view['state'] != 'active' or not get('scope', version=scope_policy)(ScopeInput(scene, view['scene'])):
        return None
    recognition = query.service.get_recognition(scope=scope, recognition_id=identity)
    if recognition is None or not recognition.authorized or not recognition.evidence_eligible or recognition.revision != revision:
        return None
    entry = recognition.retrieval_projection()
    if not entry.get('conditions'):
        return None
    authority = SourceEgressService(query.records)
    try:
        snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': identity, 'revision': revision}])
        authority.require(snapshot, 'generation')
        excerpt = format_recognition_content(entry)
    except (RecognitionError, ContextSelectionError):
        return None
    return {'id': identity, 'kind': 'recognition', 'entry': entry, 'scope': scope, 'layer': 'L3',
            'scene': view['scene'], 'title': '已确认认识', 'excerpt': excerpt, 'snapshot': snapshot,
            'windows': (EvidenceWindow(0, len(entry['content']), entry['content']),),
            'match_in': 'content', 'href': f'#view=recognition&project_id={project}',
            'document_ids': view['document_ids'], 'document_id': None, 'persona': False,
            **({'namespace': query.source_namespace} if query.origin_admission is not None else {})}


def render_task(rows, *, policy=None):
    selected = policy or get('situation_pack', version=PACK_VERSION)
    return selected.prefix + '\n\n'.join(f'[{index}] {row["excerpt"]}' for index, row in enumerate(rows, 1)) if rows else ''


def _fits(rows, policy):
    from .answer_evidence import build_evidence_catalog, model_catalog
    catalog = model_catalog(build_evidence_catalog(rows, compose_version='@34'))
    answer = policy.prefix + json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))
    return max(text_tokens(render_task(rows, policy=policy)),
        text_tokens(answer) + policy.numbering_reserve * len(rows)) <= policy.token_limit


def prepare(query, project, scene, situation, *, methods=None):
    if not enabled() or not isinstance(situation, str) or not situation.strip() or not _allowed(query, project):
        return None
    query.validate_retrieval_origin()
    policy = get('situation_pack', version=PACK_VERSION)
    now, records, scope_kind = _clock(), query.receipt_records, _scope_kind(query)
    observed = _related(records, project, scene, scope_kind, situation, policy, now)
    if len(observed) < policy.minimum:
        return None
    identity = 'pack-' + observed[0].object_id
    epoch = _privacy(query)
    previous = records.read(COLLECTION, identity)
    context = {'project_id': project, 'scene': scene, 'scope_kind': scope_kind,
               'privacy': epoch, 'policy': PACK_VERSION, 'scope_policy': version('scope'),
               'anchor_id': observed[0].object_id}
    rows = None
    if previous is not None and all(previous.payload[key] == value for key, value in context.items()):
        restored = [_member(query, project, scene, item['id'], item['revision']) for item in previous.payload['members']]
        if restored and all(row is not None for row in restored) and _fits(restored, policy):
            rows = restored
    if rows is None:
        if methods is None:
            methods = query._collect_candidates(project, situation, scene=scene, only_layers={'L3'},
                method_query=situation).get('method_candidates', ())
        rows, seen = [], set()
        for row in methods:
            if row['id'] in seen or row['scope'].project_id != project or row.get('previously_struck'):
                continue
            current = _member(query, project, scene, row['id'], row['entry']['revision'])
            if current is None or not _fits([*rows, current], policy):
                continue
            seen.add(row['id'])
            rows.append(current)
    members = [{'id': row['id'], 'revision': row['entry']['revision']} for row in rows]
    payload = {**context, 'members': members, 'last_seen': observed[-1].payload['observed_at']}
    query.validate_retrieval_origin()
    if _privacy(query) != epoch or not _allowed(query, project):
        raise RecognitionConflict('situation_pack_privacy_changed')
    updated = bool(rows) and (previous is None or dict(previous.payload) != payload)
    removed = not rows and previous is not None
    if updated or removed:
        with records.begin() as tx:
            if tx.read(COLLECTION, identity) == previous:
                if updated:
                    tx.put(COLLECTION, identity, payload, expected_revision=previous.revision if previous else 0)
                else:
                    tx.delete(COLLECTION, identity, expected_revision=previous.revision)
                tx.commit()
    return ({'id': identity, **context, 'rows': [{**row, 'situation_pack': identity} for row in rows]} if rows else None)


def validate(query, binding, rows):
    project, scene = binding['project_id'], binding['scene']
    if (not rows or binding['policy'] != PACK_VERSION or binding['privacy'] != _privacy(query)
            or not _allowed(query, project)):
        raise RecognitionConflict('situation_pack_privacy_changed')
    query.validate_retrieval_origin()
    if binding['scope_kind'] != _scope_kind(query):
        raise RecognitionConflict('situation_pack_scope_changed')
    for row in rows:
        current = _member(query, project, scene, row['id'], row['entry']['revision'],
            scope_policy=binding['scope_policy'])
        if (current is None or row['situation_pack'] != binding['id']
                or any(current.get(key) != row.get(key) for key in ('entry', 'snapshot', 'scene', 'excerpt', 'namespace'))):
            raise RecognitionConflict('situation_pack_member_changed')


def attach(plan):
    pack = plan.get('_situation_pack')
    if pack is not None and not any(plan.get(key) for key in ('raw_answer_proof', 'evidence_answer_proof', 'initial_answer_proof')):
        identities = {row['id'] for row in pack['rows']}
        plan['chosen'] = [row for row in plan['chosen'] if row['kind'] != 'recognition' or row['id'] not in identities] + pack['rows']
    return plan


def prune(records, query):
    policy, now = get('situation_pack', version=PACK_VERSION), _clock()
    observations = [row for row in records.list(OBSERVATIONS)
                    if datetime.fromisoformat(row.payload['observed_at']) < now - policy.window]
    expired = {row.object_id for row in observations}
    invalid = []
    for row in records.list(COLLECTION):
        value = row.payload
        # 共享投影随调用者授权重建，后台清理无需借用共享内容的读取权限。
        if (value['scope_kind'] == 'shared' or value['anchor_id'] in expired
                or datetime.fromisoformat(value['last_seen']) < now - policy.window
                or value['privacy'] != _privacy(query) or is_private_project(records, value['project_id'])
                or any(_member(query, value['project_id'], value['scene'], item['id'], item['revision'],
                       scope_policy=value['scope_policy']) is None
                       for item in value['members'])):
            invalid.append(row)
    if not observations and not invalid:
        return
    with records.begin() as tx:
        for collection, rows in ((OBSERVATIONS, observations), (COLLECTION, invalid)):
            for row in rows:
                if tx.read(collection, row.object_id) == row:
                    tx.delete(collection, row.object_id, expected_revision=row.revision)
        tx.commit()
