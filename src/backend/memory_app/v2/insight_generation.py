"""Generate bounded, reviewable insights from an admitted document revision."""
import logging
from copy import deepcopy
from dataclasses import replace
from functools import wraps

from backend.recognition import (
    CANDIDATE_GENERATION_STEP_VERSION, RecognitionConflict, WorkScope, normalize_conditions,
)
from ..document_recognition import CANDIDATE_SOURCE_CONSTRAINTS, ensure_document_experience
from ..generation_sources import generation_source_guard
from ..structured_generation import InsightOutput, InvalidStructuredOutput
from ..source_egress import SourceEgressService
from .insights import insight_view
from .privacy import is_private_project
from .projects import assign_scene, scene_of
from .memory_turn import MemoryTurn
from .policies import get, version, override
from .policies.types import ExtractInput, ExtractDecodeInput
from .policies.pipelines import versions_for_turn


_LOGGER = logging.getLogger(__name__)


class _MaterialBudgetError(ValueError):
    pass


def _material_capacity(models, records, turn):
    from ..kernel.aux_routing import CHOICE_KIND, CHOICES
    from ..turn_routing import CONFIGURATION_FIELDS
    # generate前内核Turn尚未建立，直接读取其已暂存选择，不能提前写内核payload。
    saved = turn.store.get_immutable_payload(turn.turn_id, CHOICE_KIND)
    staged = records.read(CHOICES, turn.turn_id)
    if staged is not None and staged.revision != 1:
        raise _MaterialBudgetError('insight_auxiliary_binding_changed')
    binding = saved[1] if saved is not None else staged.payload if staged is not None else None
    if saved is not None and staged is not None and saved[1] != staged.payload:
        raise _MaterialBudgetError('insight_auxiliary_binding_changed')
    selected = models.for_auxiliary(binding) if binding is not None else models
    configuration = selected.public()['generation']
    reader = getattr(selected, 'generation_budget_limits', None)
    limits = reader(expected_revision=configuration.get('revision'), max_tokens=None) if callable(reader) else None
    if (not isinstance(limits, dict) or type(limits.get('window')) is not int
            or type(limits.get('reserve')) is not int or not 0 < limits['reserve'] < limits['window']):
        raise _MaterialBudgetError('insight_budget_unavailable')
    return {'binding': deepcopy(binding),
        'configuration': {key: deepcopy(configuration.get(key)) for key in (
            *CONFIGURATION_FIELDS, 'subscription_binding')},
        'limits': {'window': limits['window'], 'reserve': limits['reserve']}}


def _prepare_material_input(models, records, turn, policy, request, response_model):
    from backend.shared.llm.message_metadata import (
        _estimate_input_tokens, _build_json_mode_messages, _build_prompt_fallback_messages,
    )
    capacity = _material_capacity(models, records, turn)
    output_budget = policy.budget(request)
    prepared = None
    for _ in range(3):
        current = replace(request, output_budget=output_budget)
        messages = policy.prepare(current)
        cost = max(_estimate_input_tokens(value) for value in (
            messages, _build_json_mode_messages(messages=messages, validation_error=None),
            _build_prompt_fallback_messages(messages=messages, response_model=response_model, validation_error=None)))
        try:
            projected = policy.budget(request, limits=capacity['limits'], input_tokens=cost)
        except ValueError as error:
            prepared = {**capacity, 'input_tokens': cost, 'output_budget': output_budget,
                'error': str(error)}
            break
        if projected == output_budget:
            prepared = {**capacity, 'input_tokens': cost, 'output_budget': projected, 'error': None}
            break
        output_budget = projected
    if prepared is None:
        raise _MaterialBudgetError('insight_budget_unstable')
    # 输入、真实aux选择和输出额度共同冻结；重试不能换预算偷偷再派模型。
    with records.begin() as tx:
        frozen = tx.read('v2_material_extract_budgets', turn.turn_id)
        if frozen is None:
            tx.put('v2_material_extract_budgets', turn.turn_id, prepared, expected_revision=0)
        elif frozen.revision != 1 or frozen.payload != prepared:
            raise _MaterialBudgetError('insight_material_budget_changed')
        tx.commit()
    if prepared['error']:
        raise _MaterialBudgetError(prepared['error'])
    return current, messages, prepared


def _read_material_analysis(records, documents, project_id, document_id, *, revision, reader=None):
    # 仅新版本需要材料分析；旧轮次重放不导入或读取新增旁路。
    from .material_analysis import read_document_analysis
    return read_document_analysis(records, documents, project_id, document_id,
        revision=revision, reader=reader)


def _read_material_sources(records, documents, project_id, document_id, *, revision, reader=None):
    from .material_analysis import read_document_analysis_sources
    return read_document_analysis_sources(records, documents, project_id, document_id,
        revision=revision, reader=reader)


def _checked_material_analysis(records, documents, project_id, document_id, *, revision, reader=None):
    analysis = _read_material_analysis(records, documents, project_id, document_id,
        revision=revision, reader=reader)
    if analysis is not None:
        # 独立从来源owner重建，不能用分析输入自报的正文范围或历史修订作证明。
        sources = _read_material_sources(records, documents, project_id, document_id,
            revision=revision, reader=reader)
        if (not isinstance(sources, dict) or set(sources) != {
                'body_sources', 'analysis_generation_refs', 'original_material_refs'}
                or any(not _same_material(analysis.get(key), sources[key]) for key in sources)):
            raise RecognitionConflict('insight_material_analysis_changed')
    return analysis


def _same_material(left, right):
    from .source_sections import _exact
    return _exact(left, right)


def _material_views(records, service, scope, candidate_ids, experience_id, generation_id):
    result = []
    for identity in candidate_ids:
        row = records.read('recognition_candidates', identity)
        if (row is None or row.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
                or row.payload.get('source_experience_ids') != [experience_id]
                or (row.payload.get('generation') or {}).get('id') != generation_id):
            raise RecognitionConflict('insight_candidate_identity_conflicted')
        view = insight_view(records, scope, identity, service=service)
        if view is not None and not (view['kind'] == 'candidate' and view['state'] == 'forgotten'):
            result.append(view)
    return result


def _error(code):
    # Provider exceptions and source bodies must never enter logs.
    _LOGGER.warning("%s", code)


def _policy_bound_generation(generate):
    @wraps(generate)
    def bound(*args, **kwargs):
        with override(**versions_for_turn('memory.propose_insights')):
            return generate(*args, **kwargs)
    return bound


@_policy_bound_generation
def generate_insights(models, service, documents, project_id, document_id, *, on_error=None, retry_token=None, attempt=None):
    """Return Insight DTOs; failures retain the admitted document unchanged."""
    def report(code):
        _error(code)
        if on_error is not None:
            on_error(code)

    records = service.records
    scope = WorkScope("local-user", project_id)
    def validate_attempt(reader=None):
        if attempt is None:
            return
        turn_id, run_id = attempt
        current = (reader or records).read("v2_turns", turn_id)
        if (current is None or current.payload.get("project_id") != project_id
                or current.payload.get("intent") != "remember"
                or current.payload.get("run_id") != run_id
                or current.payload.get("receipt", {}).get("remember", {}).get("state") != "processing"):
            raise RecognitionConflict("insight_attempt_replaced")
    validate_attempt()
    if is_private_project(records, project_id):
        report("insight_private_project")
        return []
    experience_id, revision = ensure_document_experience(documents, service, project_id, document_id)
    prefix = f"candidate-v2-{document_id}-r{revision}-"
    existing = [row for row in records.list("recognition_candidates")
        if row.object_id in {prefix + str(n) for n in (1, 2, 3)}
        and row.payload.get("scope") == {"user_id": scope.user_id, "project_id": project_id}]
    prior_turn = next((r for r in records.list("v2_memory_turn_keys") if r.payload["identity"] == {"kind": "memory.propose_insights", "project": project_id, "key": prefix}
                   ), None)
    def existing_views():
        return [view for row in sorted(existing, key=lambda row: row.object_id)
                if (view := insight_view(records, scope, row.object_id, service=service)) is not None
                and not (view['kind'] == 'candidate' and view['state'] == 'forgotten')]
    if existing and prior_turn is None:
        return existing_views()
    try:
        experience = service.read_candidate_experiences(scope=scope, experience_ids=[experience_id])[0]
        validate_sources = generation_source_guard(SourceEgressService(records), models, scope,
            [{"type": "experience", "id": experience_id, "revision": experience.revision}])
        comment_inputs = None
        material_analysis = None
        material_budget = None
        def validate_analysis(reader=None):
            if dynamic and not _same_material(_checked_material_analysis(records, documents, project_id, document_id,
                    revision=revision, reader=reader), material_analysis):
                raise RecognitionConflict('insight_material_analysis_changed')
        def validate():
            validate_attempt()
            validate_sources()
            if comment_inputs is not None:
                validate_comment_inputs(records, documents, project_id, comment_inputs)
            validate_analysis()
            if material_budget is not None and _material_capacity(models, records, turn) != {
                    key: material_budget[key] for key in ('binding', 'configuration', 'limits')}:
                raise RecognitionConflict('insight_material_budget_changed')
            if comparative and project_choices != public_projects(records, documents, models):
                raise RecognitionConflict('insight_project_choices_changed')
        selected = (prior_turn.payload['request'].get('policy_versions', {}).get('extract', '@1')
            if prior_turn else version('extract'))
        dynamic = selected == '@6'
        comments = selected in {'@3', '@4', '@5', '@6'}
        comparative = selected in {'@2', '@3', '@4', '@5', '@6'}
        if dynamic:
            current_analysis = _checked_material_analysis(records, documents, project_id, document_id, revision=revision)
            if prior_turn:
                frozen_extract = records.read('v2_extract_inputs', prior_turn.object_id)
                if frozen_extract is None or 'material_analysis' not in frozen_extract.payload:
                    raise RecognitionConflict('insight_material_analysis_unavailable')
                material_analysis = deepcopy(frozen_extract.payload['material_analysis'])
                if not _same_material(material_analysis, current_analysis):
                    raise RecognitionConflict('insight_material_analysis_changed')
            else:
                material_analysis = deepcopy(current_analysis)
        neighbors, project_choices = [], []
        if comparative:
            from .links import InsightLinks
            from .comparative_insights import public_projects, ComparativeOutput
            if prior_turn:
                neighbors = [records.read('recognitions', material['id'])
                    for material in prior_turn.payload['request']['privacy']['material_refs']
                    if material['type'] == 'recognition']
                if any(row is None for row in neighbors):
                    raise RecognitionConflict('insight_neighbor_unavailable')
            else:
                assignment = scene_of(records, 'document', document_id)
                neighbors = InsightLinks(records, service, models).extraction_neighbors(project_id, experience,
                    assignment['scene'] if assignment and assignment['project_id'] == project_id else None)
            project_choices = public_projects(records, documents, models)
        materials = [{"type": "experience", "id": experience_id,
                "revision": experience.revision, "project_id": project_id}] + [
            {'type': 'recognition', 'id': row.object_id, 'revision': row.revision,
             'project_id': row.payload['project_id']} for row in neighbors]
        if dynamic and material_analysis is not None:
            for reference in material_analysis['original_material_refs']:
                if reference not in materials:
                    materials.append(dict(reference))
        if comments:
            from .comment_insights import CommentOutput
            from .policies.extract_comments import CommentExtractInput, CommentExtractDecodeInput
            from .source_sections import (
                EXTRACT_INPUTS, resolve_comment_sources, validate_comment_inputs,
                freeze_comment_inputs, record_comment_candidate,
            )
            if prior_turn:
                frozen_comments = records.read(EXTRACT_INPUTS, prior_turn.object_id)
                if frozen_comments is None:
                    raise RecognitionConflict('comment_inputs_unavailable')
                comment_inputs = frozen_comments.payload
                if comment_inputs['document'] != {'id': document_id, 'revision': revision}:
                    raise RecognitionConflict('comment_document_changed')
                validate_comment_inputs(records, documents, project_id, comment_inputs)
            else:
                comment_inputs = resolve_comment_sources(records, documents, project_id, document_id, revision=revision)
            comment_materials = [{'type': source['source_type'], 'id': source['source_id'],
                'revision': source['revision'], 'project_id': source['project_id']}
                for source in comment_inputs['sources']]
            materials += [reference for reference in comment_materials if reference not in materials] if dynamic else comment_materials
        bindings = {'extract': selected}
        if prior_turn and comparative:
            bindings.update(prior_turn.payload['request'].get('policy_versions', {}))
            # Early experimental @2 requests predate the scope dependency.
            bindings.setdefault('scope', '@1')
        if comparative:
            validate()
        with override(**bindings):
            turn = MemoryTurn(records, models, kind="memory.propose_insights", project=project_id,
                key=prefix, materials=materials, validate=validate,
                retry_token=retry_token).select_insight_attempt(retry_token)
        if comments:
            comment_inputs = freeze_comment_inputs(records, documents, project_id, turn.turn_id, comment_inputs)
        applied = turn.store.get_immutable_payload(turn.turn_id, "memory-insights-applied-v1")
        if dynamic and applied:
            manifest = turn.store.get_immutable_payload(turn.turn_id, 'memory-insights-plan-v2')
            if (manifest is None or applied[1].get('candidate_ids') != manifest[1]['candidate_ids']
                    or applied[1].get('proposal_keys') != manifest[1]['proposal_keys']):
                raise RecognitionConflict('insight_candidate_manifest_conflicted')
            if all(turn.store.get_immutable_payload(turn.turn_id, 'memory-proposal-' + key)
                    for key in [*manifest[1]['proposal_keys'], *manifest[1]['support_keys']]):
                return _material_views(records, service, scope, manifest[1]['candidate_ids'],
                    experience_id, manifest[1]['generation_id'])
        if not dynamic and existing and applied and all(
                turn.store.get_immutable_payload(turn.turn_id, "memory-proposal-" + key)
                for key in applied[1].get("proposal_keys", [
                    "candidate-" + str(n) for n in range(1, len(applied[1]["candidate_ids"]) + 1)])):
            return existing_views()
        policy = get('extract', version=turn.request.get('policy_versions', {}).get('extract', '@1'))
        experiences = [{
            "id": experience.id, "revision": experience.revision, "content": experience.content,
            "provenance": experience.provenance.to_payload()}]
        neighbor_inputs = [{'id': row.object_id, 'revision': row.revision, 'project_id': row.payload['project_id'],
            'text': row.payload['content'], 'conditions': row.payload.get('conditions', [])} for row in neighbors]
        if comparative:
            with records.begin() as tx:
                frozen = tx.read('v2_extract_inputs', turn.turn_id)
                if frozen is None:
                    validate_analysis(tx)
                    frozen = tx.put('v2_extract_inputs', turn.turn_id,
                        {'neighbors': neighbor_inputs, 'projects': project_choices,
                         **({'material_analysis': material_analysis} if dynamic else {})}, expected_revision=0)
                elif dynamic and not _same_material(frozen.payload.get('material_analysis'), material_analysis):
                    raise RecognitionConflict('insight_material_analysis_changed')
                tx.commit()
            inputs = frozen.payload
            neighbor_inputs, project_choices = inputs['neighbors'], inputs['projects']
        if dynamic:
            from .material_insights import MaterialOutput
            from .policies.extract_material import MaterialExtractInput, MaterialExtractDecodeInput
            extract_input = MaterialExtractInput(experiences, CANDIDATE_SOURCE_CONSTRAINTS,
                neighbor_inputs, project_choices, project_id, comment_inputs['sources'], material_analysis)
            extract_input, messages, material_budget = _prepare_material_input(
                models, records, turn, policy, extract_input, MaterialOutput)
        elif comments:
            messages = policy.prepare(CommentExtractInput(experiences, CANDIDATE_SOURCE_CONSTRAINTS,
                neighbor_inputs, project_choices, project_id, comment_inputs['sources']))
        else:
            messages = policy.prepare(ExtractInput(experiences, CANDIDATE_SOURCE_CONSTRAINTS,
                neighbor_inputs, project_choices, project_id))
        response_model = MaterialOutput if dynamic else CommentOutput if comments else ComparativeOutput if comparative else InsightOutput
        output, metadata = turn.generate(messages, response_model=response_model,
            max_tokens=material_budget['output_budget']['max_tokens'] if dynamic else 1800)
        validate()
    except _MaterialBudgetError as error:
        report('insight_generation_failed')
        report(str(error))
        return []
    except InvalidStructuredOutput:
        report("insight_invalid_output")
        return []
    except Exception:
        report("insight_generation_failed")
        return []
    try:
        if dynamic:
            decoded = policy.decide(MaterialExtractDecodeInput(output.model_dump(), normalize_conditions,
                neighbor_inputs, project_choices, project_id, comment_inputs['sources'], material_analysis))
        elif comments:
            decoded = policy.decide(CommentExtractDecodeInput(output.model_dump(), normalize_conditions,
                neighbor_inputs, project_choices, project_id, comment_inputs['sources']))
        else:
            decoded = policy.decide(ExtractDecodeInput(output.model_dump(), normalize_conditions,
                neighbor_inputs, project_choices, project_id))
    except (ValueError, TypeError):
        report("insight_invalid_output")
        return []
    for code in decoded.errors:
        report(code)
    if not decoded.valid:
        if dynamic and 'insight_invalid_output' not in decoded.errors:
            report('insight_invalid_output')
        return []
    rows = decoded.rows
    if not isinstance(metadata, dict):
        report("insight_generation_failed")
        return []
    generation = {"id": metadata["generation_id"], "step_version": CANDIDATE_GENERATION_STEP_VERSION,
        "model": metadata.get("model"), "configuration_revision": metadata.get("configuration_revision"),
        "completed_at": metadata["completed_at"]}
    result = []
    proposal_keys = []
    manifest = None
    if dynamic:
        manifest = {'candidate_ids': [prefix + str(n) for n in range(1, len(rows) + 1)],
            'proposal_keys': ['candidate-' + str(n) + (
                ':turn:' + attempt[0] + ':run:' + attempt[1] if attempt is not None else '')
                for n in range(1, len(rows) + 1)],
            'support_keys': ['support-' + str(n) for n in range(1, len(decoded.supports) + 1)],
            'generation_id': generation['id']}
        saved_manifest = turn.store.get_immutable_payload(turn.turn_id, 'memory-insights-plan-v2')
        if saved_manifest is None:
            turn.store.get_or_create_immutable_payload(turn.turn_id, 'memory-insights-plan-v2', manifest)
            saved_manifest = turn.store.get_immutable_payload(turn.turn_id, 'memory-insights-plan-v2')
        frozen_manifest = saved_manifest[1]
        if any(frozen_manifest[key] != manifest[key] for key in ('candidate_ids', 'support_keys', 'generation_id')):
            report('insight_generation_failed')
            return []
        manifest = frozen_manifest
    for n, (text, conditions) in enumerate(rows, 1):
        candidate_id = prefix + str(n)
        try:
            validate()
            def existing_candidate():
                current = records.read("recognition_candidates", candidate_id)
                if current is not None and (
                    current.payload.get("source_experience_ids") != [experience_id]
                    or current.payload.get("scope") != {"user_id": scope.user_id, "project_id": project_id}
                    or (current.payload.get("generation") or {}).get("id") != generation["id"]
                ):
                    raise RecognitionConflict("insight_candidate_identity_conflicted")
                return current
            def write_candidate():
                options = dict(scope=scope, content=text, conditions=conditions,
                    source_experience_ids=[experience_id], candidate_id=candidate_id, generation=generation)
                if attempt is None:
                    if not comparative:
                        return service.propose(**options)
                with records.begin() as tx:
                    validate_attempt(tx)
                    if comments:
                        from ..transaction_records import TransactionRecords
                        validate_comment_inputs(records, documents, project_id, comment_inputs, reader=tx)
                        validate_analysis(tx)
                        turn.validate_request(TransactionRecords(tx), models, turn.request, purpose='generation')
                    candidate = service.propose_in_uow(tx, **options)
                    if comparative:
                        hint = decoded.hints[n-1]
                        relation_hint = {key: hint[key] for key in ('relation', 'target_id', 'scope_hint')} if comments else hint
                        tx.put('v2_candidate_hints', candidate_id, {'project_id': project_id, **relation_hint}, expected_revision=0)
                        if comments and 'comment_source' in hint:
                            record_comment_candidate(tx, candidate_id, project_id, generation['id'],
                                turn.turn_id, hint, comment_inputs)
                        if dynamic and material_analysis is not None and 'analysis_claim_ids' in hint:
                            tx.put('v2_material_candidate_evidence', candidate_id, {
                                'project_id': project_id, 'extract_turn_id': turn.turn_id,
                                'analysis_ref': material_analysis['analysis_ref'],
                                'bindings': material_analysis['bindings'],
                                'claim_ids': hint['analysis_claim_ids'], 'evidence': hint['analysis_evidence']}, expected_revision=0)
                    tx.commit()
                return candidate
            try:
                proposal_key = manifest['proposal_keys'][n-1] if dynamic else "candidate-" + str(n)
                if not dynamic and attempt is not None:
                    proposal_key += ":turn:" + attempt[0] + ":run:" + attempt[1]
                proposal_keys.append(proposal_key)
                turn.propose(key=proposal_key,
                    existing=existing_candidate,
                    write=write_candidate)
            except RecognitionConflict:
                current = existing_candidate()
                if current is None:
                    raise
            validate()
            assignment = scene_of(records, "document", document_id)
            if assignment and assignment.get("project_id") == project_id:
                assign_scene(records, "candidate", candidate_id, project_id, assignment["scene"], if_absent=True)
            result.append(insight_view(records, scope, candidate_id, service=service))
        except Exception:
            report("insight_generation_failed")
            return []
    validate()
    if comparative:
        from .links import InsightLinks
        for index, support in enumerate(decoded.supports, 1):
            validate()
            identity = 'support-' + turn.turn_id + '-' + str(index)
            turn.propose(key='support-' + str(index),
                existing=lambda identity=identity: records.read('v2_insight_evidence_support', identity),
                write=lambda identity=identity, support=support: InsightLinks(records, service).propose_extraction_support(
                    project_id, experience, {'id': document_id, 'revision': revision},
                    support['target_id'], support['evidence'], identity))
    if turn.store.get_immutable_payload(turn.turn_id, "memory-insights-applied-v1") is None:
        marker = {"candidate_ids": manifest['candidate_ids'] if dynamic else [row["id"] for row in result]}
        if dynamic or attempt is not None:
            marker["proposal_keys"] = proposal_keys
        turn.store.get_or_create_immutable_payload(turn.turn_id, "memory-insights-applied-v1", marker)
    return result
