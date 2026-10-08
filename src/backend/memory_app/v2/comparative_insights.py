"""Detached structured output and public project descriptions for extract@2."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr

from .privacy import is_private_project


class ComparativeInsight(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['supplement', 'differs', 'new_method']
    relation: Literal['new', 'supplement', 'differs', 'may_supersede']
    text: StrictStr
    conditions: list[StrictStr]
    target_id: StrictStr | None
    scope_hint: StrictStr | None


class EvidenceSupport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_id: StrictStr
    evidence: StrictStr


class ComparativeOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    insights: list[ComparativeInsight]
    supports: list[EvidenceSupport]


def public_projects(records, documents, models):
    """Bound descriptions before rendering: private names are never read."""
    from .overviews import ScopeOverviews
    overviews = ScopeOverviews(records, documents, models)
    result = []
    for row in sorted(records.list('v2_projects'), key=lambda row: row.object_id):
        if row.object_id in {'me', 'inbox'} or is_private_project(records, row.object_id):
            continue
        overview = overviews.current(row.object_id)
        text = overview.get('text', '') if overview else ''
        sentence = re.split(r'(?<=[。！？.!?])\s*|[\r\n]', text, maxsplit=1)[0] if isinstance(text, str) else ''
        result.append({'id': row.object_id, 'name': row.payload['name'], 'overview': sentence})
        if len(result) == 20:
            break
    return result
