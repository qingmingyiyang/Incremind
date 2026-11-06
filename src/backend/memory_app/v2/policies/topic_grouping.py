from math import fsum, hypot, isfinite
from unicodedata import normalize


_ASSIGN_SIMILARITY = .72
_CLUSTER_SIMILARITY = .82
_MIN_CLUSTER_SIZE = 10
_QUERY_SIMILARITY = .55
_BONUS = 1.08
_PLAN_LABEL_LIMIT = 32


def label(value):
    if not isinstance(value, str):
        raise ValueError('topic_group_label_invalid')
    value = ' '.join(normalize('NFKC', value).split())
    if not 1 <= len(value) <= 24:
        raise ValueError('topic_group_label_invalid')
    return value


def labels(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 3:
        raise ValueError('topic_group_labels_invalid')
    result = [label(value) for value in values]
    if len({value.casefold() for value in result}) != len(result):
        raise ValueError('topic_group_labels_invalid')
    return result


def frequent_labels(counts):
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    return tuple(name for name, _ in ordered[:_PLAN_LABEL_LIMIT])


def _norm(vector):
    length = hypot(*vector)
    return length if length and isfinite(length) else None


def _cosine(left, right):
    if len(left) != len(right) or not (a := _norm(left)) or not (b := _norm(right)):
        return -1.0
    return fsum((x / a) * (y / b) for x, y in zip(left, right))


def _center(ids, vectors):
    members = [vectors[identity] for identity in ids if identity in vectors]
    return [fsum(value / len(members) for value in column) for column in zip(*members)] if members else []


def _closest(vector, centers):
    return sorted(((_cosine(vector, center), key) for key, center in centers.items() if center),
                  key=lambda item: (-item[0], item[1]))


def _nearest(vector, centers):
    score, key = min(((-_cosine(vector, center), key) for key, center in centers.items() if center),
                     default=(1.0, ''))
    return -score, key


def build(rows):
    named, untagged, vectors = {}, [], {}
    dimensions = None
    for row in sorted(rows, key=lambda value: value['id']):
        identity, vector = row['id'], row['vector']
        if vector and _norm(vector):
            if dimensions is None:
                dimensions = len(vector)
            if len(vector) == dimensions:
                vectors[identity] = vector
        if row['tags']:
            for tag in row['tags']:
                group = named.setdefault('topic:' + tag.casefold(), {'label': tag, 'members': []})
                group['members'].append(identity)
        else:
            untagged.append(identity)

    centers = {key: _center(group['members'], vectors) for key, group in named.items()}
    far = []
    for identity in untagged:
        if identity not in vectors:
            continue
        score, key = _nearest(vectors[identity], centers)
        if score >= _ASSIGN_SIMILARITY:
            named[key]['members'].append(identity)
        else:
            far.append(identity)

    unnamed, prototypes = {}, {}
    for identity in far:
        vector = vectors[identity]
        score, key = _nearest(vector, prototypes)
        if score >= _CLUSTER_SIMILARITY:
            unnamed[key].append(identity)
        else:
            key = 'anonymous:' + identity
            prototypes[key] = vector
            unnamed[key] = [identity]
    groups = dict(named)
    for key, members in unnamed.items():
        if len(members) >= _MIN_CLUSTER_SIZE:
            center = _center(members, vectors)
            members = [identity for identity in members
                       if _cosine(vectors[identity], center) >= _CLUSTER_SIMILARITY]
            if len(members) >= _MIN_CLUSTER_SIZE:
                groups[key] = {'label': None, 'members': members}
    return {key: {**group, 'center': _center(group['members'], vectors)} for key, group in groups.items()}


def match(groups, question_vector):
    if not question_vector:
        return frozenset()
    selected = [key for score, key in _closest(question_vector, {
        key: group['center'] for key, group in groups.items()}) if score >= _QUERY_SIMILARITY][:3]
    return frozenset(identity for key in selected for identity in groups[key]['members'])


def bonus(score):
    return score * _BONUS
