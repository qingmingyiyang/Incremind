"""Exact retained-experience copy links, without widening ordinary work scopes."""
import re


class ExperienceOriginError(ValueError):
    pass


_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
COPY_ID = re.compile(r'^experience-copy-v2-[0-9a-f]{32}$')
_FIELDS = {'source_user_id', 'source_project_id', 'source_experience_id', 'source_revision',
           'target_project_id', 'target_experience_id'}


def read_experience_origin(reader, copied):
    """Return a verified (marker, original) pair; absent markers keep old behavior."""
    marker = reader.read('v2_experience_origins', copied.object_id)
    if marker is None:
        if COPY_ID.fullmatch(copied.object_id):
            raise ExperienceOriginError('retained experience copy has no origin')
        return None
    value = marker.payload
    scope = copied.payload.get('scope', {})
    if (copied.payload.get('id') != copied.object_id
            or set(value) != _FIELDS or type(value['source_revision']) is not int or value['source_revision'] < 1
            or any(not isinstance(value[key], str) or not _ID.fullmatch(value[key]) for key in _FIELDS - {'source_revision'})
            or value['source_user_id'] != scope.get('user_id')
            or value['target_project_id'] != scope.get('project_id')
            or value['target_experience_id'] != copied.object_id
            or value['source_project_id'] == value['target_project_id']
            or value['source_experience_id'] == copied.object_id):
        raise ExperienceOriginError('experience origin identity is invalid')
    original = reader.read('recognition_experiences', value['source_experience_id'])
    if (original is None or original.revision != value['source_revision']
            or original.payload.get('scope') != {'user_id': value['source_user_id'], 'project_id': value['source_project_id']}
            or original.payload.get('state') != 'active'
            or copied.payload.get('content') != original.payload.get('content')
            or copied.payload.get('provenance') != original.payload.get('provenance')):
        raise ExperienceOriginError('experience origin is unavailable or changed')
    return marker, original
