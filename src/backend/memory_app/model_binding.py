from collections.abc import Mapping
from copy import deepcopy


CONFIGURATION_FIELDS = ('purpose', 'provider', 'base_url', 'model', 'allow_remote',
                        'revision', 'configured', 'has_api_key')
OPTIONAL_BINDINGS = ('subscription_binding', 'server_model_binding')


def public_model_configuration(value):
    result = {key: deepcopy(value.get(key)) for key in CONFIGURATION_FIELDS}
    for key in OPTIONAL_BINDINGS:
        if value.get(key) is not None:
            result[key] = deepcopy(value[key])
    return result


def valid_public_configuration_fields(value):
    if (not isinstance(value, Mapping) or not set(CONFIGURATION_FIELDS) <= set(value)
            or set(value) - set(CONFIGURATION_FIELDS) - set(OPTIONAL_BINDINGS)):
        return False
    if 'server_model_binding' in value:
        from .v2.server_models import validate_server_model_binding
        try:
            validate_server_model_binding(value['server_model_binding'])
        except ValueError:
            return False
    return True
