import json
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import RecognitionConflict
from backend.recognition_retrieval.service import _required_string


def material_vector_namespace(query, namespace, text, model_id):
    if query.origin_admission is None:
        return namespace
    query.validate_retrieval_origin()
    if not all(isinstance(value, str) and value for value in (namespace, text, model_id)):
        raise RecognitionConflict('shared_vector_rendered_input_invalid')
    text = _required_string(text, 'content')
    admission = query.origin_admission
    body = {'schema_version': 1, 'role': 'embedding-material', 'space': namespace, 'model': model_id,
        'rendered_input': text, 'source': admission.lease.payload(), 'execution': admission.personal}
    return 'shared-rendered-vector-v1-' + uuid5(NAMESPACE_URL,
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))).hex


def rendered_chunk_text(entry, summary, text):
    return entry['title'] + '\n' + str(summary)[:120] + '\n' + text


def indexed_summary(reader, project, entry, summary):
    if entry['kind'] != 'document' or not entry.get('item_id'):
        return summary
    from .material_angles import angle_directory
    directory = angle_directory(reader, project, entry['id'], entry['revision'])
    return directory + '\n' + summary if directory else summary


def contextual_cache_prefix(summary, indexed):
    return indexed[:120] if indexed[:120] != summary[:120] else None
