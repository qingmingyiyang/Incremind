"""A single-generation evidence proposal, with explicit model coverage judgment.

Requirements and risk assessment are fallible model judgments, not a local proof
of semantic completeness.  This module verifies their structure, frozen source
basis, exact evidence coordinates and the existing calendar contract.  Current
owner qualification and authorization remain the runtime owner's responsibility.
"""
from copy import deepcopy
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from ..structured_generation import AskOutput
from .answer_evidence import (
    ReferencedStatement, _resolve_reference_statements, build_evidence_catalog,
    grounded_output_limit, model_catalog, model_date_refs, render_grounded_answer,
    validate_grounded_answer,
)
from .policies.date_grounding import build_date_ref_catalog, validate_calendar_statements_v7


_INDEX = Annotated[StrictInt, Field(ge=1)]
_RISK = Literal['missing', 'low_confidence', 'conflict', 'multi_hop', 'attribution', 'unresolved_time_scope']
_MAX_OUTPUT = 4096


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: Annotated[StrictStr, Field(min_length=1)]
    status: Literal['supported', 'missing', 'uncertain']
    statement_indices: list[_INDEX]

    @field_validator('text')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('evidence_requirement_empty')
        return value


class EvidenceIssue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    reason: _RISK
    requirement_indices: list[_INDEX]
    unit_ids: list[_INDEX]
    detail: Annotated[StrictStr, Field(min_length=1, max_length=240)]


class EvidenceAnswerOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    requirements: Annotated[list[EvidenceRequirement], Field(min_length=1)]
    statements: list[ReferencedStatement]
    issues: list[EvidenceIssue]


def _json_copy(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    copied = json.loads(encoded)
    if copied != value:
        raise ValueError('evidence_answer_context_not_json')
    return copied


def _input_tokens(messages):
    from ..answer_proof import review_input_tokens
    return review_input_tokens(messages, EvidenceAnswerOutput)


def preflight(plan):
    """Freeze actual budgeted evidence and owner-supplied basis, or return None.

    No ordinary-question parser or full-source requirement is used.  History
    and profile can be frozen as reference context.  Nonempty part/situation
    contexts remain in investigation until their parent authority is bound.
    """
    if not isinstance(plan, dict) or plan.get('part_context') or plan.get('situation'):
        return None
    if (plan.get('policy_versions', {}).get('retrieve') != '@12'
            or plan.get('policy_versions', {}).get('compose') != '@36'):
        return None
    question, history = plan.get('question'), plan.get('history', '')
    chosen, basis = plan.get('chosen'), plan.get('evidence_source_basis')
    if (not isinstance(question, str) or not question.strip() or not isinstance(history, str)
            or not isinstance(chosen, (list, tuple)) or not chosen
            or not isinstance(basis, list) or len(basis) != len(chosen)):
        return None
    for candidate, row in zip(chosen, basis):
        if not isinstance(candidate, dict) or not isinstance(row, dict):
            return None
        identity = {key: candidate.get(key) for key in ('id', 'kind', 'layer')}
        identity['coordinate_space'] = candidate.get('coordinate_space',
            'recognition_content_v1' if candidate.get('kind') == 'recognition' else None)
        if any(key not in row or row[key] != value for key, value in identity.items()):
            return None
    try:
        if type(plan.get('budget')) is not int or plan['budget'] <= 0:
            return None
        grounded_output_limit(plan, 0)
        catalog = build_evidence_catalog(chosen, compose_version='@36')
        if not catalog['units']:
            return None
        # Native calculator options contain tuple spans; their frozen JSON
        # projection uses arrays while the guard later rebuilds the real refs.
        refs = json.loads(json.dumps(build_date_ref_catalog(catalog, chosen, calendar_version=3),
                                     ensure_ascii=False, allow_nan=False))
        contract = _json_copy({
            'version': 'evidence-answer-v1', 'question': question, 'history': history,
            'profile': plan.get('profile', {}),
            'retrieval_question': plan.get('retrieval_question', question),
            'evidence_source_basis': basis, 'evidence_catalog': catalog,
            'date_provenance': refs,
            'budget': plan['budget'], 'answer_output_budget': plan['answer_output_budget'],
            'target_revision': plan['target']['revision'],
        })
        cost = _input_tokens(_messages(contract))
        cap = min(_MAX_OUTPUT, plan['answer_output_budget']['reserve'])
        if cost > min(plan['budget'], plan['answer_output_budget']['window'] - cap):
            return None
        return contract
    except (ValueError, TypeError, KeyError):
        return None


def _messages(contract):
    return [
        {'role': 'system', 'content': (
            'evidence-answer-v1: Propose a concise answer from the supplied evidence_catalog. '
            'Return only the EvidenceAnswerOutput schema: requirements, statements, issues. '
            'Keep every constraint in the ORIGINAL question: subject, attribution, requested objects, '
            'conditions, time scope, exceptions and required granularity. History/profile/retrieval wording '
            'are reference context, never factual evidence and never a replacement for that question. '
            'List every independent requirement. For each requirement report supported, missing or uncertain '
            'and its one-based statement_indices. This coverage judgment can be wrong: do not present it '
            'as a program proof or mark support merely because a related passage was retrieved. '
            'Use issues for any missing evidence, low confidence, known conflict, multi-hop gap, uncertain '
            'person/speaker attribution or unresolved time scope. Do not hide uncertainty by omitting a '
            'requirement. Every statement must be linked to a requirement and contain exact continuous '
            'unit quotes with the necessary conditions. A support quote must support that proposition; '
            'other people\'s I/we, plans, questions and cancelled events are not confirmed facts about '
            'the requested person. Source qualifications are supplied by the owner, never invent confirmation '
            'or validity dates. Only actual evidence_catalog text is evidence. Boundary flags and continuity '
            'groups preserve gaps; never join noncontiguous text. Computed calendar dates require kind=date, '
            'real matching date_refs, their complete required sentence and legal full precision. Record '
            'anchors are calculation inputs, not event days; an explanation must retain the same offset '
            'and complete result. Explicit source dates need no calculated ref. fact/inference use empty '
            'refs. Do not invent refs, derived_notes, an independent free answer or first/review bookkeeping. '
            'A direct answer is eligible only when all requirements are supported, issues is empty and all '
            'statements are facts or dates. Otherwise report the actual gaps for investigation.')},
        {'role': 'user', 'content': json.dumps({
            'evidence_answer_contract': {key: deepcopy(contract[key]) for key in
                ('version', 'question', 'history', 'profile', 'retrieval_question')},
            'evidence_catalog': model_catalog(contract['evidence_catalog']),
            'date_provenance': model_date_refs(contract['date_provenance']),
        }, ensure_ascii=False, separators=(',', ':'), allow_nan=False)},
    ]


def _current(plan, contract):
    if not isinstance(contract, dict) or preflight(plan) != contract:
        raise ValueError('evidence_answer_contract_changed_or_unavailable')


def messages(plan, contract):
    _current(plan, contract)
    return _messages(contract)


def _coverage_codes(value):
    codes, linked = [], set()
    if not value.statements:
        codes.append('statements_empty')
    for requirement in value.requirements:
        indices = requirement.statement_indices
        if requirement.status != 'supported':
            codes.append('requirements_not_supported')
        if not indices or len(set(indices)) != len(indices) or any(index > len(value.statements) for index in indices):
            codes.append('requirement_statement_indices_invalid')
        linked.update(indices)
    if linked != set(range(1, len(value.statements) + 1)):
        codes.append('statements_not_all_linked')
    if any(statement.kind not in {'fact', 'date'} for statement in value.statements):
        codes.append('statement_requires_investigation')
    if value.issues:
        codes.append('risks_reported')
    return list(dict.fromkeys(codes))


def validate_output(plan, contract, output) -> AskOutput:
    """Validate local structure/provenance, without certifying semantic coverage."""
    _current(plan, contract)
    value = EvidenceAnswerOutput.model_validate(output)
    codes = _coverage_codes(value)
    if codes:
        raise ValueError('evidence_answer_' + codes[0])
    catalog = contract['evidence_catalog']
    statements, bindings = _resolve_reference_statements(value.statements, catalog,
        plan['chosen'], compose_version='@36')
    used = {item.unit_id for statement in statements for item in statement.evidence}
    available = {unit['id'] for unit in catalog['units']}
    # This is the actual proposal's selection, not a synthetic first draft or
    # a claimed independent review.  It feeds the normal quote/coordinate guard.
    partition = {'selected_ids': sorted(used), 'irrelevant_ids': sorted(available - used), 'uncertain_ids': []}
    grounded = validate_grounded_answer({'partition': partition,
        'statements': [statement.model_dump() for statement in statements], 'withdrawals': []},
        catalog, partition, chosen=plan['chosen'])
    rebound = {id(item): bindings[id(original)] for item, original in zip(grounded.statements, statements)}
    validate_calendar_statements_v7(grounded.statements, catalog, plan['chosen'],
                                    contract['question'], record_bindings=rebound)
    cap = min(_MAX_OUTPUT, grounded_output_limit(plan, _input_tokens(_messages(contract))))
    from .policies import get
    return render_grounded_answer(grounded, catalog, partition, max_output_tokens=cap,
        chosen=plan['chosen'], **get('compose', version='@36')(None,
            operation='grounded_render', question=contract['question']))


def followup_hints(output):
    """Only structural gaps/risks, never proposal text, enter investigation."""
    try:
        value = EvidenceAnswerOutput.model_validate(output)
    except (ValueError, TypeError):
        return [{'kind': 'structural', 'code': 'evidence_answer_output_invalid'}]
    hints = [{'kind': 'requirement', 'requirement_indices': [index], 'status': requirement.status}
             for index, requirement in enumerate(value.requirements, 1) if requirement.status != 'supported']
    hints.extend({'kind': 'risk', 'reason': issue.reason,
                  'requirement_indices': list(issue.requirement_indices), 'unit_ids': list(issue.unit_ids)}
                 for issue in value.issues)
    hints.extend({'kind': 'structural', 'code': code} for code in _coverage_codes(value)
                 if code not in {'requirements_not_supported', 'risks_reported'})
    return hints
