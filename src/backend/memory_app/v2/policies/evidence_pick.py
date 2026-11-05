from datetime import datetime

from core.search_and_recall.evidence_windows import query_terms
from .insight_time import UTC, instant


def v1(statement, candidates):
    terms = {word for word, _ in query_terms(statement)}
    ordered = sorted(candidates,
        key=lambda row: instant(row.get('created_at')) or datetime.min.replace(tzinfo=UTC), reverse=True)
    ordered.sort(key=lambda row: -len(terms & {word for word, _ in query_terms(row['text'])}))
    selected, seen = [], set()
    for row in ordered:
        identity = (row['document_id'], row['source_id'], row['start'], row['end'])
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(row)
        if len(selected) == 3:
            break
    return selected
