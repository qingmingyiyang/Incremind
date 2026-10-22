"""Conservative single-slot admission for the opt-in initial-memory route.

Only literal numeric declarations in confirmed L3 memory are supported.  This
does not turn lexical coverage, publication, or a model's self-assessment into
a proof of arbitrary natural-language entailment.
"""
from copy import deepcopy
import re

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..recall_state import is_recall_excluded
from .answer_evidence import build_evidence_catalog
from .insight_validity import COLLECTION, from_confirmation, now, read_validity
from .policies.insight_time import instant, valid


_SUBJECT = r'[A-Za-z][A-Za-z0-9_.-]{0,63}'
_LABEL = r'[\u4e00-\u9fffA-Za-z0-9_.-]{1,24}'
_ATTRIBUTE = r'[\u4e00-\u9fff]{2,16}'
_ATTRIBUTES = frozenset({'重试上限', '并发上限', '连接上限', '超时上限',
                         '等待时长', '批次大小', '缓存上限'})
_CONDITIONS = frozenset({'开发环境', '测试环境', '生产环境', '预发布环境'})
_UNIT = r'(?:毫秒|分钟|小时|字节|字符|KB|MB|GB|次|秒|天|个|项|条|%)'
_SPACE = r'[ \t]*'
_QUESTION = re.compile(
    rf'(?P<subject>{_SUBJECT}){_SPACE}(?:在{_SPACE}(?P<condition>{_LABEL}){_SPACE})?的{_SPACE}'
    rf'(?P<attribute>{_ATTRIBUTE}){_SPACE}是多少{_SPACE}[？?]?')
_STATEMENT = re.compile(
    rf'(?P<subject>{_SUBJECT}){_SPACE}的{_SPACE}(?P<attribute>{_ATTRIBUTE}){_SPACE}为{_SPACE}'
    rf'(?P<value>0|[1-9][0-9]{{0,8}}){_SPACE}(?P<unit>{_UNIT}){_SPACE}[。.]?')
_PRONOUNS = {'it', 'they', 'them', 'this', 'that', 'he', 'she', 'we', 'you', 'i'}
_MAX_CORPUS = 512


def _text(value):
    return value.strip(' \t') if isinstance(value, str) else ''


def parse_question(question):
    """Consume the original question completely; unsupported wording stays out."""
    if not isinstance(question, str) or len(question) > 180 or '\n' in question or '\r' in question:
        return None
    match = _QUESTION.fullmatch(_text(question))
    if match is None:
        return None
    result = match.groupdict()
    if (result['subject'].casefold() in _PRONOUNS
            or result['attribute'] not in _ATTRIBUTES
            or result['condition'] is not None and result['condition'] not in _CONDITIONS):
        return None
    return result


def _declaration(content):
    match = _STATEMENT.fullmatch(_text(content))
    if match is None or match['attribute'] not in _ATTRIBUTES:
        return None
    return match.groupdict()


def _decision(status, reason, proof=None):
    return {'status': status, 'reason': reason, 'proof': proof}


def _time_basis(query, identity, reference):
    confirmation = from_confirmation(query.records, identity)
    marker = read_validity(query.records, identity)
    if (confirmation is None or not isinstance(marker, dict) or not valid(marker)
            or marker['valid_from'] != confirmation['valid_from']):
        raise ValueError('initial_confirmation_or_validity_unknown')
    clock, start, end = instant(reference), instant(marker['valid_from']), instant(marker['valid_until'])
    if clock is None or start > clock or end is not None and clock >= end:
        return None
    row = query.records.read(COLLECTION, identity)
    return {'validity': marker, 'validity_revision': row.revision if row else 0,
            'confirmation': confirmation}


def _matching_corpus(query, scope, request, reference):
    entries = query.service.retrieval_entries(scope=scope)
    if not isinstance(entries, (tuple, list)) or len(entries) > _MAX_CORPUS:
        raise ValueError('initial_matching_corpus_incomplete')
    matches = []
    for entry in entries:
        content = _text(entry.get('content'))
        if request['subject'] not in content or request['attribute'] not in content:
            continue
        declaration = _declaration(entry.get('content'))
        if declaration is None:
            raise ValueError('initial_matching_declaration_unknown')
        if any(declaration[key] != request[key] for key in ('subject', 'attribute')):
            continue
        basis = _time_basis(query, entry['id'], reference)
        if basis is None:
            continue
        if (entry.get('authorized') is not True or entry.get('source_evidence_complete') is not True
                or not entry.get('source_evidence')):
            raise ValueError('initial_matching_source_incomplete')
        matches.append({'id': entry['id'], 'revision': entry['revision'],
            'content': entry['content'], 'conditions': list(entry.get('conditions', ())),
            'declaration': declaration, 'source_evidence': deepcopy(entry['source_evidence']), **basis})
    if len({row['id'] for row in matches}) != len(matches):
        raise ValueError('initial_matching_corpus_incomplete')
    identities = {row['id'] for row in matches}
    relations = []
    own_scope = {'user_id': scope.user_id, 'project_id': scope.project_id}
    for row in query.records.list('recognition_relations'):
        edge = row.payload
        if edge.get('scope') != own_scope or not identities & {edge.get('from_id'), edge.get('to_id')}:
            continue
        if edge.get('relation') == 'refutes':
            # Missing or ineligible counterparts do not establish resolution.
            raise ValueError('initial_unresolved_refutation')
        if edge.get('relation') == 'supersedes':
            relations.append({'id': row.object_id, 'revision': row.revision,
                              'from_id': edge['from_id'], 'to_id': edge['to_id'],
                              'relation': edge['relation']})
    return sorted(matches, key=lambda row: row['id']), sorted(relations, key=lambda row: row['id'])


def assess(query, plan, *, reference=None):
    """Admit only an existing selected literal L3 fact with a complete binding."""
    request = parse_question(plan.get('question'))
    if request is None:
        return _decision('unsupported', 'question_shape_unsupported')
    if plan.get('history') or plan.get('part_context'):
        return _decision('unsupported', 'context_dependent_question')
    if plan.get('policy_versions', {}).get('compose') != '@34':
        return _decision('unsupported', 'compose_contract_unsupported')
    scope = plan.get('scope')
    if type(scope) is not WorkScope or scope != WorkScope('local-user', plan.get('project_id')):
        return _decision('insufficient', 'scope_unavailable')
    reference = reference or now()
    clock = instant(reference)
    if clock is None:
        return _decision('insufficient', 'reference_time_unknown')
    reference = clock.isoformat()
    try:
        corpus, relations = _matching_corpus(query, scope, request, reference)
        if not corpus:
            return _decision('insufficient', 'no_current_confirmed_slot')
        values = {(row['declaration']['value'], row['declaration']['unit']) for row in corpus}
        if len(values) != 1:
            return _decision('insufficient', 'matching_slot_values_conflict')
        expected_conditions = [request['condition']] if request['condition'] is not None else []
        for candidate in plan.get('chosen', ()):
            if (candidate.get('kind') != 'recognition' or candidate.get('layer') != 'L3'
                    or candidate.get('scope') != scope or candidate.get('persona')
                    or candidate.get('bookshelf') or candidate.get('inspiration')):
                continue
            entry = candidate.get('entry', {})
            current = query.service.get_recognition(scope=scope, recognition_id=candidate['id'])
            if (current is None or not current.authorized or not current.source_evidence_complete
                    or not current.source_evidence or current.retrieval_projection() != entry
                    or is_recall_excluded(query.records, scope, candidate['id'])
                    or list(current.conditions) != expected_conditions):
                continue
            matching = next((row for row in corpus if row['id'] == candidate['id']), None)
            if matching is None:
                continue
            catalog = build_evidence_catalog([candidate], compose_version='@34')
            if len(catalog['units']) != 1:
                continue
            unit = catalog['units'][0]
            proof = {'version': 1, 'question': plan['question'], 'request': request,
                'reference': reference, 'source_id': candidate['id'], 'source_revision': current.revision,
                'statement': current.content, 'slot': matching['declaration'],
                'conditions': list(current.conditions), 'unit_id': unit['id'], 'quote': unit['text'],
                'coordinate_space': unit['coordinate_space'], 'span': unit['span'],
                'matching_corpus': corpus, 'relations': relations}
            return _decision('sufficient', 'confirmed_literal_slot_complete', proof)
        return _decision('insufficient', 'selected_confirmed_slot_unavailable')
    except (RecognitionError, ValueError, TypeError, KeyError, AttributeError, OSError) as failure:
        return _decision('insufficient', str(failure) if isinstance(failure, ValueError)
                         and str(failure).startswith('initial_') else 'evidence_owner_unavailable')


def validate_plan(query, plan):
    proof = plan.get('initial_answer_proof')
    if proof is None:
        return
    if not isinstance(proof, dict):
        raise RecognitionConflict('initial_answer_evidence_changed')
    decision = assess(query, plan, reference=proof.get('reference'))
    if decision['status'] != 'sufficient' or decision['proof'] != proof:
        raise RecognitionConflict('initial_answer_evidence_changed')


def validate_output(plan, output, catalog):
    """The normal reviewed answer must actually answer the admitted literal slot."""
    proof = plan.get('initial_answer_proof')
    if proof is None:
        return
    value = output.model_dump() if hasattr(output, 'model_dump') else output
    try:
        statements = value['statements']
        if len(statements) != 1:
            raise ValueError('initial_answer_output_incomplete')
        statement = statements[0]
        unit = next(unit for unit in catalog['units'] if unit['id'] == proof['unit_id'])
        if (len(catalog['sources']) != 1 or unit['source_number'] != 1
                or unit['text'] != proof['quote'] or unit['coordinate_space'] != proof['coordinate_space']
                or unit['span'] != proof['span'] or statement['kind'] != 'fact'
                or _declaration(statement['text']) != proof['slot']
                or not any(item.get('role') == 'support' and item.get('unit_id') == proof['unit_id']
                           and item.get('quote') == proof['quote'] for item in statement['evidence'])):
            raise ValueError('initial_answer_output_incomplete')
    except (KeyError, TypeError, StopIteration, AttributeError) as failure:
        raise ValueError('initial_answer_output_incomplete') from failure
