"""Read confirmation facts and write only the validity sidecar under owner transactions."""
from datetime import datetime, timezone
from .policies import get

COLLECTION = 'v2_insight_validity'


def now():
    return datetime.now(timezone.utc).isoformat()


def from_confirmation(reader, identity):
    row = reader.read('recognitions', identity)
    if row is None:
        return None
    version = reader.read('recognition_versions', identity + '~v1')
    confirmation = (version.payload.get('snapshot', {}).get('created_at') if version
        and version.payload.get('recognition_id') == identity and version.payload.get('version') == 1
        and version.payload.get('action') == 'publish'
        and version.payload.get('snapshot', {}).get('scope') == row.payload.get('scope') else None)
    if confirmation is None:
        # Only an actual published alias establishes confirmation for old imports.
        confirmations = [item.payload.get('reviewed_at') for item in _matching(reader, 'recognition_candidates', recognition_id=identity)
                         if item.payload.get('state') == 'published'
                         and item.payload.get('recognition_id') == identity
                         and item.payload.get('scope') == row.payload.get('scope')]
        confirmation = min((value for value in confirmations if isinstance(value, str)), default=None)
    return {'valid_from': confirmation, 'valid_until': None, 'superseded_by': None} if confirmation else None


def _matching(reader, collection, **fields):
    return (reader.list_matching(collection, **fields) if hasattr(reader, 'list_matching')
            else [row for row in reader.list(collection) if all(row.payload.get(key) == value for key, value in fields.items())])


def read_validity(reader, identity):
    row = reader.read(COLLECTION, identity)
    return dict(row.payload) if row else historical_validity(reader, identity)


def historical_validity(reader, identity):
    marker = from_confirmation(reader, identity)
    old = reader.read('recognitions', identity)
    if marker is None or old is None:
        return None
    replacements = []
    for row in _matching(reader, 'recognition_relations', to_id=identity):
        edge = row.payload
        if edge.get('relation') != 'supersedes' or edge.get('to_id') != identity:
            continue
        target = reader.read('recognitions', edge.get('from_id'))
        if (target and target.payload.get('scope', {}).get('user_id') == old.payload['scope']['user_id']
                and edge.get('scope', {}).get('user_id') == old.payload['scope']['user_id']):
            replacements.append((edge['created_at'], target.object_id))
    interference = reader.read('v2_insight_interference', identity)
    if interference:
        target = reader.read('recognitions', interference.payload.get('superseding_id'))
        if target and target.payload.get('scope', {}).get('user_id') == old.payload['scope']['user_id']:
            replacements.append((interference.payload['confirmed_at'], target.object_id))
    if replacements:
        at, target = min(replacements)
        marker.update(valid_until=at, superseded_by=target)
    return marker


def close(transaction, old_id, new_id, confirmed_at):
    old, new = transaction.read('recognitions', old_id), transaction.read('recognitions', new_id)
    # Existing relation review owns project/me admission; this helper preserves it.
    if (old is None or new is None
            or old.payload.get('scope', {}).get('user_id') != new.payload.get('scope', {}).get('user_id')):
        raise ValueError('validity_endpoint_scope_mismatch')
    previous = transaction.read(COLLECTION, old_id)
    marker = dict(previous.payload) if previous else historical_validity(transaction, old_id)
    if marker is None:
        raise ValueError('validity_confirmation_unavailable')
    # The first replacement ends this interval; later links do not reopen history.
    if marker['valid_until'] is not None and previous:
        return
    updated = marker if marker['valid_until'] is not None else {
        **marker, 'valid_until': confirmed_at, 'superseded_by': new_id}
    if not get('retrieve', version='@3')(None, updated, operation='validity'):
        raise ValueError('invalid_insight_validity')
    transaction.put(COLLECTION, old_id, updated,
                    expected_revision=previous.revision if previous else 0)


def backfill(records):
    """Read old confirmations/interference; never rewrite original facts or existing sidecars."""
    added = 0
    with records.begin() as tx:
        for row in tx.list('recognitions'):
            if tx.read(COLLECTION, row.object_id):
                continue
            marker = historical_validity(tx, row.object_id)
            if marker is None or not get('retrieve', version='@3')(None, marker, operation='validity'):
                continue
            tx.put(COLLECTION, row.object_id, marker, expected_revision=0)
            added += 1
        tx.commit()
    return {'added': added, 'total': len(records.list(COLLECTION))}
