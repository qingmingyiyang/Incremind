"""按真实灵感轮次绑定原话，供提问、干活和只读下钻共用。"""
from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.recognition.experience_origins import read_experience_origin
from backend.recognition.provenance import ExperienceProvenance
from core.search_and_recall.evidence_windows import select_evidence_windows

from ..source_egress import SourceEgressService
from .insights import insight_view, resolve_insight
from .policies import get
from .policies.types import ScopeInput, RankInput
from .privacy import is_private_project
from .usage import recall_weight


COORDINATES = 'recognition_experience_content_v1'


def _scoped(row, project):
    return row is not None and row.payload.get('scope') == {
        'user_id': 'local-user', 'project_id': project}


def _stamp(row):
    return (row.object_id, row.revision) if row is not None else None


def _bindings(reader, project, *, recall=True):
    """只跟随原灵感轮次及既有归类连接，手工认识不进入全局池。"""
    for turn in reader.list('v2_turns'):
        value = turn.payload
        origin_project = value.get('project_id')
        if (turn.revision != 1 or value.get('intent') != 'inspiration'
                or origin_project not in {'inbox', project}):
            continue
        captured = value.get('receipt', {}).get('inspiration', {}).get('insight', {})
        if captured.get('kind') != 'candidate' or captured.get('text') != value.get('user_text'):
            continue
        identity = captured.get('id')
        candidate = reader.read('recognition_candidates', identity) if isinstance(identity, str) else None
        if not _scoped(candidate, origin_project):
            continue
        ids = candidate.payload.get('source_experience_ids', [])
        if len(ids) != 1 or candidate.payload.get('source_recognition_ids'):
            continue
        original = reader.read('recognition_experiences', ids[0])
        try:
            expected_provenance = (ExperienceProvenance.from_payload(
                {'kind': 'user_statement', 'actor': 'local-user'},
                recorded_at=original.payload.get('created_at')).to_payload() if original is not None else None)
        except ValueError:
            continue
        if (not _scoped(original, origin_project) or original.payload.get('state') != 'active'
                or original.payload.get('provenance') != expected_provenance
                or original.payload.get('content') != value.get('user_text')
                or candidate.payload.get('source_experience_revisions', {}).get(ids[0]) != original.revision):
            continue
        canonical = resolve_insight(reader, WorkScope('local-user', origin_project), identity)
        if canonical is None:
            continue
        filing = reader.read('v2_inbox_filings', canonical.object_id)
        selected, experience, own, origin_marker = candidate, original, origin_project, None
        if filing is not None:
            filed = filing.payload
            if (origin_project != 'inbox' or filed.get('source_project_id') != origin_project
                    or filed.get('target_project_id') != project):
                continue
            selected = reader.read('recognition_candidates', filed.get('candidate_id'))
            if not _scoped(selected, project) or selected.payload.get('source_recognition_ids'):
                continue
            matches = []
            for copied_id in selected.payload.get('source_experience_ids', []):
                copied = reader.read('recognition_experiences', copied_id)
                if not _scoped(copied, project) or copied.payload.get('state') != 'active':
                    continue
                try:
                    origin = read_experience_origin(reader, copied)
                except ValueError:
                    continue
                if (origin is not None and origin[1] == original
                        and selected.payload.get('source_experience_revisions', {}).get(copied_id) == copied.revision):
                    matches.append((copied, origin[0]))
            if len(matches) != 1:
                continue
            experience, origin_marker = matches[0]
            own = project
        scope = WorkScope('local-user', own)
        resolved = resolve_insight(reader, scope, selected.object_id)
        if resolved is None:
            continue
        if recall:
            view = insight_view(reader, scope, selected.object_id)
            if view is None or view['state'] not in {'pending', 'active'}:
                continue
        else:
            view = {'scene': None}
        proof = {
            'turn': _stamp(turn), 'candidate': _stamp(selected), 'canonical': _stamp(resolved),
            'origin_candidate': _stamp(candidate), 'original': _stamp(original),
            'filing': _stamp(filing), 'origin_marker': _stamp(origin_marker),
            'fade': _stamp(reader.read('v2_candidate_fade', selected.object_id)),
            'merge': _stamp(reader.read('v2_candidate_merges', selected.object_id)),
            'recall': _stamp(reader.read('recognition_recall_preferences', resolved.object_id)),
            'scene': view.get('scene'),
        }
        yield {'row': experience, 'scope': scope, 'candidate_id': selected.object_id,
               'filed': own != 'inbox', 'proof': proof, 'scene': view.get('scene')}


def resolve_inspiration(reader, project, identity, *, recall=True):
    """拒绝歧义身份；不根据候选正文或相似文本推断原件。"""
    matches = [row for row in _bindings(reader, project, recall=recall) if row['row'].object_id == identity]
    if len(matches) != 1:
        raise RecognitionConflict('inspiration original binding unavailable')
    return matches[0]


def collect_inspirations(records, project, question, scene=None, *, instruction=None):
    rules = getattr(get('scope'), 'inspirations', None)
    if rules is None:
        return []
    policy = rules(instruction or question)
    authority = SourceEgressService(records)
    result = []
    from .budget import WINDOW_SCAN_CHARS
    for bound in _bindings(records, project):
        row, scope = bound['row'], bound['scope']
        if (bound['filed'] and not policy['include_project']
                or is_private_project(records, 'inbox') and not bound['filed']
                or is_private_project(records, scope.project_id)
                or not get('scope')(ScopeInput(scene, bound['scene']))):
            continue
        try:
            snapshot = authority.snapshot(scope, [{'type': 'experience', 'id': row.object_id,
                                                  'revision': row.revision}])
            authority.require(snapshot, 'generation')
        except RecognitionError:
            continue
        selection = select_evidence_windows(row.payload['content'], question, max_chars=WINDOW_SCAN_CHARS)
        if not selection.score:
            continue
        entry = {'id': row.object_id, 'kind': 'experience', 'project_id': scope.project_id,
                 'revision': row.revision, 'content': row.payload['content'], 'title': '你的灵感'}
        result.append({'id': row.object_id, 'kind': 'experience', 'layer': 'inspiration',
            'inspiration': True, 'brainstorming': policy['include_project'], 'scope': scope,
            'entry': entry, 'snapshot': snapshot, 'inspiration_proof': bound['proof'],
            'candidate_id': bound['candidate_id'], 'scene': bound['scene'], 'title': '你的灵感',
            'excerpt': selection.excerpt, 'windows': selection.windows, 'match_in': selection.match_in,
            'coordinate_space': COORDINATES, 'href': '', 'document_id': None, 'document_ids': [],
            'sort_time': row.payload.get('created_at'),
            'score': get('rank')(RankInput(selection.score, recall_weight(records, 'inspiration', row.object_id)))})
    return result


def validate_inspiration(records, project, candidate, *, remote=True):
    """每次外发前后复验原话、状态、归类和既有外发快照。"""
    bound = resolve_inspiration(records, project, candidate['id'])
    row, scope = bound['row'], bound['scope']
    if (candidate['inspiration_proof'] != bound['proof']
            or candidate['entry']['revision'] != row.revision
            or candidate['entry']['content'] != row.payload['content']
            or candidate['scope'] != scope or is_private_project(records, scope.project_id)):
        raise RecognitionConflict('inspiration changed during turn')
    authority = SourceEgressService(records)
    authority.validate_snapshot(scope, candidate['snapshot'])
    if remote:
        authority.require(candidate['snapshot'], 'generation')


def validate_task_inputs(records, models, request, *, purpose='generation', query=None):
    """只为真实灵感原话适配任务边界，其余材料走原校验器。"""
    from copy import deepcopy
    from .turn_requests import validate_frozen_inputs
    from .privacy import resolve_turn_material
    from .policies import override
    project = request['scope']['project_id']
    reduced = deepcopy(request)
    privacy = reduced['privacy']
    extras = []
    with override(**request.get('policy_versions', {})):
        for item in request['privacy']['material_refs']:
            if item['project_id'] in {project, 'me'}:
                continue
            if (item['type'] != 'experience' or item['project_id'] != 'inbox'
                    or getattr(get('scope'), 'inspirations', None) is None):
                raise RecognitionConflict('task material scope conflicted')
            bound = resolve_inspiration(records, project, item['id'])
            if bound['scope'].project_id != item['project_id'] or bound['row'].revision != item['revision']:
                raise RecognitionConflict('task inspiration authority changed')
            resolve_turn_material(records, bound['scope'], item)
            snapshot = SourceEgressService(records).snapshot(bound['scope'], [
                {'type': 'experience', 'id': item['id'], 'revision': item['revision']}])
            if snapshot not in request['privacy']['source_snapshots']:
                raise RecognitionConflict('task inspiration snapshot changed')
            SourceEgressService(records).validate_snapshot(bound['scope'], snapshot)
            if request['privacy']['allow_remote']:
                SourceEgressService(records).require(snapshot, purpose)
            extras.append((item, snapshot))
        privacy['material_refs'] = [row for row in privacy['material_refs'] if all(row != item for item, _ in extras)]
        privacy['source_snapshots'] = [row for row in privacy['source_snapshots'] if all(row != snapshot for _, snapshot in extras)]
        validate_frozen_inputs(records, models, reduced, purpose=purpose, query=query)


def freeze_task_turn(*, inspirations=(), query=None, **values):
    """用原冻结装配回调接入已选原话，不改通用隐私范围。"""
    from ..kernel.turn_requests import freeze_product_turn
    from .privacy import freeze_turn_materials, resolve_turn_material, privacy_revision
    chosen = {(row['scope'].project_id, row['id']): row for row in inspirations}
    project = values['project_id']

    def freeze(records, models, project, materials, **options):
        ordinary, extra = [], []
        for item in materials:
            (extra if (item['project_id'], item['id']) in chosen else ordinary).append(item)
        authority = SourceEgressService(records)
        selected, privacy = freeze_turn_materials(records, models, project, ordinary,
                                                 authority=authority, **options)
        for item in extra:
            candidate = chosen[item['project_id'], item['id']]
            if item != {'type': 'experience', 'id': candidate['id'],
                        'revision': candidate['entry']['revision'], 'project_id': candidate['scope'].project_id}:
                raise RecognitionConflict('task inspiration material changed')
            validate_inspiration(records, project, candidate, remote=not options.get('local_only', False))
            resolved, roots = resolve_turn_material(records, candidate['scope'], item)
            snapshot = authority.snapshot(candidate['scope'], roots)
            if snapshot != candidate['snapshot']:
                raise RecognitionConflict('task inspiration snapshot changed')
            selected.append(resolved)
            privacy['material_refs'].append(dict(item))
            privacy['source_snapshots'].append(snapshot)
        if privacy_revision(records) != privacy['privacy_revision']:
            raise RecognitionConflict('privacy changed while freezing task inspiration')
        return selected, privacy

    def validate(records, models, request, *, purpose='generation'):
        return validate_task_inputs(records, models, request, purpose=purpose, query=query)

    return freeze_product_turn('project.task', freeze_materials=freeze,
        validate_request=validate, instruction=values.get('text', ''), **values)


def task_read_planner(ordinary, records, models, *, query=None):
    """产品注入仅适配原话锁，原研究读取和普通材料边界继续委托原 owner。"""
    from copy import deepcopy
    from contextlib import ExitStack
    from ..research_sources import ReadControl, _WireLocks
    from ..original_sources import source_store
    from ..transaction_records import TransactionRecords

    class Planner:
        def __getattr__(self, name):
            return getattr(ordinary, name)

        def plan(self, request, events, capabilities, payloads, execution_control=None):
            project = request.get('scope', {}).get('project_id')
            extras = [snapshot for snapshot in request.get('privacy', {}).get('source_snapshots', [])
                      if snapshot.get('scope', {}).get('project_id') not in {project, 'me'}]
            if not extras or execution_control is None:
                return ordinary.plan(request, events, capabilities, payloads, execution_control=execution_control)
            validate_task_inputs(records, models, request, query=query)
            reduced = deepcopy(request)
            reduced['privacy']['source_snapshots'] = [snapshot for snapshot in
                request['privacy']['source_snapshots'] if snapshot not in extras]
            control = ReadControl(execution_control, records, ordinary.turns, ordinary.agents, reduced)
            control.validate()

            class OriginalControl:
                def __getattr__(self, name):
                    return getattr(control, name)

                def checkpoint(self):
                    validate_task_inputs(records, models, request, query=query)
                    return control.checkpoint()

                def begin_model_wire_attempt(self, *args, **kwargs):
                    with records.begin() as reader, ExitStack() as locks:
                        enlisted = TransactionRecords(reader)
                        collector = _WireLocks(enlisted, 'local-user')
                        for snapshot in extras:
                            collector.snapshot(snapshot)
                        for collection, identity in sorted(collector.identities):
                            locks.enter_context(source_store(reader).locked(collection, identity))
                        validate_task_inputs(enlisted, models, request, query=query)
                        return control.begin_model_wire_attempt(*args, **kwargs)

            return ordinary.inner.plan(request, events, capabilities, payloads,
                                       execution_control=OriginalControl())

    return Planner()
