from collections.abc import Mapping
from uuid import UUID

from backend.recognition import RecognitionConflict

PREFIX = 'workspace-route-'
ROUTE_ORIGINAL_OWNER_FIELDS = frozenset({'route_original_revision', 'route_correction_operation_id',
    'route_correction_part_index', 'route_correction_child_id', 'route_original_birth_revision'})


def route_original_id(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise RecognitionConflict('route_original_id_invalid')
    suffix = value[len(PREFIX):]
    try:
        parsed = UUID(suffix)
    except ValueError:
        raise RecognitionConflict('route_original_id_invalid') from None
    if parsed.hex != suffix:
        raise RecognitionConflict('route_original_id_invalid')
    return value


def validate_route_original_metadata(value, identifier, revision):
    if not isinstance(value, Mapping) or set(value) != ROUTE_ORIGINAL_OWNER_FIELDS:
        raise RecognitionConflict('route_original_graph_owner_invalid')
    identifier(value['route_correction_operation_id'])
    identifier(value['route_correction_child_id'])
    if revision(value['route_original_revision']) != 1 or revision(value['route_original_birth_revision']) != 1:
        raise RecognitionConflict('route_original_graph_birth_invalid')
    revision(value['route_correction_part_index'], minimum=0)
