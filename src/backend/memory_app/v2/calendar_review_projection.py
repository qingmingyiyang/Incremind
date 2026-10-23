"""Finite versioned calendar projections of frozen output, never a model retry.

The original first/review JSON is retained.  Classification repair changes only
kind and review disposition in a copy; uncertain conversions quote the existing
evidence instead.  Version 35 retains the unchanged @34 guards; 36 selects its
strict arithmetic-anchor guard for both diagnostics and final verification.
"""
from copy import deepcopy
import re


_CALENDAR = 'grounded_calendar_provenance_invalid'
_SOFT = {_CALENDAR, 'invalid_first_fact_requires_correction'}
_CORE = ('kind', 'text', 'evidence', 'date_refs')
_CORRECTION = 'Program projection: verified calendar classification against the existing frozen reference.'
_FALLBACK = 'Program projection: retain source quotation without an unverified calendar conversion.'
_LIMITATION = '本轮换算未验证。'


def _report(route, repaired=0, fallback=0):
    return {'route': route, 'repaired_statements': repaired, 'fallback_statements': fallback,
            'codes': (['calendar_classification_corrected'] if repaired else [])
                     + (['calendar_conversion_unverified'] if fallback else [])}


def _strict(review, catalog, first, chosen, question, guard_version):
    from .answer_evidence import reviewed_referenced_answer
    return reviewed_referenced_answer(review, catalog, first, chosen=chosen,
                                     question=question, compose_version=guard_version)


def _hard_review(review, first, catalog, chosen, guard_version):
    """Check *all* original quotes, refs and positions before any soft repair.

    These are the existing provenance/partition guards, separated from calendar
    text validation.  No failed provenance is repaired by the fallback path.
    """
    from .answer_evidence import (FocusedReviewedScopeOutput, ReviewedScopeAnswerOutput,
        _resolve_reference_statements, first_fact_partition, grounded_scope_answer)
    value = FocusedReviewedScopeOutput.model_validate(review)
    positions = []
    for statement in value.statements:
        for use in statement.first_fact_uses:
            if not 1 <= use.fact_index <= len(first.facts):
                raise ValueError('first_fact_position_invalid')
            if not use.reason.strip():
                raise ValueError('first_fact_review_reason_empty')
            positions.append(use.fact_index)
            if use.action != 'correct':
                for original in first.facts[use.fact_index - 1].evidence:
                    if not any(item.unit_id == original.unit_id and original.quote in item.quote
                               and (original.role != 'support' or item.role == 'support')
                               for item in statement.evidence):
                        raise ValueError('first_fact_adoption_quote_missing')
    for group in value.excluded_facts:
        if not group.reason.strip():
            raise ValueError('excluded_fact_reason_empty')
        positions.extend(group.fact_indices)
    if len(set(positions)) != len(positions) or set(positions) != set(range(1, len(first.facts) + 1)):
        raise ValueError('first_fact_review_coverage_invalid')
    resolved, _ = _resolve_reference_statements(value.statements, catalog, chosen, compose_version=guard_version)
    normalized = ReviewedScopeAnswerOutput.model_validate({**value.model_dump(exclude={'statements'}),
        'statements': [{**statement.model_dump(), 'first_fact_uses': raw.first_fact_uses}
                       for statement, raw in zip(resolved, value.statements)]})
    grounded_scope_answer({'statements': [item.model_dump(exclude={'first_fact_uses'})
                                        for item in normalized.statements],
                          'unused': normalized.unused.model_dump(),
                          'uncertain': [item.model_dump() for item in normalized.uncertain]},
                         catalog, first_fact_partition(first.model_dump(), catalog, compose_version=guard_version),
                         chosen=chosen)
    return value


def _can_classify(statement, diagnostic, catalog, chosen, refs, guard_version):
    from .answer_evidence import ReferencedStatement
    from .policies.date_grounding import resolve_date_refs
    if len(statement['date_refs']) != 1:
        return False
    issues = diagnostic['issues']
    if not issues or any(issue.get('matching_date_ref_ids') != statement['date_refs'] for issue in issues):
        return False
    records, _ = resolve_date_refs(ReferencedStatement.model_validate({**statement, 'kind': 'date'}),
                                  catalog, chosen, refs, compose_version=guard_version)
    return len(records) == 1 and len(records[0]['options']) == 1


def _fallback(review, indices, catalog):
    """Display fragments and record labels separately, without person binding."""
    units = {unit['id']: unit for unit in catalog['units']}
    limitations = {}
    for index in indices:
        statement = review['statements'][index]
        parts = []
        for evidence in statement['evidence']:
            unit = units[evidence['unit_id']]
            prefix = re.match(r'^\s*\[D[0-9]+:[0-9]+\]\s*[^:\r\n]{1,60}:\s*', unit['text'])
            speaker = prefix.group().strip() if prefix is not None else ''
            quote = evidence['quote']
            label = '记录层标签（不判定片段内人称）：' + speaker + '\n' if speaker else ''
            parts.append(label + '原文片段：' + quote)
            limitations[(evidence['unit_id'], quote)] = {**evidence, 'role': 'context'}
        statement.update(kind='fact', text='原文引述：\n' + '\n\n'.join(parts), date_refs=[])
        statement['first_fact_uses'][0].update(action='correct', reason=_FALLBACK)
    review['statements'].append({'kind': 'limitation', 'text': _LIMITATION,
        'evidence': list(limitations.values()), 'date_refs': [], 'first_fact_uses': []})


def project_review(output, catalog, first_output, *, chosen, question, compose_version='@35'):
    """Return a guarded review copy and a content-free, fixed-shape report.

    Only a one-position same-core fact with calendar-only diagnostics can be
    projected.  A missing ref is never guessed, a quote is never expanded, and
    source/authorization failures are not caught here.
    """
    from .answer_evidence import FocusedReviewedScopeOutput, inspect_referenced_first_draft
    from .policies.date_grounding import build_date_ref_catalog
    if compose_version not in {'@35', '@36'}:
        raise ValueError('calendar_projection_version_unsupported')
    guard_version = '@36' if compose_version == '@36' else '@34'
    original = deepcopy(FocusedReviewedScopeOutput.model_validate(output).model_dump())
    try:
        _strict(original, catalog, first_output, chosen, question, guard_version)
    except ValueError as error:
        if str(error) not in _SOFT:
            raise
        rejection = str(error)
    else:
        return original, _report('unchanged')
    first, _, diagnostics = inspect_referenced_first_draft(first_output, catalog,
        chosen=chosen, question=question, compose_version=guard_version)
    for item in diagnostics:
        if item['codes'] != [_CALENDAR]:
            raise ValueError(next(code for code in item['codes'] if code != _CALENDAR))
    value = _hard_review(original, first, catalog, chosen, guard_version)
    invalid = {item['fact_index']: item for item in diagnostics}
    targets, covered = [], set()
    for index, statement in enumerate(original['statements']):
        uses = statement['first_fact_uses']
        if len(uses) != 1 or uses[0]['fact_index'] not in invalid:
            continue
        position = uses[0]['fact_index']
        initial = first.facts[position - 1].model_dump()
        if statement['kind'] == 'fact' and all(statement[key] == initial[key] for key in _CORE):
            targets.append((index, position, initial))
            covered.add(position)
    used_invalid = {use.fact_index for statement in value.statements for use in statement.first_fact_uses
                    if use.fact_index in invalid}
    if not targets or covered != used_invalid:
        raise ValueError(rejection)
    refs = build_date_ref_catalog(catalog, chosen, calendar_version=3)
    candidate, repaired, fallback = deepcopy(original), [], []
    for index, position, statement in targets:
        if _can_classify(statement, invalid[position], catalog, chosen, refs, guard_version):
            candidate['statements'][index]['kind'] = 'date'
            candidate['statements'][index]['first_fact_uses'][0].update(action='correct', reason=_CORRECTION)
            repaired.append(index)
        else:
            fallback.append(index)
    if fallback:
        _fallback(candidate, fallback, catalog)
    try:
        _strict(candidate, catalog, first_output, chosen, question, guard_version)
    except ValueError as error:
        if str(error) not in _SOFT or not repaired:
            raise
        # A bounded second local projection may quote all affected facts; no
        # retries, new references, missing fields or fresh evidence are added.
        candidate = deepcopy(original)
        fallback = [index for index, _, _ in targets]
        repaired = []
        _fallback(candidate, fallback, catalog)
        _strict(candidate, catalog, first_output, chosen, question, guard_version)
    return candidate, _report('evidence_fallback' if fallback else 'classification_repair',
                              len(repaired), len(fallback))
