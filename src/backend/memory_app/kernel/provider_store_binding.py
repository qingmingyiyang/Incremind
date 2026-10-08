"""Freeze body-free auxiliary storage selection, without dispatch authority."""
from copy import deepcopy

from backend.shared.llm.openai_responses import ResponsesCompletion
from ..model_config import ModelConfigurationError, ProviderStoreActivation, _generation_execution_location
from ..turn_routing import CONFIGURATION_FIELDS, SNAPSHOT_KIND, RecognitionRoutingSnapshot, _revision as _routing_revision
from .aux_routing import CHOICES, CHOICE_KIND, ROUTE_KIND


BINDINGS = 'v2_provider_store_bindings'
_SETTINGS = 'v2_provider_store_settings'
_FIELDS = {'schema_version', 'turn_id', 'project_id', 'kind', 'enabled',
    'selection_revision', 'parent', 'auxiliary', 'configuration', 'adapter'}
_IDENTITIES = ('parent', 'auxiliary', 'configuration', 'adapter')


def _invalid():
    raise ModelConfigurationError('provider_store_binding_invalid')


def _revision(value):
    return type(value) is int and value >= 0


def _parent(value):
    if (not isinstance(value, dict) or set(value) != {'configuration', 'mode_revision'}
            or not _revision(value['mode_revision'])):
        _invalid()
    configuration = value['configuration']
    if (not isinstance(configuration, dict) or set(configuration) != set(CONFIGURATION_FIELDS)
            or configuration['purpose'] != 'generation'
            or any(type(configuration[key]) is not str for key in ('provider', 'base_url', 'model'))
            or any(type(configuration[key]) is not bool for key in ('allow_remote', 'configured', 'has_api_key'))
            or not _revision(configuration['revision'])):
        _invalid()
    return configuration


def _auxiliary(value):
    if (not isinstance(value, dict) or set(value) != {'model', 'selection_revision', 'parent'}
            or not _revision(value['selection_revision'])
            or value['model'] is not None and (type(value['model']) is not str
                or not value['model'].strip() or len(value['model']) > 200 or value['selection_revision'] < 1)):
        _invalid()
    _parent(value['parent'])


def capture_provider_store_selection(reader, models):
    """Use the caller's current reader; never acquire a configuration lock."""
    row = reader.read(_SETTINGS, 'default')
    revision = row.revision if row is not None else 0
    if not _revision(revision):
        _invalid()
    off = {'enabled': False, 'selection_revision': revision, 'parent': None, 'adapter': None}
    if row is not None:
        value = row.payload
        if not isinstance(value, dict) or set(value) != {'enabled', 'binding'} or type(value['enabled']) is not bool:
            _invalid()
        if value['enabled']:
            _parent(value['binding'])
        elif value['binding'] is not None:
            _invalid()
    capability = getattr(models, 'provider_store_capability', None)
    adapter_getter = getattr(models, 'provider_store_adapter', None)
    # Old synthetic model protocols have no capability declaration. They retain
    # their original path and acquire no background selection from a model name.
    if row is None or not row.payload['enabled'] or not callable(capability) or not callable(adapter_getter):
        return off
    current = capability(reader=reader)
    if not isinstance(current, dict) or set(current) != {'available', 'binding'} or type(current['available']) is not bool:
        _invalid()
    if not current['available'] or row.payload['binding'] != current['binding']:
        return off
    configuration = _parent(current['binding'])
    adapter = adapter_getter(reader=reader)
    if (not isinstance(adapter, ResponsesCompletion) or adapter.background_resume_capable is not True
            or adapter.api_base != configuration['base_url'].rstrip('/')
            or configuration['provider'] != 'openai' or configuration['configured'] is not True
            or configuration['has_api_key'] is not True):
        _invalid()
    return {'enabled': True, 'selection_revision': revision, 'parent': deepcopy(current['binding']),
        'adapter': {'kind': 'openai-responses', 'api_base': adapter.api_base, 'background_resume': True}}


def _live_auxiliary(reader, parent):
    row = reader.read('v2_generation_fast_model', 'default')
    value = row.payload if row is not None else {'model': None, 'binding': None}
    if (not isinstance(value, dict) or set(value) != {'model', 'binding'}
            or value['model'] is not None and (type(value['model']) is not str or not value['model'].strip())
            or not _revision(row.revision if row is not None else 0)):
        _invalid()
    return {'model': value['model'] if value['model'] and value['binding'] == parent else None,
        'selection_revision': row.revision if row is not None else 0, 'parent': parent}


def _identity(request):
    if not isinstance(request, dict) or not isinstance(request.get('scope'), dict):
        _invalid()
    value = {'turn_id': request.get('turn_id'), 'project_id': request.get('scope', {}).get('project_id'),
        'kind': request.get('desired_outcome')}
    if any(type(item) is not str or not item for item in value.values()):
        _invalid()
    return value


def stage_provider_store_binding(models, tx, request):
    """Called only after the winning first index/aux choice writes in this TX."""
    selection = capture_provider_store_selection(tx, models)
    payload = {'schema_version': '1.0.0', **_identity(request), **selection,
        'auxiliary': None, 'configuration': None}
    if selection['enabled']:
        row = tx.read(CHOICES, request['turn_id'])
        if row is None or row.revision != 1:
            _invalid()
        _auxiliary(row.payload)
        if row.payload != _live_auxiliary(tx, selection['parent']):
            raise ModelConfigurationError('provider_store_binding_changed')
        payload['auxiliary'] = deepcopy(row.payload)
        payload['configuration'] = {**selection['parent']['configuration']}
        if row.payload['model'] is not None:
            payload['configuration']['model'] = row.payload['model']
    return tx.put(BINDINGS, request['turn_id'], payload, expected_revision=0)


def validate_provider_store_binding(models, reader, request):
    """Validate selection only. A true result grants no permission or lease."""
    identity = _identity(request)
    row = reader.read(BINDINGS, identity['turn_id'])
    if row is None:
        return False  # Historical absence is never re-frozen.
    value = row.payload
    if (row.revision != 1 or not isinstance(value, dict) or set(value) != _FIELDS
            or value['schema_version'] != '1.0.0' or type(value['enabled']) is not bool
            or not _revision(value['selection_revision']) or any(value[key] != item for key, item in identity.items())):
        _invalid()
    if not value['enabled']:
        if any(value[key] is not None for key in _IDENTITIES):
            _invalid()
        return False
    parent_configuration = _parent(value['parent'])
    _auxiliary(value['auxiliary'])
    _parent({'configuration': value['configuration'], 'mode_revision': value['parent']['mode_revision']})
    expected_configuration = {**parent_configuration,
        'model': value['auxiliary']['model'] or parent_configuration['model']}
    adapter = value['adapter']
    if (value['auxiliary']['parent'] != value['parent'] or value['configuration'] != expected_configuration
            or not isinstance(adapter, dict) or set(adapter) != {'kind', 'api_base', 'background_resume'}
            or adapter != {'kind': 'openai-responses', 'api_base': parent_configuration['base_url'].rstrip('/'),
                          'background_resume': True} or type(adapter['background_resume']) is not bool):
        _invalid()
    current = capture_provider_store_selection(reader, models)
    choice = reader.read(CHOICES, identity['turn_id'])
    if (not current['enabled'] or current['selection_revision'] != value['selection_revision']
            or current['parent'] != value['parent'] or current['adapter'] != adapter
            or choice is None or choice.revision != 1 or choice.payload != value['auxiliary']
            or _live_auxiliary(reader, current['parent']) != value['auxiliary']):
        raise ModelConfigurationError('provider_store_binding_changed')
    return True


def stage_main_provider_store_binding(models, tx, request, route):
    """Capture only the immutable route creator's Main selection, not authority."""
    selection = capture_provider_store_selection(tx, models)
    payload = {'schema_version': '1.0.0', **_identity(request), **selection,
        'auxiliary': None, 'configuration': None}
    if selection['enabled']:
        configuration = _parent(selection['parent'])
        if route.get('configuration') != configuration:
            raise ModelConfigurationError('provider_store_binding_changed')
        payload['configuration'] = deepcopy(configuration)
    return tx.put(BINDINGS, request['turn_id'], payload, expected_revision=0)


def validate_main_provider_store_binding(models, reader, request, route):
    """Validate the Main choice without adopting a setting for historical Turns."""
    identity = _identity(request)
    row = reader.read(BINDINGS, identity['turn_id'])
    if row is None:
        return False
    value = row.payload
    if (row.revision != 1 or not isinstance(value, dict) or set(value) != _FIELDS
            or value['schema_version'] != '1.0.0' or type(value['enabled']) is not bool
            or not _revision(value['selection_revision']) or value['auxiliary'] is not None
            or any(value[key] != item for key, item in identity.items())):
        _invalid()
    if not value['enabled']:
        if any(value[key] is not None for key in _IDENTITIES):
            _invalid()
        return False
    configuration = _parent(value['parent'])
    adapter = value['adapter']
    if (value['configuration'] != configuration or route.get('configuration') != configuration
            or not isinstance(adapter, dict) or set(adapter) != {'kind', 'api_base', 'background_resume'}
            or adapter != {'kind': 'openai-responses', 'api_base': configuration['base_url'].rstrip('/'),
                          'background_resume': True} or type(adapter['background_resume']) is not bool):
        _invalid()
    current = capture_provider_store_selection(reader, models)
    if (not current['enabled'] or current['selection_revision'] != value['selection_revision']
            or current['parent'] != value['parent'] or current['adapter'] != adapter):
        raise ModelConfigurationError('provider_store_binding_changed')
    return True


def main_provider_store_activation(models, reader, request, route, *, provider_resume_source=None):
    """Use an already frozen Main choice; never capture or grant execution here."""
    if not validate_main_provider_store_binding(models, reader, request, route):
        return None
    frozen_request, frozen_route = deepcopy(request), deepcopy(route)
    adapter = models.provider_store_adapter(reader=reader)
    if (not isinstance(adapter, ResponsesCompletion) or not adapter.background_resume_capable
            or adapter.api_base != frozen_route['configuration']['base_url'].rstrip('/')):
        _invalid()
    def validate():
        if not validate_main_provider_store_binding(models, reader, frozen_request, frozen_route):
            raise ModelConfigurationError('provider_store_binding_changed')
    # 只有原显式继续主人投影的数据会传入；这里不获取或授予继续权限。
    return ProviderStoreActivation(adapter=adapter, validate_current=validate, resume_source=provider_resume_source)


def steward_provider_store_activation(models, reader, request, route):
    """Reuse the frozen primary choice after the caller's real steward checks."""
    if request.get('desired_outcome') != 'agent.steward.plan':
        _invalid()
    return main_provider_store_activation(models, reader, request, route)


def auxiliary_provider_store_activation(models, selected, reader, store, request, primary, route):
    """Combine existing ASK choices, keeping Main and selected model owners distinct."""
    if not validate_main_provider_store_binding(models, reader, request, primary):
        return None
    choice = store.get_immutable_payload(request['turn_id'], CHOICE_KIND)
    if choice is None:
        return None  # Missing historical choice is never adopted or captured.
    if request.get('desired_outcome') != 'project.answer':
        _invalid()
    _auxiliary(choice[1])
    frozen_request, frozen_primary, frozen_route = deepcopy(request), deepcopy(primary), deepcopy(route)
    frozen_choice = deepcopy(choice)
    frozen_binding = deepcopy(reader.read(BINDINGS, request['turn_id']).payload)

    def validate():
        if not validate_main_provider_store_binding(models, reader, frozen_request, frozen_primary):
            raise ModelConfigurationError('provider_store_binding_changed')
        turn_id = frozen_request['turn_id']
        saved_primary = store.get_immutable_payload(turn_id, SNAPSHOT_KIND)
        if (saved_primary is None or saved_primary[1] != frozen_primary
                or store.get_immutable_payload(turn_id, CHOICE_KIND) != frozen_choice):
            _invalid()
        parent = frozen_binding['parent']
        if frozen_choice[1]['parent'] != parent or _live_auxiliary(reader, parent) != frozen_choice[1]:
            raise ModelConfigurationError('provider_store_binding_changed')
        configuration = {**parent['configuration'],
            'model': frozen_choice[1]['model'] or parent['configuration']['model']}
        actual = selected.public()['generation']
        if ({key: actual.get(key) for key in CONFIGURATION_FIELDS} != configuration
                or not isinstance(frozen_route, dict)
                or set(frozen_route) != {'payload_ref', 'revision', 'prompt_cache_scope_identity',
                                         'configuration', 'execution_location'}
                or frozen_route['configuration'] != configuration):
            _invalid()
        if frozen_choice[1]['model'] is None:
            expected = RecognitionRoutingSnapshot(saved_primary[0], _routing_revision(saved_primary[1]),
                saved_primary[1]).generation_binding()
            if selected is not models or frozen_route != expected:
                _invalid()
        else:
            saved_route = store.get_immutable_payload(turn_id, ROUTE_KIND)
            if (saved_route is None or saved_route[0] != frozen_route['payload_ref']
                    or saved_route[1] != {'turn_id': turn_id,
                        'project_id': frozen_request['scope']['project_id'], 'configuration': configuration,
                        'primary_route_ref': saved_primary[0], 'choice_ref': frozen_choice[0],
                        'execution_location': frozen_route['execution_location'], 'purpose': 'aux'}):
                _invalid()
            expected = _routing_revision(saved_route[1])
            if frozen_route['revision'] != expected or frozen_route['prompt_cache_scope_identity'] != expected:
                _invalid()

    validate()
    adapter = models.provider_store_adapter(reader=reader)
    if (not isinstance(adapter, ResponsesCompletion) or not adapter.background_resume_capable
            or adapter.api_base != frozen_route['configuration']['base_url'].rstrip('/')):
        _invalid()
    return ProviderStoreActivation(adapter=adapter, validate_current=validate, expected_purpose='aux')


def route_provider_store_activation(models, selected, reader, store, request, route):
    """Use the Route's existing standalone choice and exact immutable route."""
    if request.get('desired_outcome') != 'workbench.route':
        _invalid()
    if not validate_provider_store_binding(models, reader, request):
        return None
    choice = store.get_immutable_payload(request['turn_id'], CHOICE_KIND)
    if choice is None:
        _invalid()  # An enabled first-TX binding must have its staged choice.
    _auxiliary(choice[1])
    frozen_request, frozen_route, frozen_choice = deepcopy(request), deepcopy(route), deepcopy(choice)
    frozen_binding = deepcopy(reader.read(BINDINGS, request['turn_id']).payload)

    def validate():
        if not validate_provider_store_binding(models, reader, frozen_request):
            raise ModelConfigurationError('provider_store_binding_changed')
        turn_id = frozen_request['turn_id']
        if (store.get_immutable_payload(turn_id, CHOICE_KIND) != frozen_choice
                or frozen_choice[1] != frozen_binding['auxiliary']):
            _invalid()
        configuration = frozen_binding['configuration']
        actual = selected.public()['generation']
        if ({key: actual.get(key) for key in CONFIGURATION_FIELDS} != configuration
                or frozen_choice[1]['model'] is None and selected is not models
                or not isinstance(frozen_route, dict)
                or set(frozen_route) != {'payload_ref', 'revision', 'prompt_cache_scope_identity',
                                         'configuration', 'execution_location'}
                or frozen_route['configuration'] != configuration):
            _invalid()
        saved = store.get_immutable_payload(turn_id, 'workbench-route-model-v1')
        if (saved is None or saved[0] != frozen_route['payload_ref']
                or saved[1] != {'configuration': configuration,
                    'project_id': frozen_request['scope']['project_id'], 'turn_id': turn_id,
                    'purpose': 'aux', 'execution_location': frozen_route['execution_location']}):
            _invalid()
        expected = _routing_revision(saved[1])
        if (frozen_route['revision'] != expected or frozen_route['prompt_cache_scope_identity'] != expected
                or frozen_route['execution_location'] != _generation_execution_location(configuration['base_url'])):
            _invalid()

    validate()
    adapter = models.provider_store_adapter(reader=reader)
    if (not isinstance(adapter, ResponsesCompletion) or not adapter.background_resume_capable
            or adapter.api_base != frozen_route['configuration']['base_url'].rstrip('/')):
        _invalid()
    return ProviderStoreActivation(adapter=adapter, validate_current=validate, expected_purpose='aux')


def memory_provider_store_call(models, selected, reader, store, request, route_ref, route):
    """Project the original five-field memory route into a governed ON call."""
    if not isinstance(request.get('desired_outcome'), str) or not request['desired_outcome'].startswith('memory.'):
        _invalid()
    if not validate_provider_store_binding(models, reader, request):
        return None
    frozen_request, frozen_route = deepcopy(request), deepcopy(route)
    frozen_binding = deepcopy(reader.read(BINDINGS, request['turn_id']).payload)
    frozen_choice = store.get_immutable_payload(request['turn_id'], CHOICE_KIND)
    configuration = frozen_binding['configuration']
    fields = ('provider', 'model', 'base_url', 'revision', 'allow_remote')

    def validate():
        if not validate_provider_store_binding(models, reader, frozen_request):
            raise ModelConfigurationError('provider_store_binding_changed')
        turn_id = frozen_request['turn_id']
        expected = {field: configuration[field] for field in fields}
        actual = selected.public()['generation']
        if (frozen_choice is None or frozen_choice[1] != frozen_binding['auxiliary']
                or store.get_immutable_payload(turn_id, CHOICE_KIND) != frozen_choice
                or store.get_immutable_payload(turn_id, 'memory-model-route-v1') != (route_ref, frozen_route)
                or not isinstance(frozen_route, dict) or frozen_route != expected
                or any(type(frozen_route[key]) is not type(expected[key]) for key in fields)
                or any(type(actual.get(key)) is not type(value) or actual.get(key) != value
                       for key, value in configuration.items())
                or frozen_choice[1]['model'] is None and selected is not models):
            _invalid()

    validate()
    adapter = models.provider_store_adapter(reader=reader)
    if (not isinstance(adapter, ResponsesCompletion) or not adapter.background_resume_capable
            or adapter.api_base != configuration['base_url'].rstrip('/')):
        _invalid()
    governed = {'payload_ref': route_ref, 'revision': _routing_revision(frozen_route),
        'prompt_cache_scope_identity': _routing_revision({'turn_id': request['turn_id'], 'route': frozen_route}),
        'configuration': deepcopy(configuration),
        'execution_location': _generation_execution_location(configuration['base_url'])}
    return governed, ProviderStoreActivation(adapter=adapter, validate_current=validate, expected_purpose='aux')
