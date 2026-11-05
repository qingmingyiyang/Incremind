from jsonschema import Draft202012Validator

from .realtime_asr_lexicon import MAX_SESSION_TERMS, MAX_SUPER_TERMS, SUPER_WEIGHT, _term


def _operation(action, required, extra=None):
    properties = {'action': {'const': action}, 'term': {'type': 'string', 'minLength': 1}}
    properties.update(extra or {})
    return {'type': 'object', 'additionalProperties': False,
        'required': ['action', 'term', *required], 'properties': properties}


_WEIGHT = {'type': 'integer', 'enum': [1, 2, 3, 4, 5, SUPER_WEIGHT]}
MAINTENANCE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['operations'],
    'properties': {'operations': {'type': 'array', 'minItems': 1, 'maxItems': MAX_SESSION_TERMS,
        'items': {'oneOf': [
            _operation('add', [], {'weight': _WEIGHT}),
            _operation('rename', ['new_term'], {'new_term': {'type': 'string', 'minLength': 1}, 'weight': _WEIGHT}),
            _operation('set_weight', ['weight'], {'weight': _WEIGHT}),
            _operation('retire', []),
            _operation('restore', [], {'weight': _WEIGHT}),
        ]}}}}
_VALIDATOR = Draft202012Validator(MAINTENANCE_SCHEMA)


class LexiconChangeError(ValueError):
    pass


def validate_operations(operations):
    if not _VALIDATOR.is_valid({'operations': operations}):
        raise LexiconChangeError('invalid_asr_lexicon_changes')
    touched = set()
    for operation in operations:
        names = [operation['term']]
        if operation['action'] == 'rename':
            names.append(operation['new_term'])
        current = set()
        for name in names:
            clean, normalized = _term(name)
            clean.encode('utf-8')
            current.add(normalized)
        if touched & current:
            raise LexiconChangeError('asr_lexicon_change_conflict')
        touched.update(current)


def _view(value):
    if value is None:
        return None
    return {'term': value.term, 'weight': value.weight, 'status': value.status}


def apply_operations(domain, operations):
    validate_operations(operations)
    changes = []
    current = {row.term.casefold(): row for row in domain.list_terms()}
    for operation in operations:
        action = operation['action']
        term, normalized = _term(operation['term'])
        old = current.get(normalized)
        if action == 'add':
            if old is not None and old.status == 'active':
                raise LexiconChangeError('asr_lexicon_term_exists')
            weight = operation.get('weight', old.weight if old else 4)
            new = domain.manual_upsert(term, weight=weight, is_super=weight == SUPER_WEIGHT)
        else:
            if old is None:
                raise LexiconChangeError('asr_lexicon_term_not_found')
            weight = operation.get('weight', old.weight)
            if action == 'retire':
                new = domain.retire(old.term)
            elif action == 'rename':
                target, target_key = _term(operation['new_term'])
                if target_key != normalized and target_key in current:
                    raise LexiconChangeError('asr_lexicon_term_exists')
                new = domain.rename(old.term, target, weight=weight)
                current.pop(normalized)
            elif action == 'set_weight':
                if old.status != 'active':
                    raise LexiconChangeError('asr_lexicon_term_retired')
                new = old if old.weight == weight else domain.manual_upsert(
                    old.term, weight=weight, is_super=weight == SUPER_WEIGHT)
            else:
                new = old if old.status == 'active' and old.weight == weight else domain.manual_upsert(
                    old.term, weight=weight, is_super=weight == SUPER_WEIGHT)
        current[new.term.casefold()] = new
        changes.append({'action': action, 'before': _view(old), 'after': _view(new)})
    active = [row for row in current.values() if row.status == 'active']
    if len(active) > MAX_SESSION_TERMS or sum(row.weight == SUPER_WEIGHT for row in active) > MAX_SUPER_TERMS:
        raise LexiconChangeError('asr_lexicon_capacity_exceeded')
    return changes
