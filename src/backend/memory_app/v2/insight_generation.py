"""Generate bounded, reviewable insights from an admitted document revision."""
import logging
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
        def validate():
            validate_attempt()
            validate_sources()
            if comment_inputs is not None:
                validate_comment_inputs(records, documents, project_id, comment_inputs)
            if comparative and project_choices != public_projects(records, documents, models):
                raise RecognitionConflict('insight_project_choices_changed')
        selected = (prior_turn.payload['request'].get('policy_versions', {}).get('extract', '@1')
            if prior_turn else version('extract'))
        comments = selected in {'@3', '@4', '@5'}
        comparative = selected in {'@2', '@3', '@4', '@5'}
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
            materials += [{'type': source['source_type'], 'id': source['source_id'],
                'revision': source['revision'], 'project_id': source['project_id']}
                for source in comment_inputs['sources']]
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
        if existing and applied and all(
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
                    frozen = tx.put('v2_extract_inputs', turn.turn_id,
                        {'neighbors': neighbor_inputs, 'projects': project_choices}, expected_revision=0)
                tx.commit()
            inputs = frozen.payload
            neighbor_inputs, project_choices = inputs['neighbors'], inputs['projects']
        if comments:
            messages = policy.prepare(CommentExtractInput(experiences, CANDIDATE_SOURCE_CONSTRAINTS,
                neighbor_inputs, project_choices, project_id, comment_inputs['sources']))
        else:
            messages = policy.prepare(ExtractInput(experiences, CANDIDATE_SOURCE_CONSTRAINTS,
                neighbor_inputs, project_choices, project_id))
        response_model = CommentOutput if comments else ComparativeOutput if comparative else InsightOutput
        output, metadata = turn.generate(messages, response_model=response_model, max_tokens=1800)
        validate()
    except InvalidStructuredOutput:
        report("insight_invalid_output")
        return []
    except Exception:
        report("insight_generation_failed")
        return []
    try:
        if comments:
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
                        turn.validate_request(TransactionRecords(tx), models, turn.request, purpose='generation')
                    candidate = service.propose_in_uow(tx, **options)
                    if comparative:
                        hint = decoded.hints[n-1]
                        relation_hint = {key: hint[key] for key in ('relation', 'target_id', 'scope_hint')} if comments else hint
                        tx.put('v2_candidate_hints', candidate_id, {'project_id': project_id, **relation_hint}, expected_revision=0)
                        if comments and 'comment_source' in hint:
                            record_comment_candidate(tx, candidate_id, project_id, generation['id'],
                                turn.turn_id, hint, comment_inputs)
                    tx.commit()
                return candidate
            try:
                proposal_key = "candidate-" + str(n)
                if attempt is not None:
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
        marker = {"candidate_ids": [row["id"] for row in result]}
        if attempt is not None:
            marker["proposal_keys"] = proposal_keys
        turn.store.get_or_create_immutable_payload(turn.turn_id, "memory-insights-applied-v1", marker)
    return result
