from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from backend.recognition import RecognitionConflict
from core.ai_kernel.agent_store import SQLiteAgentStore
from core.ai_kernel.product_admission import admission_identity
from core.ai_kernel.sqlite_store import SQLiteAITurnStore

from ..research_reads import COLLECTION, read_dependencies
from ..source_evidence_refs import READS
from ..transaction_records import TransactionRecords
from .material_origins import ORIGIN_KIND
from .outcome_source_reads import SourceReadRecords
from .shared_auxiliary import current_qualified_commit_scope
from .shared_query import SharedQueryFactory
from .task_attachment_input import _agent_owner


PREPARED_KIND = 'shared-task-read-prepared-v1-'
SHARED_FIELDS = frozenset({'schema_version', 'root_turn_id', 'root_request_identity',
    'material_proof', 'binding', 'output', 'prepared_ref'})


class SharedTaskReads:
    def __init__(self, application):
        self.application = application
        self.records = application.state.recognition_records

    def _stores(self):
        turns = self.application.state.ai_turn_store
        agents = self.application.state.agent_runtime_composition.store
        if (not isinstance(turns, SQLiteAITurnStore) or not isinstance(agents, SQLiteAgentStore)
                or Path(turns._path).resolve() != Path(agents._path).resolve()):
            raise RecognitionConflict('shared_task_read_native_owner_changed')
        return turns, agents

    def _native_root(self, request):
        turns, agents = self._stores()
        current = turns.get_request(request.get('turn_id'))
        if current is None:
            raise RecognitionConflict('shared_task_read_native_request_unavailable')
        if (request.get('scope') != current['scope']
                or 'privacy' in request and request['privacy'] != current['privacy']):
            raise RecognitionConflict('shared_task_read_native_request_changed')
        seen = set()
        while True:
            identity = current['turn_id']
            if identity in seen or len(seen) >= 256 or current['scope'] != request['scope']:
                raise RecognitionConflict('shared_task_read_ancestry_changed')
            seen.add(identity)
            _agent_owner(turns, agents, current)
            parent_id = current.get('agent_binding', {}).get('parent_run_id')
            if parent_id is None:
                break
            parent = agents.get_run(parent_id)
            if parent is None or parent.project_id != current['scope']['project_id']:
                raise RecognitionConflict('shared_task_read_parent_unavailable')
            current = turns.get_request(parent.turn_id)
            if current is None:
                raise RecognitionConflict('shared_task_read_parent_unavailable')
        bundle = turns.restore_admission(current)
        origins = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
        if len(origins) > 1:
            raise RecognitionConflict('shared_task_read_root_changed')
        return current, bool(origins)

    def root(self, request):
        root, shared = self._native_root(request)
        return root if shared and root['desired_outcome'] == 'project.task' else None

    def provider_root(self, request):
        root, shared = self._native_root(request)
        if shared and root['desired_outcome'] != 'project.task':
            raise RecognitionConflict('shared_task_read_root_changed')
        return root if shared else None

    def _freshness(self):
        freshness = self.application.state.product_outcome_freshness
        if freshness.application is not self.application or freshness.records is not self.records:
            raise RecognitionConflict('shared_task_read_actor_owner_changed')
        return freshness

    @contextmanager
    def _query(self, root, *, reader=None):
        factory = self.application.state.shared_query_factory
        if not isinstance(factory, SharedQueryFactory):
            raise RecognitionConflict('shared_task_read_factory_unavailable')
        with self._freshness().source_scope(request=root, reader=reader) as query:
            if (query is None or query.origin_factory is not factory
                    or query.receipt_records is not self.records
                    or query.execution_owner.records is not self.records):
                raise RecognitionConflict('shared_task_read_query_owner_changed')
            query.origin_admission.restore(root)
            yield query

    @contextmanager
    def qualified_sources(self, request, *, validator=None):
        root = self.root(request)
        if root is None:
            raise RecognitionConflict('shared_task_read_root_unavailable')
        from .shared_task_materials import read_task_material_item
        with self._query(root) as query, self._freshness()._read_scope(request=root) as reader:
            read_task_material_item(query, root, reader=reader)
            turns, agents = self._stores()
            for proof in read_dependencies(reader, turns, agents, request['turn_id'], root['scope']['project_id']):
                self.validate_proof(proof, reader=reader, root_request=root)
            if validator is not None:
                validator(reader, request)
            yield reader

    def invoke(self, capability, inner, request):
        if capability not in READS:
            raise RecognitionConflict('shared_task_read_capability_changed')
        root = self.root(request)
        if root is None:
            raise RecognitionConflict('shared_task_read_root_unavailable')
        identity = request.get('tool_call_id')
        if not isinstance(identity, str) or not identity:
            raise RecognitionConflict('shared_task_read_tool_identity_unavailable')
        turns, _ = self._stores()
        with self.qualified_sources(turns.get_request(request['turn_id'])) as reader:
            saved = reader.read(COLLECTION, identity)
            if saved is not None:
                proof = {'id': saved.object_id, 'revision': saved.revision, **saved.payload}
                if (proof.get('turn_id') != request['turn_id'] or proof.get('capability_id') != capability
                        or proof.get('shared_task_read', {}).get('binding', {}).get('provider_arguments') != _arguments(request)):
                    raise RecognitionConflict('shared_task_read_request_changed')
                self._validate_proof(proof, reader=reader, root_request=root, require_completion=False)
                return deepcopy(proof['shared_task_read']['output'])
        # 检索的实际模型调用在写入证明的短事务外执行。
        with self._query(root) as query:
            issued = query
        if capability in {'memory.recall', 'workbench.question.answer'}:
            output, binding = _recall(self, issued, root, request, capability)
        else:
            output = binding = None
        with self._query(root) as query, self.records.begin() as transaction:
            reader = self._freshness()._reader(transaction)
            if not isinstance(reader, SourceReadRecords):
                raise RecognitionConflict('shared_task_read_source_scope_unavailable')
            if output is None:
                output, binding = _domain_read(self, query, root, reader, capability, inner, request)
            proof = _capture(self, query, root, reader, capability, request, output, binding)
            existing = transaction.read(COLLECTION, proof['tool_call_id'])
            if existing is not None and existing.payload != proof:
                raise RecognitionConflict('research read authority cannot be rebound')
            if existing is None:
                transaction.put(COLLECTION, proof['tool_call_id'], proof, expected_revision=0)
            transaction.commit()
        return output

    def materials(self, proof):
        selected = proof.get('shared_task_read')
        if not isinstance(selected, dict) or set(selected) != SHARED_FIELDS:
            raise RecognitionConflict('shared_task_read_proof_changed')
        return deepcopy(selected['material_proof']['selection']['origin_materials'])

    def aliases(self, proof):
        from ..source_evidence_refs import qualified_ref_material
        selected = proof['shared_task_read']
        materials = self.materials(proof)
        references = selected['output']['evidence_refs']
        if not isinstance(references, list) or any(not isinstance(item, str) for item in references):
            raise RecognitionConflict('shared_task_read_reference_changed')
        if selected['binding']['kind'] == 'candidate':
            if len(materials) != 1:
                raise RecognitionConflict('shared_task_read_reference_owner_changed')
            return [{'ref': reference, 'material': deepcopy(materials[0])} for reference in references]
        return [{'ref': reference, 'material': deepcopy(qualified_ref_material(reference, materials))}
            for reference in references]

    def qualified_proof(self, proof, *, reader=None, root_request=None):
        from ..research_reads import completed_read_result
        self.validate_proof(proof, reader=reader, root_request=root_request)
        turns, _ = self._stores()
        completed = completed_read_result(turns, proof)
        return {'entry': {'kind': 'read_proof', 'scope': {'user_id': 'local-user', 'project_id': proof['project_id']},
            'product_turn_id': proof['shared_task_read']['root_turn_id'], 'turn_id': proof['turn_id'],
            'tool_call_id': proof['id'], 'proof_revision': proof['revision'], 'capability_id': proof['capability_id'],
            'completed_sequence': completed['completed_sequence'], 'outcome_ref': completed['outcome_ref'],
            'result_ref': completed['result_ref']}, 'materials': self.materials(proof), 'aliases': self.aliases(proof),
            'material_proof': deepcopy(proof['shared_task_read']['material_proof'])}

    def validate_proof(self, proof, *, reader=None, root_request=None):
        return self._validate_proof(proof, reader=reader, root_request=root_request, require_completion=True)

    def _validate_proof(self, proof, *, reader=None, root_request=None, require_completion):
        from .shared_task_materials import validate_task_materials
        if (proof.get('id') != proof.get('tool_call_id') or type(proof.get('revision')) is not int
                or proof['revision'] != 1):
            raise RecognitionConflict('shared_task_read_record_changed')
        turns, _ = self._stores()
        request = turns.get_request(proof.get('turn_id'))
        if request is None:
            raise RecognitionConflict('shared_task_read_native_request_unavailable')
        root = self.root(request)
        if root is None or root_request is not None and root != root_request:
            raise RecognitionConflict('shared_task_read_root_changed')
        selected = proof.get('shared_task_read')
        if (not isinstance(selected, dict) or set(selected) != SHARED_FIELDS
                or type(selected['schema_version']) is not int or selected['schema_version'] != 1
                or selected['root_turn_id'] != root['turn_id']
                or selected['root_request_identity'] != admission_identity(root)
                or proof.get('project_id') != root['scope']['project_id'] or proof.get('capability_id') not in READS
                or proof.get('source_egress') is not None or proof.get('documents') != []
                or proof.get('style_sources') != [] or proof.get('configurations') != []):
            raise RecognitionConflict('shared_task_read_proof_changed')
        prepared = {key: value for key, value in selected.items() if key != 'prepared_ref'}
        immutable = turns.get_immutable_payload(request['turn_id'], PREPARED_KIND + proof['tool_call_id'])
        expected = {'turn_id': request['turn_id'], 'tool_call_id': proof['tool_call_id'],
            'capability_id': proof['capability_id'], 'prepared': prepared}
        if immutable != (selected['prepared_ref'], expected):
            raise RecognitionConflict('shared_task_read_preparation_changed')
        if require_completion:
            from ..research_reads import completed_read_result
            completed = completed_read_result(turns, proof)
            if (completed['result'] != selected['output'].get('result')
                    or completed['evidence_refs'] != selected['output'].get('evidence_refs')):
                raise RecognitionConflict('shared_task_read_completed_body_changed')
        with self._query(root, reader=reader) as query:
            if reader is None:
                with self._freshness()._read_scope(request=root) as actual:
                    self._validate_proof(proof, reader=actual, root_request=root, require_completion=require_completion)
                return
            row = reader.read(COLLECTION, proof['tool_call_id'])
            if (row is None or row.revision != proof['revision']
                    or row.payload != {key: value for key, value in proof.items() if key not in {'id', 'revision'}}):
                raise RecognitionConflict('shared_task_read_record_changed')
            validate_task_materials(query, root, selected['material_proof'], reader=reader)
            _validate_binding(self, query, root, reader, proof['capability_id'], selected)


def _capture(service, query, root, reader, capability, request, output, binding):
    from .shared_task_materials import capture_task_materials
    identity = request.get('tool_call_id')
    if not isinstance(identity, str) or not identity:
        raise RecognitionConflict('shared_task_read_tool_identity_unavailable')
    binding = {**deepcopy(binding), 'provider_arguments': deepcopy(dict(_arguments(request)))}
    prepared = {'schema_version': 1, 'root_turn_id': root['turn_id'],
        'root_request_identity': admission_identity(root),
        'material_proof': capture_task_materials(query, root, binding['materials'],
            local_only=root['privacy']['allow_remote'] is not True, reader=reader),
        'binding': deepcopy(binding), 'output': deepcopy(output)}
    _validate_binding(service, query, root, reader, capability, prepared)
    turns, _ = service._stores()
    reference = turns.get_or_create_immutable_payload(request['turn_id'], PREPARED_KIND + identity,
        {'turn_id': request['turn_id'], 'tool_call_id': identity, 'capability_id': capability, 'prepared': prepared})
    return {'turn_id': request['turn_id'], 'tool_call_id': identity,
        'project_id': root['scope']['project_id'], 'capability_id': capability,
        'source_egress': None, 'documents': [], 'style_sources': [], 'configurations': [],
        'shared_task_read': {**prepared, 'prepared_ref': reference}}


def _material(kind, identity, revision, project, namespace):
    from ..source_evidence_refs import qualified_material_ref
    selected = {'type': kind, 'id': identity, 'revision': revision,
        'project_id': project, 'namespace': namespace}
    qualified_material_ref(selected)
    return selected


def _recall(service, query, root, request, capability):
    from ..retrieval_models import retrieval_authority
    from .task_recall import TaskRecall
    turns, agents = service._stores()
    native = turns.get_request(request['turn_id'])
    if capability == 'workbench.question.answer':
        from core.product_core.workbench_direct_question import _required_question
        request = {**request, 'arguments': {**_arguments(request),
            'query': _required_question(_required(_arguments(request).get('query')))}}

    def validate_parent():
        context = request.get('execution_context')
        if context is not None:
            context.checkpoint()
        with service.qualified_sources(native):
            pass

    with retrieval_authority(validate_parent):
        output, selected = TaskRecall(query, turns, None, agents=agents)._prepare(request)
        validate_parent()
    materials = [_material({'source': 'original_source'}.get(item['kind'], item['kind']),
        item['id'], item['revision'], item['project_id'], item['namespace'])
        for item in selected['materials']]
    for document in selected['documents']:
        material = _material('document', document['id'], document['revision'],
            document['project_id'], document['namespace'])
        if material not in materials:
            materials.append(material)
    binding = {'kind': 'recall', 'materials': materials, 'recall': selected,
        'query': request['arguments']['query'].strip(), 'turn_id': request['turn_id'],
        'tool_call_id': request['tool_call_id'], 'project_id': root['scope']['project_id']}
    if capability == 'workbench.question.answer':
        binding['kind'] = 'question'
        output = _question_output(output, binding)
    return output, binding


def _recall_output(plan, limit, materials):
    from ..source_evidence_refs import qualified_material_ref
    from .material_origins import SharedProjectLease
    results = []
    source_namespace = SharedProjectLease.from_payload(plan['origin_lease']).namespace
    selected = plan['chosen'][:max(0, limit - len(plan['profile']['items']))]
    for candidate in selected:
        entry = candidate['entry']
        namespace = candidate.get('namespace', source_namespace)
        material = _material({'source': 'original_source'}.get(candidate['kind'], candidate['kind']),
            entry['id'], entry['revision'], candidate['scope'].project_id, namespace)
        if material not in materials:
            raise RecognitionConflict('shared_task_recall_material_changed')
        results.append({'object_id': entry['id'], 'layer': candidate['layer'], 'title': candidate['title'],
            'content': candidate['excerpt'], 'source_refs': [qualified_material_ref(material)],
            'trust_status': 'published', 'score': candidate['score'], 'bookshelf': bool(candidate.get('bookshelf'))})
    for profile in plan['profile']['items']:
        matches = [item for item in materials if item['type'] == 'recognition' and item['id'] == profile['id']
            and item['revision'] == profile['revision'] and item['project_id'] == 'me']
        if len(matches) != 1:
            raise RecognitionConflict('shared_task_recall_profile_changed')
        results.append({'object_id': profile['id'], 'layer': 'persona', 'title': '已确认画像',
            'content': profile['content'], 'source_refs': [qualified_material_ref(matches[0])],
            'trust_status': 'published', 'score': 1, 'bookshelf': False})
    return {'summary': f'recalled {len(results)} evidence items', 'payload_ref': None, 'receipt_ref': None,
        'evidence_refs': [ref for item in results for ref in item['source_refs']], 'result': results}


def _question_output(recalled, binding):
    from core.ai_kernel import validate_turn_presentation_artifact
    from core.product_core.workbench_direct_question import (
        WorkbenchDirectQuestionResult, _RecallOutcome, _local_answer, serialize_workbench_direct_question,
    )
    evidence = tuple({'layer': item['layer'], 'object_id': item['object_id'], 'score': item['score'],
        'quality': item['trust_status'], 'snippet': item['content'], 'target_ref': item['source_refs'][0],
        'source_refs': [{'source_id': item['object_id'], 'locator': item['source_refs'][0]}]}
        for item in recalled['result'])
    outcome = _RecallOutcome(status='recalled' if evidence else 'insufficient_evidence',
        evidence_refs=evidence, evidence_count=len(evidence))
    answer = _local_answer(binding['query'], recall_outcome=outcome)
    value = WorkbenchDirectQuestionResult(status='answered', question_id=binding['turn_id'],
        answer_id=binding['tool_call_id'], question=binding['query'], answer_preview=answer,
        qa_mode='direct_local_answer', knowledge_base_write=False, source_created=False,
        job_created=False, library_item_created=False, memory_publication_state='not_published',
        blocked_operations=('source_creation', 'job_creation', 'library_item_creation', 'long_term_memory_publication',
            'remote_model_provider_execution', 'api_key_use'), recall_status=outcome.status,
        evidence_refs=evidence, evidence_count=len(evidence), provider_call_performed=False,
        provider_status='fallback_provider_unavailable' if evidence else 'not_attempted_no_evidence')
    presentation = serialize_workbench_direct_question(value)
    presentation['source_links'] = [{'source_id': item['object_id'], 'locator': item['source_refs'][0],
        'ref': item['source_refs'][0]} for item in recalled['result']]
    presentation['project_route'] = {'status': 'explicit', 'selected_project_id': binding['project_id'], 'ai_assist': None}
    artifact = validate_turn_presentation_artifact({'schema_version': '1.0.0',
        'kind': 'workbench.question.answer', 'content': presentation})
    return {'summary': answer, 'payload_ref': None, 'receipt_ref': None,
        'evidence_refs': recalled['evidence_refs'], 'result': artifact}


def _origin_reader(query, reader, material):
    return _origin_context(query, reader, material['namespace'], material['project_id'])


def _origin_context(query, reader, namespace, project):
    from .material_origin_admission import BoundMaterialOrigins
    origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})
    owner = origins.owner(project, namespace)
    scope = current_qualified_commit_scope()
    if scope is None:
        raise RecognitionConflict('shared_task_read_source_scope_unavailable')
    actual = TransactionRecords(scope.reader_for(origins, owner, actor_reader=TransactionRecords(reader)))
    authority_owner = (query.material_owner if namespace == query.source_namespace
        else query.execution_owner.records)
    return owner, actual, authority_owner


def _validate_recall(query, reader, binding):
    from ..kernel.answer_continuations import decode
    from .material_origin_sql import _validate_primary_body
    selected = binding['recall']
    frozen = decode(selected['shared_selection'])
    if not isinstance(frozen, dict) or set(frozen) != {'plan', 'guards', 'limit'}:
        raise RecognitionConflict('shared_task_recall_selection_changed')
    plan, limit = frozen['plan'], frozen['limit']
    if (type(limit) is not int or not 0 <= limit <= 12 or plan['project_id'] != query.origin_admission.lease.project_id
            or plan['question'] != binding['query']):
        raise RecognitionConflict('shared_task_recall_selection_changed')
    from .model_material_coverage import validate_primary_guards
    validate_primary_guards(query, plan, frozen['guards'], {})
    query.validate_ask_plan(plan)
    for material in binding['materials']:
        owner, actual, authority_owner = _origin_reader(query, reader, material)
        _validate_primary_body(query, owner, actual, material, plan,
            authority_owner=authority_owner, strict_excerpt=True)
    for selected in binding['recall']['preferences']:
        if selected['collection'] not in {'recognition_recall_preferences', 'v2_document_recall'}:
            raise RecognitionConflict('shared_task_recall_preference_changed')
        namespace = selected['namespace']
        project = 'me' if namespace == query.origin_admission.personal['namespace'] else plan['project_id']
        _, actual, _ = _origin_context(query, reader, namespace, project)
        row = actual.read(selected['collection'], selected['id'])
        if (0 if row is None else row.revision) != selected['revision']:
            raise RecognitionConflict('shared_task_recall_preference_changed')
    return _recall_output(plan, limit, binding['materials'])


def _arguments(request):
    value = request.get('arguments')
    if not isinstance(value, Mapping):
        raise RecognitionConflict('shared_task_read_arguments_unavailable')
    return value


def _required(value):
    if not isinstance(value, str) or not value.strip():
        raise RecognitionConflict('shared_task_read_argument_unavailable')
    return value.strip()


def _store(query, namespace):
    if namespace == query.source_namespace:
        return query.sources
    if namespace == query.origin_admission.personal['namespace']:
        return query.execution_owner.sources
    raise RecognitionConflict('shared_task_read_configuration_owner_changed')


def _configuration(query, namespace, collection, identity):
    store = _store(query, namespace)
    return {'namespace': namespace, 'collection': collection, 'id': identity,
        'revision': store.revision(collection, identity), 'payload': deepcopy(store.read(collection, identity))}


def _validate_configurations(query, configurations):
    for frozen in configurations:
        if (not isinstance(frozen, dict) or set(frozen) != {'namespace', 'collection', 'id', 'revision', 'payload'}
                or frozen['collection'] not in {'project_skills', 'source_content_reads', 'media_processing_outputs'}
                or _configuration(query, frozen['namespace'], frozen['collection'], frozen['id']) != frozen):
            raise RecognitionConflict('shared_task_read_configuration_changed')


def _configuration_materials(query, root, reader, configuration):
    from backend.recognition import WorkScope
    from ..source_evidence_refs import _source_refs, roots_for
    body = configuration['payload']
    if body is None:
        return []
    if (not isinstance(body, dict) or configuration['collection'] == 'project_skills'
            and body.get('project_id') != root['scope']['project_id']):
        raise RecognitionConflict('shared_task_read_configuration_changed')
    if not isinstance(body.get('source_refs', []), list) or not isinstance(body.get('output_rules', []), list):
        raise RecognitionConflict('shared_task_read_configuration_changed')
    raw_refs = list(body.get('source_refs', []))
    for rule in body.get('output_rules', []):
        if not isinstance(rule, dict):
            raise RecognitionConflict('shared_task_read_configuration_changed')
        if not isinstance(rule.get('source_refs', []), list):
            raise RecognitionConflict('shared_task_read_configuration_changed')
        raw_refs.extend(rule.get('source_refs', []))
    namespace = configuration['namespace']
    _, actual, _ = _origin_context(query, reader, namespace, root['scope']['project_id'])
    materials, refs = [], []
    for reference in raw_refs:
        locator = reference.get('locator') if isinstance(reference, dict) else reference
        if isinstance(locator, str) and locator.startswith('crp://'):
            material = _qualified_reference_material(query, root, reader, locator)
            if isinstance(reference, dict) and reference.get('source_id') != material['id']:
                raise RecognitionConflict('shared_task_read_reference_changed')
            if material not in materials:
                materials.append(material)
        elif isinstance(reference, dict) and (locator == 'published_memory:summary'
                or locator == 'document:summary' or isinstance(locator, str) and locator.startswith('document://')):
            identity = _required(reference.get('source_id'))
            kind = 'recognition' if locator == 'published_memory:summary' else 'document'
            collection = 'recognitions' if kind == 'recognition' else 'documents'
            row = actual.read(collection, identity)
            if (row is None or kind == 'document' and isinstance(locator, str) and locator.startswith('document://')
                    and locator != 'document://' + identity):
                raise RecognitionConflict('shared_task_read_reference_changed')
            material = _material(kind, identity, row.revision, root['scope']['project_id'], namespace)
            if material not in materials:
                materials.append(material)
        else:
            refs.extend(_source_refs([reference]))
    roots = roots_for(actual, WorkScope('local-user', root['scope']['project_id']), refs)
    for item in roots:
        material = _material(item['type'], item['id'], item['revision'], root['scope']['project_id'], namespace)
        if material not in materials:
            materials.append(material)
    return materials


def _qualified_reference_material(query, root, reader, reference):
    from ..source_evidence_refs import qualified_material_ref
    parsed = urlsplit(reference)
    if parsed.scheme != 'crp' or parsed.query or parsed.fragment:
        raise RecognitionConflict('shared_task_read_reference_changed')
    choices = []
    for namespace in (query.source_namespace, query.origin_admission.personal['namespace']):
        prefix = urlsplit('crp://' + quote(namespace, safe='/') + '/')
        if parsed.netloc == prefix.netloc and parsed.path.startswith(prefix.path):
            choices.append((namespace, parsed.path[len(prefix.path):].split('/')))
    if len(choices) != 1 or len(choices[0][1]) != 2:
        raise RecognitionConflict('shared_task_read_reference_owner_changed')
    namespace, (collection, encoded_id) = choices[0]
    kinds = {'recognitions': 'recognition', 'recognition_experiences': 'experience',
        'documents': 'document', 'sources': 'original_source', 'workspace_items': 'original_item'}
    if collection not in kinds:
        raise RecognitionConflict('shared_task_read_reference_changed')
    identity = unquote(encoded_id)
    if collection == 'sources':
        store = _store(query, namespace)
        body = store.read(collection, identity)
        if body is None:
            raise RecognitionConflict('shared_task_read_reference_unavailable')
        project, revision = body.get('project_id', 'default'), store.revision(collection, identity)
    else:
        actual = (reader.source_reader(query.material_owner) if namespace == query.source_namespace
            else TransactionRecords(reader))
        row = actual.read(collection, identity)
        if row is None:
            raise RecognitionConflict('shared_task_read_reference_unavailable')
        project = (row.payload.get('scope', {}).get('project_id') if collection in
            {'recognitions', 'recognition_experiences'} else row.payload.get('project_id'))
        revision = row.revision
    material = _material(kinds[collection], identity, revision, project, namespace)
    _origin_reader(query, reader, material)
    if qualified_material_ref(material) != reference:
        raise RecognitionConflict('shared_task_read_reference_changed')
    return material


def _domain_read(service, query, root, reader, capability, inner, request):
    arguments = _arguments(request)
    project = root['scope']['project_id']
    if capability == 'source.evidence.read':
        identity = _required(arguments.get('source_id'))
        body = query.sources.read('sources', identity)
        if body is None or body.get('project_id', 'default') != project:
            raise RecognitionConflict('shared_task_read_source_unavailable')
        material = _material('original_source', identity, query.sources.revision('sources', identity),
            project, query.source_namespace)
        configuration = _configuration(query, query.source_namespace, 'project_skills', project)
        materials = [material]
        for parent in _configuration_materials(query, root, reader, configuration):
            if parent not in materials:
                materials.append(parent)
        binding = {'kind': 'source', 'materials': materials, 'source': material,
            'template_type': _required(arguments.get('template_type')), 'configurations': [configuration]}
        return _source_output(body, binding), binding
    if capability == 'project_skill.evidence.read':
        return _skill_read(query, root, reader, arguments)
    if capability == 'memory.candidate.evidence.read':
        return _candidate_read(service, query, root, reader, inner, request)
    raise RecognitionConflict('shared_task_read_capability_changed')


def _source_output(body, binding):
    from core.product_core.source_template_document import prepare_source_document_ai_evidence
    from ..source_evidence_refs import qualified_material_ref
    skill = binding['configurations'][0]['payload']
    outline = skill.get('outline') if isinstance(skill, dict) else None
    evidence = prepare_source_document_ai_evidence(source=body, source_revision=binding['source']['revision'],
        template_type=binding['template_type'], outline_override=outline if isinstance(outline, list) and outline else None)
    if isinstance(skill, dict):
        evidence['project_skill_ref'] = {'project_id': binding['source']['project_id'], 'revision': skill.get('revision')}
    return {'summary': 'Source document evidence is ready', 'receipt_ref': None, 'payload_ref': None,
        'evidence_refs': [qualified_material_ref(binding['source'])], 'result': evidence}


def _skill_read(query, root, reader, arguments):
    from backend.recognition import WorkScope
    from core.document_engine import SQLiteDocumentRepository
    from core.product_core.project_skill_ai_authoring import build_project_skill_evidence_bundle
    from ..source_egress import recognition_service
    project = root['scope']['project_id']
    _, source_reader, authority_owner = _origin_context(query, reader, query.source_namespace, project)
    repository = SQLiteDocumentRepository(source_reader, namespace_id=query.material_owner.documents.namespace_id)
    sources = {item['id']: item for item in query.sources.list('sources') if item.get('project_id', 'default') == project}
    documents = {item['id']: {**item, 'markdown': repository.markdown(item['id'], revision=item['revision'])}
        for item in repository.list() if item.get('project_id') == project and item.get('status') != 'archived'}
    service = recognition_service(source_reader, source_owner=authority_owner)
    memories = {item.id: _memory_value(source_reader.read('recognitions', item.id).payload,
        _material('recognition', item.id, item.revision, project, query.source_namespace))
        for item in service.list_recognitions(scope=WorkScope('local-user', project)) if item.authorized}
    configuration = _configuration(query, query.source_namespace, 'project_skills', project)
    bundle = build_project_skill_evidence_bundle(project_id=project, sources=tuple(sources.values()),
        documents=tuple(documents.values()), memories=tuple(memories.values()), current_skill=configuration['payload'])
    if bundle.insufficient_evidence:
        raise RecognitionConflict('shared_task_read_project_evidence_unavailable')
    materials = []
    for item in bundle.items:
        identity, kind = item['object_id'], item['kind']
        if kind == 'source':
            material = _material('original_source', identity, query.sources.revision('sources', identity), project, query.source_namespace)
        elif kind == 'document':
            material = _material('document', identity, documents[identity]['revision'], project, query.source_namespace)
        elif kind == 'published_memory':
            material = _material('recognition', identity, memories[identity]['revision'], project, query.source_namespace)
        elif kind == 'current_project_skill':
            continue
        else:
            raise RecognitionConflict('shared_task_read_project_evidence_changed')
        materials.append(material)
    if not materials:
        raise RecognitionConflict('shared_task_read_project_evidence_unavailable')
    evidence_materials = deepcopy(materials)
    for parent in _configuration_materials(query, root, reader, configuration):
        if parent not in materials:
            materials.append(parent)
    binding = {'kind': 'skill', 'materials': materials, 'evidence_materials': evidence_materials, 'project_id': project,
        'goal': _required(arguments.get('goal')), 'operation': arguments.get('operation') if isinstance(arguments.get('operation'), str) else None,
        'configurations': [configuration]}
    bodies = [{'material': material, 'payload': (sources[material['id']] if material['type'] == 'original_source'
        else documents[material['id']] if material['type'] == 'document' else memories[material['id']]),
        'markdown': documents[material['id']]['markdown'] if material['type'] == 'document' else None}
        for material in evidence_materials]
    return _skill_output(bodies, binding), binding


def _skill_output(bodies, binding):
    from core.product_core.project_skill_ai_authoring import build_project_skill_evidence_bundle, project_skill_ai_user_payload
    from ..source_evidence_refs import qualified_material_ref
    sources, documents, memories = [], [], []
    for item in bodies:
        material = item['material']
        if material not in binding['evidence_materials']:
            continue
        value = {**item['payload'], 'id': material['id'], 'project_id': material['project_id'], 'revision': material['revision']}
        if material['type'] == 'original_source':
            sources.append(value)
        elif material['type'] == 'document':
            documents.append({**value, 'markdown': item['markdown']})
        elif material['type'] == 'recognition':
            memories.append(_memory_value(value, material))
        else:
            raise RecognitionConflict('shared_task_read_project_evidence_changed')
    current = binding['configurations'][0]['payload']
    bundle = build_project_skill_evidence_bundle(project_id=binding['project_id'], sources=sources,
        documents=documents, memories=memories, current_skill=current)
    if bundle.insufficient_evidence:
        raise RecognitionConflict('shared_task_read_project_evidence_unavailable')
    model_input = project_skill_ai_user_payload(project_id=binding['project_id'], goal=binding['goal'],
        operation=binding['operation'], evidence=bundle, current_skill=current)
    return {'summary': f'resolved {len(bundle.items)} Project Skill evidence items', 'receipt_ref': None,
        'payload_ref': None, 'evidence_refs': [qualified_material_ref(item) for item in binding['evidence_materials']],
        'result': {'schema_version': '1.0.0', 'kind': 'project_skill.evidence', 'project_id': binding['project_id'],
            'goal': binding['goal'], 'operation': binding['operation'],
            'expected_project_skill_revision': 0 if current is None else current.get('revision'),
            'model_input': model_input, 'source_refs': [dict(item) for item in bundle.source_refs]}}


def _memory_value(payload, material):
    from ..context_adapter import format_recognition_content
    value = {**payload, 'id': material['id'], 'revision': material['revision'], 'project_id': material['project_id']}
    return {**value, 'summary': format_recognition_content(value)}


def _candidate_read(service, query, root, reader, inner, request):
    from .shared_task_materials import capture_task_materials
    arguments = _arguments(request)
    grant_id = _required(arguments.get('grant_id', arguments.get('evidence_id')))
    store = service.application.state.four_layer_memory_candidate_evidence_grant_store
    grant = store.inspect(grant_id)
    project = root['scope']['project_id']
    if grant.project_id != project:
        raise RecognitionConflict('shared_task_read_candidate_scope_changed')
    namespace = query.origin_admission.personal['namespace']
    actor_store = _store(query, namespace)
    source = actor_store.read('sources', grant.source_id)
    if source is None or source.get('project_id', 'default') != project:
        raise RecognitionConflict('shared_task_read_candidate_source_unavailable')
    material = _material('original_source', grant.source_id, actor_store.revision('sources', grant.source_id), project, namespace)
    # 旧授权由本应用签发，先核操作人原件的真实共享逻辑出生，再沿原提供者读取。
    capture_task_materials(query, root, [material], local_only=root['privacy']['allow_remote'] is not True, reader=reader)
    collection = {'source_content_read': 'source_content_reads', 'media_processing_output': 'media_processing_outputs'}.get(grant.evidence_kind)
    if collection is None:
        raise RecognitionConflict('shared_task_read_candidate_kind_changed')
    configuration = _configuration(query, namespace, collection, grant.evidence_id)
    output = inner.invoke(request)
    if store.inspect(grant_id) != grant:
        raise RecognitionConflict('shared_task_read_candidate_grant_changed')
    binding = {'kind': 'candidate', 'materials': [material], 'grant': deepcopy(grant.public()),
        'evidence_id': grant.evidence_id, 'evidence_kind': grant.evidence_kind, 'source_id': grant.source_id,
        'configurations': [configuration]}
    _validate_candidate_output(output, binding)
    return output, binding


def _validate_candidate_output(output, binding):
    result = output.get('result')
    record = binding['configurations'][0]['payload']
    revision = record.get('revision') if isinstance(record, dict) else None
    revision = revision if type(revision) is int and revision > 0 else 1
    if (not isinstance(record, dict) or record.get('status') != 'completed'
            or record.get('source_id') != binding['source_id'] or not isinstance(record.get('preview'), str)
            or not isinstance(result, dict) or result.get('kind') != 'memory.candidate.evidence'
            or result.get('evidence_id') != binding['evidence_id'] or result.get('source_id') != binding['source_id']
            or result.get('evidence_kind') != binding['evidence_kind'] or result.get('grant') != binding['grant']
            or result.get('baseline', {}).get('evidence_revision') != revision
            or not isinstance(result.get('refs'), list) or output.get('evidence_refs') != result['refs']):
        raise RecognitionConflict('shared_task_read_candidate_evidence_changed')


def _validate_binding(service, query, root, reader, capability, selected):
    binding, material_proof = selected['binding'], selected['material_proof']
    if (not isinstance(binding, dict) or binding.get('materials') != material_proof['selection']['origin_materials']
            or [item['material'] for item in material_proof['bodies']] != binding['materials']):
        raise RecognitionConflict('shared_task_read_material_binding_changed')
    kind = {'memory.recall': 'recall', 'workbench.question.answer': 'question',
        'source.evidence.read': 'source', 'project_skill.evidence.read': 'skill',
        'memory.candidate.evidence.read': 'candidate'}[capability]
    fields = {
        'recall': {'kind', 'materials', 'recall', 'query', 'turn_id', 'tool_call_id', 'project_id'},
        'question': {'kind', 'materials', 'recall', 'query', 'turn_id', 'tool_call_id', 'project_id'},
        'source': {'kind', 'materials', 'source', 'template_type', 'configurations'},
        'skill': {'kind', 'materials', 'evidence_materials', 'project_id', 'goal', 'operation', 'configurations'},
        'candidate': {'kind', 'materials', 'grant', 'evidence_id', 'evidence_kind', 'source_id', 'configurations'},
    }
    if (binding.get('kind') != kind or set(binding) != fields[kind] | {'provider_arguments'}
            or not isinstance(binding['provider_arguments'], dict)):
        raise RecognitionConflict('shared_task_read_capability_binding_changed')
    arguments = binding['provider_arguments']
    if kind in {'recall', 'question'}:
        expected = _required(arguments.get('query'))
        if kind == 'question':
            from core.product_core.workbench_direct_question import _required_question
            expected = _required_question(expected)
        if expected != binding['query']:
            raise RecognitionConflict('shared_task_read_request_changed')
    elif kind == 'source' and (binding['source']['id'] != _required(arguments.get('source_id'))
            or binding['template_type'] != _required(arguments.get('template_type'))):
        raise RecognitionConflict('shared_task_read_request_changed')
    elif kind == 'skill' and (binding['goal'] != _required(arguments.get('goal'))
            or binding['operation'] != (arguments.get('operation') if isinstance(arguments.get('operation'), str) else None)):
        raise RecognitionConflict('shared_task_read_request_changed')
    elif kind == 'candidate' and binding['grant'].get('grant_id') != _required(arguments.get('grant_id', arguments.get('evidence_id'))):
        raise RecognitionConflict('shared_task_read_request_changed')
    _validate_configurations(query, binding.get('configurations', []))
    if kind in {'source', 'skill'}:
        materials = [binding['source']] if kind == 'source' else deepcopy(binding['evidence_materials'])
        for configuration in binding['configurations']:
            for material in _configuration_materials(query, root, reader, configuration):
                if material not in materials:
                    materials.append(material)
        if materials != binding['materials']:
            raise RecognitionConflict('shared_task_read_configuration_materials_changed')
    if kind in {'recall', 'question'}:
        output = _validate_recall(query, reader, binding)
        if kind == 'question':
            output = _question_output(output, binding)
    elif kind == 'source':
        bodies = [item for item in material_proof['bodies'] if item['material'] == binding['source']]
        if len(bodies) != 1:
            raise RecognitionConflict('shared_task_read_source_changed')
        output = _source_output(bodies[0]['payload'], binding)
    elif kind == 'skill':
        output = _skill_output(material_proof['bodies'], binding)
    else:
        _validate_candidate_output(selected['output'], binding)
        output = selected['output']
    if output != selected['output']:
        raise RecognitionConflict('shared_task_read_rendered_body_changed')
