from .model_config import ModelConfigurationError, PURPOSES


def select_provider_connection(models, registry, identity, *, purpose, model, expected_revision,
        expected_configuration_revision, expected_mode_revision, operation_id):
    if (purpose not in PURPOSES or not isinstance(model, str) or not model
            or any(type(value) is not int or value < 0 for value in
                (expected_revision, expected_configuration_revision, expected_mode_revision))):
        raise ModelConfigurationError('provider_connection_selection_invalid')
    models._require_independent(purpose)
    initial_selection = models._server_selection(purpose)
    if initial_selection is not None and initial_selection['mode'] != 'independent':
        raise ModelConfigurationError('server_model_configuration_readonly')
    binding = initial_selection['server_model_binding'] if initial_selection is not None else None
    with models._lock, models.records.begin() as tx:
        selected = models._server_selection(purpose)
        if selected is not None:
            if selected['mode'] != 'independent':
                raise ModelConfigurationError('server_model_configuration_readonly')
            if selected['server_model_binding'] != binding:
                raise ModelConfigurationError('provider_connection_allocation_changed')
            models.server_model_access.validate_binding(binding)
        elif binding is not None:
            raise ModelConfigurationError('provider_connection_allocation_changed')
        choice = tx.read('recognition_generation_mode', 'default')
        if (choice.revision if choice else 0) != expected_mode_revision:
            raise ModelConfigurationError('provider_connection_mode_changed')
        if purpose == 'generation' and models.generation_mode()['mode'] != 'api':
            raise ModelConfigurationError('select_api_before_provider_connection')
        row, generation = registry.connection_snapshot(tx, identity, expected_revision)
        if model not in row.payload['models']:
            raise ModelConfigurationError('provider_connection_model_unavailable')
        current = tx.read('recognition_model_config', purpose)
        payload = dict(current.payload) if current else {}
        if (current.revision if current else 0) != expected_configuration_revision:
            raise ModelConfigurationError('provider_connection_configuration_changed')
        previous_ref = payload.get('secret_ref', '')
        payload.update(provider='openai', base_url=row.payload['base_url'], model=model,
            secret_ref=row.payload['secret_ref'])
        payload.setdefault('enabled', False)
        payload.setdefault('allow_remote', False)
        saved = tx.put('recognition_model_config', purpose, payload, expected_revision=expected_configuration_revision)
        use = tx.read('v2_provider_connection_uses', purpose)
        tx.put('v2_provider_connection_uses', purpose, {'connection_id': identity,
            'connection_revision': row.revision, 'secret_generation': generation,
            **{key: payload[key] for key in ('provider', 'base_url', 'model', 'secret_ref')}},
            expected_revision=use.revision if use else 0)
        if purpose == 'generation' and choice is not None:
            tx.put('recognition_generation_mode', 'default', {**choice.payload, 'api_config': payload},
                expected_revision=choice.revision)
        registry.stage_connection_selection(tx, operation_id, identity=identity, purpose=purpose,
            configuration_revision=saved.revision, connection_revision=row.revision)
        if selected is not None:
            models.server_model_access.validate_binding(binding)
        tx.commit()
    registry.release_connection_secret(previous_ref)
    return models.public_for_purpose(purpose)
