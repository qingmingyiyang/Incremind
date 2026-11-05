"""One governed extractive generation; rejected attempts remain real usage."""
from hashlib import sha256
import json
from time import monotonic

from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..answer_proof import review_input_tokens
from ..model_config import ModelConfigurationError
from ..kernel.answer_turns import (
    ACTIVE_ANSWER, abandon_answer_model, answer_observation, generate_answer,
)
from .followup import sum_usage
from .budget import text_tokens
from .insight_validity import now
from .policies.insight_time import instant


INVOCATION = 'raw-answer-v1'


def source_basis(query, plan):
    """Re-read the existing source owner's acceptance, never manufacture it."""
    scope = WorkScope('local-user', plan['project_id'])
    result = []
    for row in plan['chosen']:
        if (row.get('kind') != 'source' or row.get('layer') != 'L0' or row.get('scope') != scope
                or any(row.get(key) for key in ('persona', 'bookshelf', 'inspiration', 'stale'))):
            raise ValueError('raw_source_scope_unsupported')
        original = query.source_store.read('sources', row['id'])
        if not isinstance(original, dict):
            raise ValueError('raw_source_acceptance_unavailable')
        body = original.get('metadata', {}).get('content')
        recorded = instant(original.get('created_at'))
        if (original.get('project_id') != plan['project_id'] or original.get('type') != 'text'
                or original.get('trust_status') != 'user_confirmed'
                or original.get('processing_state') != 'captured'
                or not isinstance(original.get('storage_uri'), str)
                or not isinstance(body, str) or body != row['entry'].get('content')
                or original.get('content_hash') != sha256(body.encode('utf-8')).hexdigest()
                or recorded is None or recorded > instant(now())):
            raise ValueError('raw_source_acceptance_unavailable')
        result.append({'id': row['id'], 'revision': row['entry']['revision'],
            **{key: original[key] for key in ('project_id', 'type', 'trust_status',
                'processing_state', 'storage_uri', 'content_hash', 'created_at')}})
    return result


def validate_raw_plan(query, plan):
    from .raw_answer import preflight
    if 'raw_answer_proof' not in plan:
        return
    proof = plan['raw_answer_proof']
    try:
        valid = (plan.get('policy_versions', {}).get('retrieve') == '@11'
            and proof.get('invocation') == INVOCATION
            and source_basis(query, plan) == plan.get('raw_source_basis')
            and preflight(plan) == proof.get('contract'))
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid:
        raise RecognitionConflict('raw_answer_evidence_changed')


async def attempt(query, plan, collected, route):
    from .raw_answer import RawAnswerOutput, messages, preflight, validate_output
    result = {'accepted': False, 'usage': {}, 'receipt_ids': [], 'usage_complete': True}
    # Every retrieved row must be represented: do not hide a retrieved conflict
    # merely because the regular answer budget selected fewer records.
    if (len(plan['chosen']) != len(collected['candidates'])
            or {(row['kind'], row['id']) for row in plan['chosen']}
               != {(row['kind'], row['id']) for row in collected['candidates']}):
        route['reason'] = 'retrieved_evidence_not_fully_selected'
        return result
    contract = preflight(plan)
    if contract is None:
        route['reason'] = 'raw_evidence_not_complete'
        return result
    try:
        basis = source_basis(query, plan)
    except ValueError:
        route['reason'] = 'raw_source_acceptance_unavailable'
        return result
    prepared = messages(plan, contract)
    frozen_budget = plan.get('answer_output_budget')
    if (not isinstance(frozen_budget, dict) or type(frozen_budget.get('reserve')) is not int
            or frozen_budget['reserve'] <= 0
            or frozen_budget.get('target_revision') != plan['target']['revision']):
        route['reason'] = 'raw_output_budget_unavailable'
        return result
    output_limit = min(2048, frozen_budget['reserve'])
    limits = query.models.generation_budget_limits(
        expected_revision=plan['target']['revision'], max_tokens=output_limit)
    if review_input_tokens(prepared, RawAnswerOutput) > min(
            plan['budget'], limits['window'] - limits['reserve']):
        route['reason'] = 'raw_input_budget_insufficient'
        return result

    def validate():
        query.validate_ask_plan(plan)
        active_request, store, _, _ = ACTIVE_ANSWER.get()
        query.validate_answer_request(query.models, store.get_request(active_request['turn_id']))
        if source_basis(query, plan) != basis or preflight(plan) != contract:
            raise RecognitionConflict('raw_answer_evidence_changed')

    route['generation_invocations'] += 1
    started = monotonic()
    try:
        output, metadata = await run_in_threadpool(lambda: generate_answer(
            query.models, prepared, response_model=RawAnswerOutput,
            max_tokens=output_limit, validate_current=validate,
            invocation_key=INVOCATION, timeout_seconds=35))
        result['usage'] = metadata.get('usage', {})
        validate()
        try:
            if text_tokens(json.dumps(output.model_dump(), ensure_ascii=False)) > output_limit:
                raise ValueError('raw_output_budget_exceeded')
            validate_output(plan, contract, output)
        except ValueError:
            route['reason'] = 'raw_output_rejected'
            return result
        plan['raw_source_basis'] = basis
        plan['raw_answer_proof'] = {'contract': contract, 'invocation': INVOCATION}
        validate_raw_plan(query, plan)
        route.update(route='direct', reason='raw_answer_accepted')
        result['accepted'] = True
        return result
    except RecognitionError:
        abandon_answer_model(INVOCATION)
        raise
    except ModelConfigurationError:
        # Transport errors share this public exception type. Revalidate all
        # guards so a changed route/consent cannot be treated as ordinary lack
        # of evidence; a stable transport failure may use the old investigation.
        validate()
        abandon_answer_model(INVOCATION)
        route['reason'] = 'raw_generation_failed'
        return result
    except (ValueError, TimeoutError):
        abandon_answer_model(INVOCATION)
        route['reason'] = 'raw_generation_failed'
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
    """Publish the one accepted frozen result; never invoke a second composer."""
    from .raw_answer import messages, validate_output
    validate_raw_plan(query, plan)
    request, store, _, _ = ACTIVE_ANSWER.get()
    value = store.get_immutable_payload(request['turn_id'], 'answer-model-result-' + INVOCATION)
    sent = store.get_immutable_payload(request['turn_id'], 'answer-model-input-' + INVOCATION)
    contract = plan['raw_answer_proof']['contract']
    if value is None or sent is None or sent[1]['messages'] != messages(plan, contract):
        raise RecognitionConflict('raw_answer_result_unavailable')
    output = validate_output(plan, contract, value[1]['output'])
    return output, sent[1]['messages'], value[1]['metadata']
