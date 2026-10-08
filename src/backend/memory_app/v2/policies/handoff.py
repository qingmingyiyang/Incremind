"""Pure, unregistered handoff@1 framing for already-qualified evidence.

The caller owns scope, egress permission, source authority and token counting.
This policy only admits complete JSON entries within the budget for ``text``.
It neither retrieves data nor marks delivered entries as used.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import json


DEFAULT_BUDGET = 3000
MAX_BUDGET = 12000
_FIELDS = {"object_id", "layer", "title", "excerpt", "sources", "revision", "conditions"}


def _entry(value: Mapping) -> dict:
    if (not isinstance(value, Mapping) or set(value) != _FIELDS
            or not isinstance(value["layer"], str) or value["layer"] not in {"L0", "L1", "L2", "L3"}
            or type(value["revision"]) is not int or value["revision"] < 1
            or any(not isinstance(value[name], str) or not value[name]
                   for name in ("object_id", "title", "excerpt"))
            or not isinstance(value["conditions"], list)
            or any(not isinstance(condition, str) or not condition for condition in value["conditions"])
            or not isinstance(value["sources"], list) or not value["sources"]):
        raise ValueError("invalid_handoff_entry")
    for source in value["sources"]:
        if (not isinstance(source, dict) or not {"type", "id", "revision"} <= set(source)
                or any(not isinstance(source[name], str) or not source[name] for name in ("type", "id"))
                or type(source["revision"]) is not int or source["revision"] < 1):
            raise ValueError("invalid_handoff_entry")
    result = deepcopy(dict(value))
    # JSON round-tripping must never repair keys, tuples or unsupported values.
    def valid_json(item):
        if isinstance(item, dict):
            return all(isinstance(key, str) and valid_json(part) for key, part in item.items())
        if isinstance(item, list):
            return all(valid_json(part) for part in item)
        return item is None or type(item) in {str, bool, int, float}
    try:
        if not valid_json(result):
            raise ValueError("invalid_handoff_entry")
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_handoff_entry") from error
    return result


def _text(entries, profile):
    if not entries and not profile:
        return ""
    return json.dumps({"entries": entries, "profile": profile}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False)


def v1(entries: Sequence[Mapping], *, count_tokens: Callable[[str], int],
       profile: Sequence[Mapping] = (), budget: int = DEFAULT_BUDGET) -> dict:
    """Preserve ranked input order and metadata; omit oversized entries whole.

    Profile entries are considered first and use P identifiers. Ordinary
    evidence uses M identifiers. Both share the complete serialized budget.
    The injected counter must be the same pure counter used by the caller.
    """
    if type(budget) is not int or budget < 1:
        raise ValueError("invalid_handoff_budget")
    budget = min(budget, MAX_BUDGET)
    if (not callable(count_tokens) or isinstance(entries, (str, bytes))
            or not isinstance(entries, Sequence) or isinstance(profile, (str, bytes))
            or not isinstance(profile, Sequence)):
        raise ValueError("invalid_handoff_entry")
    rows, people = [_entry(row) for row in entries], [_entry(row) for row in profile]
    selected, personas, tokens = [], [], 0
    for values, target, prefix in ((people, personas, "P"), (rows, selected, "M")):
        for row in values:
            proposed = {"id": f"{prefix}{len(target) + 1}", **row}
            ordinary = [*selected, proposed] if prefix == "M" else selected
            personal = [*personas, proposed] if prefix == "P" else personas
            cost = count_tokens(_text(ordinary, personal))
            if type(cost) is not int or cost < 0:
                raise ValueError("invalid_handoff_token_count")
            if cost <= budget:
                target.append(proposed)
                tokens = cost
    return {"version": "handoff@1", "budget": budget, "entries": selected,
        "profile": personas, "tokens": tokens, "text": _text(selected, personas)}


def v2(projects: Sequence[Mapping], *, count_tokens: Callable[[str], int],
       budget: int = DEFAULT_BUDGET) -> dict:
    """Admit complete catalog metadata rows, with no memory IDs or usage facts."""
    if type(budget) is not int or budget < 1 or not callable(count_tokens):
        raise ValueError('invalid_handoff_budget')
    budget = min(budget, MAX_BUDGET)
    if isinstance(projects, (str, bytes)) or not isinstance(projects, Sequence):
        raise ValueError('invalid_handoff_catalog')
    rows, seen = [], set()
    for row in projects:
        if (not isinstance(row, Mapping) or set(row) != {'id', 'name', 'overview', 'scenes'}
                or any(not isinstance(row[key], str) or not row[key] for key in ('id', 'name', 'overview'))
                or row['id'] in seen or not isinstance(row['scenes'], list)):
            raise ValueError('invalid_handoff_catalog')
        seen.add(row['id'])
        for scene in row['scenes']:
            if (not isinstance(scene, dict) or set(scene) != {'name', 'overview'}
                    or any(not isinstance(scene[key], str) or not scene[key] for key in scene)):
                raise ValueError('invalid_handoff_catalog')
        rows.append(deepcopy(dict(row)))
    selected, text, tokens = [], '', 0
    for row in rows:
        proposed = [*selected, row]
        body = json.dumps({'projects': proposed}, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False)
        cost = count_tokens(body)
        if type(cost) is not int or cost < 0:
            raise ValueError('invalid_handoff_token_count')
        if cost <= budget:
            selected, text, tokens = proposed, body, cost
    return {'version': 'handoff@2', 'budget': budget, 'entries': [], 'profile': [],
        'projects': selected, 'tokens': tokens, 'text': text}
