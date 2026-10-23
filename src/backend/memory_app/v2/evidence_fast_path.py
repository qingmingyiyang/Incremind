"""One governed evidence answer; rejected proposals retain their real usage."""
from hashlib import sha256
import json
from time import monotonic

from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..answer_proof import review_input_tokens
from ..kernel.answer_turns import (
    ACTIVE_ANSWER, abandon_answer_model, answer_observation, generate_answer,
)
from ..model_config import ModelConfigurationError
from ..recall_state import is_recall_excluded
from .budget import text_tokens
from .followup import sum_usage
from .insight_validity import COLLECTION, from_confirmation, now, read_validity
from .policies.insight_time import instant, valid


INVOCATION = 'evidence-answer-v1'
MAX_OUTPUT_TOKENS = 4096


def _json_fact(value):
    """Owner facts in the capsule must use ordinary closed JSON values."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))


def _scope(value, project):
    if type(value) is dict:
        value = WorkScope(value.get('user_id'), value.get('project_id'))
    if (type(value) is not WorkScope or value.user_id != 'local-user'
            or value.project_id not in {project, 'me'}):
        raise ValueError('evidence_source_scope_unsupported')
    return value


def _recognition_basis(query, row, project):
    scope = _scope(row.get('scope'), project)
    identity = row['id']
    current = query.service.get_recognition(scope=scope, recognition_id=identity)
    if current is None or not current.authorized:
        raise ValueError('evidence_recognition_unavailable')
    if current.scope != scope:
        raise RecognitionConflict('evidence_answer_evidence_changed')
    entry = current.retrieval_projection()
    if row.get('entry') is not None and row['entry'] != entry:
        raise RecognitionConflict('evidence_answer_evidence_changed')
    if (not current.source_evidence_complete or not current.source_evidence
            or is_recall_excluded(query.records, scope, identity)):
        raise ValueError('evidence_recognition_sources_incomplete')
    confirmation, marker = from_confirmation(query.records, identity), read_validity(query.records, identity)
    clock = instant(now())
    if (confirmation is None or not isinstance(marker, dict) or not valid(marker)
            or marker['valid_from'] != confirmation['valid_from'] or clock is None
            or instant(marker['valid_from']) > clock
            or marker['valid_until'] is not None and clock >= instant(marker['valid_until'])):
        raise ValueError('evidence_recognition_not_current')
    sidecar = query.records.read(COLLECTION, identity)
    return {'id': identity, 'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
        'owner': 'recognition_service', 'revision': current.revision,
        'content_sha256': sha256(current.content.encode('utf-8')).hexdigest(),
        'conditions': list(current.conditions), 'source_evidence': list(current.source_evidence),
        'confirmation': confirmation, 'validity': marker,
        'validity_revision': sidecar.revision if sidecar else 0}


def _registered_relations(query, project, identities):
    """Read registered edges even when the other endpoint did not fit the prompt."""
    result = []
    scopes = [{'user_id': 'local-user', 'project_id': item} for item in {project, 'me'}]
    for row in query.records.list('recognition_relations'):
        edge = row.payload
        if (edge.get('scope') not in scopes or edge.get('relation') not in {'refutes', 'supersedes'}):
            continue
        endpoints = set()
        for name in ('from_id', 'to_id'):
            identity = edge.get(name)
            endpoint = query.records.read('recognitions', identity) if isinstance(identity, str) else None
            owner = endpoint.payload.get('scope', {}) if endpoint else {}
            if (owner.get('user_id'), owner.get('project_id'), identity) in identities:
                endpoints.add(name)
        if not endpoints:
            continue
        if edge['relation'] == 'refutes':
            raise ValueError('evidence_registered_conflict')
        if 'to_id' in endpoints:
            raise ValueError('evidence_recognition_not_current')
        result.append({'id': row.object_id, 'revision': row.revision,
            **{key: edge.get(key) for key in ('scope', 'from_id', 'to_id', 'relation')}})
    return sorted(result, key=lambda item: item['id'])


def source_basis(query, plan):
    """Re-read normal owners; L1/L2 text never becomes confirmed L3 memory.

    Do not call validate_ask_plan here: that function invokes this marker guard.
    Its existing source owner is safe to reuse without recursively validating
    the marker.  Dispatch also runs the complete plan/request guards.
    """
    query._validate_ask_sources(plan)
    project, result = plan['project_id'], []
    retrieved = plan.get('evidence_retrieved_recognitions', [])
    if not isinstance(retrieved, list):
        raise ValueError('evidence_retrieved_manifest_invalid')
    recognition_basis = [_recognition_basis(query, row, project) for row in retrieved]
    identities = {(row['scope']['user_id'], row['scope']['project_id'], row['id'])
                  for row in recognition_basis}
    for row in plan['chosen']:
        scope = _scope(row.get('scope'), project)
        kind, layer, entry = row['kind'], row['layer'], row['entry']
        if (kind == 'recognition' and layer != 'L3'
                or kind not in {'recognition', 'document', 'source'}
                or kind != 'recognition' and layer not in {'L0', 'L1', 'L2'}
                or row.get('inspiration')):
            raise ValueError('evidence_source_kind_unsupported')
        content, excerpt, windows = entry.get('content'), row.get('excerpt'), row.get('windows')
        if (not isinstance(content, str) or not isinstance(excerpt, str) or not excerpt.strip()
                or not isinstance(windows, (tuple, list)) or not windows
                or type(entry.get('revision')) is not int):
            raise ValueError('evidence_source_content_unavailable')
        if kind == 'recognition':
            qualification = _recognition_basis(query, row, project)
            identities.add((scope.user_id, scope.project_id, row['id']))
        else:
            # The normal owner just checked current bodies/revisions and egress.
            # No raw capture type, complete-source or user_confirmed restriction.
            qualification = {'owner': 'workspace_query_sources'}
        result.append({'id': row['id'], 'kind': kind, 'layer': layer,
            'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
            'revision': entry['revision'], 'coordinate_space': row.get('coordinate_space',
                'recognition_content_v1' if kind == 'recognition' else None),
            'content_sha256': sha256(content.encode('utf-8')).hexdigest(),
            'excerpt_sha256': sha256(excerpt.encode('utf-8')).hexdigest(),
            'windows': [{'start': window.start, 'end': window.end,
                         'text_sha256': sha256(window.text.encode('utf-8')).hexdigest()}
                        for window in windows],
            'snapshot': row.get('snapshot'), 'conditions': list(entry.get('conditions', [])),
            'qualification': qualification})
    if result:
        result[0]['retrieved_recognitions'] = recognition_basis
        result[0]['registered_relations'] = _registered_relations(query, project, identities)
    return _json_fact(result)


def _known_risks(collected):
    """Only existing explicit metadata; infer no semantic conflicts from text."""
    for row in collected.get('candidates', []):
        if row.get('link_kind') == 'refutes':
            return [{'kind': 'risk', 'reason': 'conflict', 'unit_ids': []}]
        marker = row.get('validity')
        clock = instant(now())
        if (row.get('stale') or row.get('kind') == 'recognition'
                and row.get('entry', {}).get('status') in {'stale', 'superseded', 'revoked'}
                or isinstance(marker, dict) and marker and (not {'valid_from', 'valid_until'} <= marker.keys()
                    or not valid(marker) or clock is None
                    or instant(marker['valid_from']) > clock
                    or marker['valid_until'] is not None and clock >= instant(marker['valid_until']))):
            return [{'kind': 'risk', 'reason': 'unresolved_time_scope', 'unit_ids': []}]
    for excluded in collected.get('excluded_sources', []):
        if excluded.get('reason') == 'recognition_source_evidence_incomplete':
            return [{'kind': 'risk', 'reason': 'low_confidence', 'unit_ids': []}]
    return []


def validate_evidence_plan(query, plan):
    from .evidence_answer import preflight
    if 'evidence_answer_proof' not in plan:
        return
    proof = plan['evidence_answer_proof']
    if (plan.get('policy_versions', {}).get('retrieve') != '@12'
            or plan.get('policy_versions', {}).get('compose') != '@36'
            or type(proof) is not dict or set(proof) != {'contract', 'invocation'}
            or proof['invocation'] != INVOCATION or type(proof['contract']) is not dict):
        raise RecognitionConflict('evidence_answer_evidence_changed')
    try:
        accepted = (source_basis(query, plan) == plan.get('evidence_source_basis')
                    and preflight(plan) == proof['contract'])
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        accepted = False
    if not accepted:
        raise RecognitionConflict('evidence_answer_evidence_changed')


def _output_limit(plan):
    budget = plan.get('answer_output_budget')
    if (type(budget) is not dict or set(budget) != {'window', 'reserve', 'target_revision'}
            or any(type(budget.get(key)) is not int for key in ('window', 'reserve', 'target_revision'))
            or not 0 < budget['reserve'] < budget['window']
            or type(plan['target'].get('revision')) is not int
            or budget['target_revision'] != plan['target']['revision']
            or type(plan.get('budget')) is not int or plan['budget'] <= 0):
        raise ValueError('evidence_output_budget_unavailable')
    return min(MAX_OUTPUT_TOKENS, budget['reserve'])


async def attempt(query, plan, collected, route):
    from .evidence_answer import EvidenceAnswerOutput, followup_hints, messages, preflight, validate_output
    result = {'accepted': False, 'usage': {}, 'receipt_ids': [], 'usage_complete': True,
              'diagnostics': []}
    risks = _known_risks(collected) or _known_risks({'candidates': plan['chosen']})
    if risks:
        route['reason'], result['diagnostics'] = 'evidence_known_risk', risks
        return result
    manifest = {}
    for row in collected.get('candidates', []):
        if row.get('kind') == 'recognition':
            scope = _scope(row.get('scope'), plan['project_id'])
            manifest[(scope.project_id, row['id'])] = {'id': row['id'],
                'scope': {'user_id': scope.user_id, 'project_id': scope.project_id}}
    plan['evidence_retrieved_recognitions'] = list(manifest.values())
    query.validate_ask_plan(plan)
    try:
        basis = source_basis(query, plan)
    except RecognitionError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        route['reason'] = 'evidence_source_unqualified'
        result['diagnostics'] = [{'kind': 'risk', 'reason': 'low_confidence', 'unit_ids': []}]
        return result
    plan['evidence_source_basis'] = basis
    contract = preflight(plan)
    if contract is None:
        route['reason'] = 'evidence_contract_unavailable'
        return result
    prepared = messages(plan, contract)
    try:
        output_limit = _output_limit(plan)
        limits = query.models.generation_budget_limits(
            expected_revision=plan['target']['revision'], max_tokens=output_limit)
        input_tokens = review_input_tokens(prepared, EvidenceAnswerOutput)
        if (not isinstance(limits, dict) or any(type(limits.get(key)) is not int for key in ('window', 'reserve'))
                or not 0 < limits['reserve'] < limits['window']
                or input_tokens > min(plan['budget'], limits['window'] - limits['reserve'],
                                      plan['answer_output_budget']['window'] - output_limit)):
            raise ValueError('evidence_input_budget_insufficient')
    except (ValueError, TypeError, KeyError):
        route['reason'] = 'evidence_input_budget_insufficient'
        return result

    def validate():
        query.validate_ask_plan(plan)
        active_request, store, _, _ = ACTIVE_ANSWER.get()
        query.validate_answer_request(query.models, store.get_request(active_request['turn_id']))
        try:
            stable = (not _known_risks(collected) and not _known_risks({'candidates': plan['chosen']})
                      and source_basis(query, plan) == basis
                      and preflight(plan) == contract)
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            stable = False
        if not stable:
            raise RecognitionConflict('evidence_answer_evidence_changed')

    route['generation_invocations'] = route.get('generation_invocations', 0) + 1
    started = monotonic()
    try:
        output, metadata = await run_in_threadpool(lambda: generate_answer(
            query.models, prepared, response_model=EvidenceAnswerOutput,
            max_tokens=output_limit, validate_current=validate,
            invocation_key=INVOCATION, timeout_seconds=35))
        result['usage'] = metadata.get('usage', {})
        validate()
        try:
            if text_tokens(json.dumps(output.model_dump(), ensure_ascii=False)) > output_limit:
                raise ValueError('evidence_output_budget_exceeded')
            validate_output(plan, contract, output)
        except ValueError:
            result['diagnostics'] = followup_hints(output) or [
                {'kind': 'structural', 'code': 'evidence_answer_output_rejected'}]
            route['reason'] = 'evidence_output_rejected'
            return result
        plan['evidence_answer_proof'] = {'contract': contract, 'invocation': INVOCATION}
        validate_evidence_plan(query, plan)
        route.update(route='direct', reason='evidence_answer_accepted')
        result['accepted'] = True
        return result
    except RecognitionError:
        abandon_answer_model(INVOCATION)
        raise
    except ModelConfigurationError:
        validate()
        abandon_answer_model(INVOCATION)
        route['reason'] = 'evidence_generation_failed'
        return result
    except (ValueError, TimeoutError):
        abandon_answer_model(INVOCATION)
        result['diagnostics'] = [{'kind': 'structural', 'code': 'evidence_answer_generation_failed'}]
        route['reason'] = 'evidence_generation_failed'
        return result
    finally:
        observed = answer_observation(INVOCATION)
        result['receipt_ids'] = observed['receipt_ids']
        result['usage_complete'] = observed['complete']
        if not result['usage']:
            result['usage'] = sum_usage(*observed['observations'])
        route['generation_usage_complete'] = observed['complete']
        route['generation_physical_attempts'] = len(observed['receipt_ids'])
        route['generation_seconds'] = round(monotonic() - started, 6)


def frozen_result(query, plan):
    """Revalidate and publish the accepted immutable result without generation."""
    from .evidence_answer import EvidenceAnswerOutput, messages, validate_output
    validate_evidence_plan(query, plan)
    request, store, _, _ = ACTIVE_ANSWER.get()
    value = store.get_immutable_payload(request['turn_id'], 'answer-model-result-' + INVOCATION)
    sent = store.get_immutable_payload(request['turn_id'], 'answer-model-input-' + INVOCATION)
    contract = plan['evidence_answer_proof']['contract']
    if (value is None or sent is None or sent[1].get('purpose') != 'primary'
            or sent[1].get('response_schema') != EvidenceAnswerOutput.model_json_schema()
            or sent[1].get('messages') != messages(plan, contract)):
        raise RecognitionConflict('evidence_answer_result_unavailable')
    parsed = EvidenceAnswerOutput.model_validate(value[1]['output'])
    if text_tokens(json.dumps(parsed.model_dump(), ensure_ascii=False)) > _output_limit(plan):
        raise RecognitionConflict('evidence_answer_result_unavailable')
    output = validate_output(plan, contract, parsed)
    return output, sent[1]['messages'], value[1]['metadata']
