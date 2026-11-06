from core.document_engine import SQLiteDocumentRepository
from core.document_engine.markdown_sections import subsections_of

from .policies.material_angles import CORE_ANGLES as CORE_ANGLES


FIELDS = 'v2_material_fields'
SECTIONS = 'v2_angle_sections'
INSIGHTS = 'v2_insight_angles'
CANDIDATE_EVIDENCE = 'v2_material_candidate_evidence'
PUBLICATIONS = 'v2_material_analysis_publications'


def interpretation_ids(analysis):
    if analysis is None or 'summary_by_angle' not in analysis['plan']:
        return frozenset()
    return frozenset(row['id'] for row in analysis['plan']['angles']
        if row['kind'] == 'interpret' and row['present'])


def _extract_fields(publication):
    payload = publication.payload
    analysis, original = payload['analysis'], payload['source_text']
    results, seen = [], set()

    def add(kind, value, evidence, literal=None):
        literal = value if literal is None else literal
        quote = evidence['quote']
        left, right = evidence['start'], evidence['end']
        if not 0 <= left < right <= len(original) or original[left:right] != quote:
            raise ValueError('material_analysis_binding_changed')
        offset = quote.find(literal) if literal else -1
        while offset >= 0:
            start = left + offset
            end = start + len(literal)
            key = (kind, value, start, end)
            if key not in seen:
                seen.add(key)
                results.append({'kind': kind, 'value': value, 'source_id': payload['item_id'],
                    'start': start, 'end': end, 'quote': literal, 'coordinate_space': 'workspace_source_text_v1'})
            offset = quote.find(literal, offset + len(literal))

    for claim in analysis['claims']:
        for evidence in claim['evidence']:
            for kind, values in (('actor', claim['actors']), ('entity', claim['entities']), ('term', claim.get('terms', []))):
                for value in values:
                    add(kind, value, evidence)
    for event in analysis['temporal']:
        evidence = event['evidence']
        for actor in event['actors']:
            add('actor', actor, evidence)
        expression = event['expression']
        if expression is None:
            continue
        add('date', expression, evidence)
        if event['computed']:
            add('date', event['computed'], evidence, expression)
        for option in event['event_time']['options']:
            add('date', option['start'], evidence, expression)
            add('date', option['end'], evidence, expression)
    return results


def _section_ranges(analysis, markdown):
    sections = subsections_of(markdown, '材料分析')
    return [{'angle_id': row['id'], 'start': sections[row['label']][0], 'end': sections[row['label']][1]}
        for row in analysis['plan']['angles'] if row['present'] and row['label'] in sections]


def _directory(analysis):
    angles = [row for row in analysis['plan']['angles'] if row['present']]
    counts = dict.fromkeys((row['id'] for row in angles), 0)
    for claim in analysis['claims']:
        for identity in claim['angle_ids']:
            if identity in counts:
                counts[identity] += 1
    if 'time_place' in counts:
        counts['time_place'] += len(analysis['temporal'])
    parts = [row['label'] + ' ' + str(counts[row['id']]) for row in angles if counts[row['id']]]
    return '材料角度 · ' + ' · '.join(parts) if parts else ''


def _projection_binding(document, publication):
    return {'project_id': document['project_id'], 'document_revision': document['revision'],
        'analysis_id': publication.object_id, 'analysis_revision': publication.revision,
        'policy_version': publication.payload['generation']['policy_versions']['material_analysis']}


def save_document_angles(tx, document, publication, markdown):
    analysis = publication.payload['analysis']
    if 'summary_by_angle' not in analysis['plan']:
        return
    binding = _projection_binding(document, publication)
    identity = document['id']
    for collection, payload in (
        (FIELDS, {**binding, 'fields': _extract_fields(publication), 'directory': _directory(analysis)}),
        (SECTIONS, {**binding, 'sections': _section_ranges(analysis, markdown)}),
    ):
        previous = tx.read(collection, identity)
        if previous is None or previous.payload != payload:
            tx.put(collection, identity, payload, expected_revision=previous.revision if previous else 0)


def _publication(reader, document, projection):
    if projection is None or projection.payload['project_id'] != document['project_id']:
        return None
    publication = reader.read(PUBLICATIONS, projection.payload['analysis_id'])
    if (publication is None or publication.payload['document_id'] != document['id']
            or publication.payload['project_id'] != document['project_id']
            or 'summary_by_angle' not in publication.payload['analysis']['plan']):
        return None
    original = reader.read('workspace_items', publication.payload['item_id'])
    if (original is None or original.payload['project_id'] != document['project_id']
            or original.payload.get('status') != 'confirmed'
            or original.payload['source_text'] != publication.payload['source_text']):
        return None
    return publication


def refresh_document_angles(tx, document, markdown):
    publication = _publication(tx, document, tx.read(FIELDS, document['id']))
    if publication is not None:
        save_document_angles(tx, document, publication, markdown)


def _read_projection(reader, collection, project_id, document_id, revision, key):
    projection = reader.read(collection, document_id)
    if projection is None or projection.payload.get('project_id') != project_id:
        return ()
    row = reader.read('documents', document_id)
    if row is None or row.payload.get('project_id') != project_id or row.payload['revision'] != revision:
        return ()
    document = row.payload
    publication = _publication(reader, document, projection)
    if publication is None:
        return ()
    binding = _projection_binding(document, publication)
    if all(projection.payload.get(name) == value for name, value in binding.items()):
        return projection.payload[key]
    if key == 'sections':
        markdown = SQLiteDocumentRepository(reader).markdown(document_id, revision=revision)
        return _section_ranges(publication.payload['analysis'], markdown) if markdown is not None else ()
    return _extract_fields(publication) if key == 'fields' else _directory(publication.payload['analysis'])


def material_fields(reader, project_id, document_id, revision):
    return tuple(_read_projection(reader, FIELDS, project_id, document_id, revision, 'fields'))


def angle_sections(reader, project_id, document_id, revision):
    return tuple(_read_projection(reader, SECTIONS, project_id, document_id, revision, 'sections'))


def angle_directory(reader, project_id, document_id, revision):
    return _read_projection(reader, FIELDS, project_id, document_id, revision, 'directory') or ''


def publish_insight_angles(tx, scope, candidate, recognition):
    evidence = tx.read(CANDIDATE_EVIDENCE, candidate.object_id)
    if (evidence is None or evidence.payload.get('project_id') != scope.project_id
            or evidence.payload.get('candidate_revision') != candidate.revision
            or not evidence.payload.get('angle_ids')):
        return
    tx.put(INSIGHTS, recognition.object_id, {'project_id': scope.project_id,
        'recognition_revision': recognition.revision, 'angle_ids': evidence.payload['angle_ids'],
        'claim_ids': evidence.payload['claim_ids']}, expected_revision=0)


def insight_angles(reader, project_id, recognition_id, revision):
    projection = reader.read(INSIGHTS, recognition_id)
    if (projection is None or projection.payload['project_id'] != project_id
            or projection.payload['recognition_revision'] != revision):
        return ()
    record = reader.read('recognitions', recognition_id)
    if (record is None or record.revision != revision or record.payload.get('project_id') != project_id
            or record.payload.get('state') != 'active'):
        return ()
    return tuple(projection.payload['angle_ids'])
