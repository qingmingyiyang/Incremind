from contextlib import contextmanager
from copy import deepcopy
import json

from backend.recognition import RecognitionConflict
from backend.shared.memory_sidecars import decayed_score, utc_now
from core.ai_kernel.product_admission import bundle_from_payload

from ..transaction_records import TransactionRecords
from .budget import text_tokens
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_readers import qualified_record_read
from .policies import get
from .shared_auxiliary import _qualified_commit
from .shared_rendered_preparation import _query_current
from .style_context import _eligible


HOST_FIELDS = frozenset({'agent_binding', 'agent_policy_binding'})


def task_query(query, request, *, prepared_bundle=None):
    application = query.answer_turns.application
    native = application.state.ai_turn_store
    actual = native.get_request(request['turn_id'])
    if actual is None:
        if prepared_bundle is None:
            if query.origin_admission is not None:
                _query_current(query)
            elif any(ref.get('uri', '').startswith('crp://material-origin/') for ref in request['input']['refs']):
                raise RecognitionConflict('shared_task_admission_unavailable')
            return query, request
        bundle = bundle_from_payload(prepared_bundle)
        from .material_origins import ORIGIN_KIND
        items = [item for item in bundle.items if item.kind == ORIGIN_KIND]
        if not items:
            if query.origin_admission is not None:
                raise RecognitionConflict('shared_task_origin_missing')
            return query, request
        if len(items) != 1:
            raise RecognitionConflict('shared_task_origin_changed')
        factory = getattr(application.state, 'shared_query_factory', None)
        if factory is None:
            raise RecognitionConflict('shared_task_factory_unavailable')
        from .material_origins import SharedProjectLease
        lease = SharedProjectLease.from_payload(items[0].payload['shared']['lease'])
        actor = factory.devices.identity_for_device(lease.device_id, lease.actor_user_id)
        if actor is None or actor.revision != lease.device_revision:
            raise RecognitionConflict('shared_task_origin_changed')
        issued = factory.query(application, actor, lease.project_id, lease=lease)
        issued.origin_admission._bind_bundle(request, bundle)
        return issued, request
    if ({key: value for key, value in actual.items() if key not in HOST_FIELDS}
            != {key: value for key, value in request.items() if key not in HOST_FIELDS}):
        raise RecognitionConflict('shared_task_native_request_changed')
    if actual['desired_outcome'] != 'project.task' or actual.get('agent_binding', {}).get('parent_run_id'):
        from .task_model_inputs import task_profile_chain
        actual = task_profile_chain(native, application.state.agent_runtime_composition.store, actual)[-1]
    from .shared_auxiliary import current_qualified_commit_scope
    current = current_qualified_commit_scope()
    if current is not None:
        admission = current._admission
        if admission.application is not application:
            raise RecognitionConflict('shared_task_execution_owner_changed')
        admission.restore(actual)
        factory = application.state.shared_query_factory
        with factory._lock:
            issued = [item for item in factory._issued_queries if item.origin_admission is admission]
        if len(issued) != 1:
            raise RecognitionConflict('shared_task_query_not_issued')
        _query_current(issued[0])
        return issued[0], actual
    issued = query._query_for_native_request(actual)
    if issued.origin_admission is not None:
        _query_current(issued)
    return issued, actual


@contextmanager
def task_source_scope(query, request, *, reader=None, prepared_bundle=None):
    issued, root = task_query(query, request, prepared_bundle=prepared_bundle)
    if issued.origin_admission is None:
        yield issued, root, reader
        return
    from .shared_auxiliary import current_qualified_commit_scope
    if reader is not None and current_qualified_commit_scope() is None:
        raise RecognitionConflict('shared_task_source_scope_required_before_actor_transaction')
    with _qualified_commit(issued.origin_admission) as scope:
        if reader is None:
            with qualified_record_read(issued.receipt_records, validate=scope._validate) as transaction:
                current = TransactionRecords(transaction)
                scope._actor_reader(current)
                yield issued, root, current
        else:
            current = reader if isinstance(reader, TransactionRecords) else TransactionRecords(reader)
            scope._actor_reader(current)
            yield issued, root, current
        scope._validate()


def validate_inputs(query, request, *, reader=None, prepared_bundle=None, validate_constraints=True):
    from .shared_task_materials import read_task_material_item, read_prepared_task_material_item
    with task_source_scope(query, request, reader=reader, prepared_bundle=prepared_bundle) as (issued, root, current):
        if issued.origin_admission is None:
            return False
        if prepared_bundle is None:
            read_task_material_item(issued, root, reader=current)
        else:
            read_prepared_task_material_item(issued, root, bundle_from_payload(prepared_bundle), reader=current)
        if validate_constraints:
            issued.project_constraint_input.validate(root, reader=current)
        from .part_context import validate_bound_context
        validate_bound_context(current, issued.models, root, issued)
        return True


def profile(query, request, *, reader=None):
    from .profile import frozen_task_profile
    from .method_context import frozen_task_methods
    from .shared_task_materials import read_task_material_item
    with task_source_scope(query, request, reader=reader) as (issued, root, current):
        if issued.origin_admission is None:
            return None
        read_task_material_item(issued, root, reader=current)
        return {**frozen_task_profile(current, issued.profile_owner.service, root),
            'constraints': issued.project_constraint_input.validate(root, reader=current),
            'methods': frozen_task_methods(current, issued, root), 'style': frozen_style(current, issued, root)}


def division_sources(query, row):
    from .shared_task_materials import read_task_material_item
    execution = query.receipt_records.read('v2_task_executions', row.object_id)
    if execution is None or row.payload['project_id'] != query.origin_admission.lease.project_id:
        raise RecognitionConflict('shared_task_division_origin_unavailable')
    old_query, root = task_query(query.execution_owner, execution.payload['request'])
    if (old_query.origin_admission is None or old_query.source_namespace != query.source_namespace
            or old_query.origin_admission.personal != query.origin_admission.personal
            or json.loads(root['input']['text']).get('task') != row.payload['task_text']):
        raise RecognitionConflict('shared_task_division_origin_changed')
    with task_source_scope(old_query, root) as (issued, root, reader):
        initial = read_task_material_item(issued, root, reader=reader)
        freshness = issued.outcome_freshness
        context = freshness.completion_context(root, reader=reader)
        qualified = freshness._qualify_source_proofs(context['source_proofs'], reader=reader, local_only=True)
        if not qualified['complete'] or 'generation' not in qualified['privacy_ceiling']['effective_purposes']:
            raise RecognitionConflict('shared_task_division_source_unavailable')
        materials = list(initial['selection']['origin_materials'])
        for item in context['materials']:
            material = {key: item[key] for key in ('type', 'id', 'revision', 'project_id', 'namespace')}
            if material not in materials:
                materials.append(material)
        return materials


def division_examples(query, divisions, rows, project, text):
    from urllib.parse import urlsplit
    from .privacy import egress_allowed, is_private_project
    from .shared_projects import SharedProjectError
    from .shared_task_division_vectors import DivisionVectorProducer
    _query_current(query)
    candidates, materials = [], {}
    for row in rows:
        if row.payload['project_id'] != project or row.payload.get('deleted') or row.payload['outcome'] == 'failed':
            continue
        try:
            materials[row.object_id] = division_sources(query, row)
        except (RecognitionConflict, SharedProjectError):
            continue
        candidates.append(row)

    def validate():
        _query_current(query)
        embedding = query.models.public().get('embedding', {})
        remote = urlsplit(str(embedding.get('base_url', ''))).hostname not in {'localhost', '127.0.0.1', '::1'}
        if (embedding.get('configured') and embedding.get('enabled') and remote
                and not egress_allowed(query.records, query.models, project, 'embedding')):
            raise RecognitionConflict('shared_task_division_egress_changed')
        for row in candidates:
            if division_sources(query, row) != materials[row.object_id]:
                raise RecognitionConflict('shared_task_division_source_changed')

    vectors = DivisionVectorProducer(query, candidates, text) if candidates else None
    examples = divisions.similar(project, text, rows=candidates, validate_current=validate,
        private_project=lambda own: own != project or is_private_project(query.records, own),
        embedding_wire=vectors, embedding_allowed=vectors.allowed if vectors is not None else None)
    selected = []
    for example in examples:
        for item in materials[example['source_turn_id']]:
            if item not in selected:
                selected.append(item)
    return examples, selected


def style_materials(query, project, *, scene, version, scope_version, strengths=None):
    _query_current(query)
    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
    rows = []
    with _qualified_commit(query.origin_admission) as scope:
        with qualified_record_read(query.receipt_records, validate=scope._validate) as transaction:
            current = TransactionRecords(transaction)
            scope._actor_reader(current)
            for owner, service in ((origins.shared, query.service),
                    (origins.profile, query.profile_owner.service)):
                source_reader = scope.reader_for(origins, owner, actor_reader=current)
                records = owner.records if owner is origins.shared else TransactionRecords(source_reader)
                eligible = _eligible(records, service, owner.project_id, scene, scope_version,
                    source_owner=owner if owner is origins.shared else query.receipt_records,
                    source_reader=source_reader, own_projects=(owner.project_id,))
                for item in eligible:
                    key = json.dumps([owner.namespace, item['id']], ensure_ascii=False, separators=(',', ':'))
                    rows.append({**item, 'namespace': owner.namespace, 'style_key': key})
    rows.sort(key=lambda item: item['style_key'])
    if strengths is None:
        now = utc_now()
        strengths = []
        for item in rows:
            owner = origins.owner(item['project_id'], item['namespace'])
            usage = owner.records.read('v2_usage_insight', item['id'])
            strengths.append(max(0, decayed_score(usage.payload, now)) if usage else 0)
    if len(strengths) != len(rows):
        raise RecognitionConflict('shared_task_style_changed')
    policy_rows = [{**item, 'id': item['style_key'], 'strength': strength}
        for item, strength in zip(rows, strengths)]
    result = get('style', version=version)(policy_rows, estimate_tokens=text_tokens)
    by_key = {item['style_key']: item for item in rows}
    return {'project_id': project, 'scene': scene, 'version': version, 'scope_version': scope_version,
        **result, 'items': [by_key[item['id']] for item in result['selected']],
        'shared_task_style': {'schema_version': 1, 'execution': deepcopy(query.origin_admission.personal),
            'origin': query.origin_admission.lease.payload(), 'rows': deepcopy(rows), 'strengths': strengths}}


def validate_style(query, block):
    proof = block.get('shared_task_style')
    if (not isinstance(proof, dict) or set(proof) != {'schema_version', 'execution', 'origin', 'rows', 'strengths'}
            or proof['schema_version'] != 1 or proof['execution'] != query.origin_admission.personal
            or proof['origin'] != query.origin_admission.lease.payload()):
        raise RecognitionConflict('shared_task_style_changed')
    fresh = style_materials(query, block['project_id'], scene=block['scene'], version=block['version'],
        scope_version=block['scope_version'], strengths=proof['strengths'])
    if fresh != block:
        raise RecognitionConflict('shared_task_style_changed')


def frozen_style(records, query, request):
    from .style_context import TASK_COLLECTION, style_input
    row = records.read(TASK_COLLECTION, request['turn_id'])
    expected = json.loads(request['input']['text']).get('style_input')
    if row is None and expected is None:
        return {'text': '', 'count': 0, 'tokens': 0, 'items': []}
    if (row is None or row.revision != 1 or row.payload['project_id'] != request['scope']['project_id']
            or row.payload['input_refs'] != request['input']['refs'] or style_input(row.payload['style']) != expected):
        raise RecognitionConflict('shared_task_style_changed')
    block = row.payload['style']
    validate_style(query, block)
    return block
