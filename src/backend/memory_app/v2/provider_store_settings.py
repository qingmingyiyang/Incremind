"""Independent selection of optional provider storage, never dispatch authority."""
from ..model_config import ModelConfigurationError


COLLECTION = 'v2_provider_store_settings'


class ProviderStoreSettings:
    def __init__(self, records, models):
        self.records = records
        self.models = models

    def get(self):
        with self.records.begin() as tx:
            return self._view(tx, self.models.provider_store_capability(reader=tx))

    def capture(self, reader):
        from ..kernel.provider_store_binding import capture_provider_store_selection
        return capture_provider_store_selection(reader, self.models)

    def _view(self, reader, capability):
        row = reader.read(COLLECTION, 'default')
        binding = capability['binding']
        return {'available': capability['available'], 'enabled': bool(capability['available'] and row
            and row.payload.get('enabled') is True and row.payload.get('binding') == binding),
            'revision': row.revision if row else 0,
            'generation_revision': binding['configuration']['revision'],
            'mode_revision': binding['mode_revision']}

    def update(self, *, enabled, expected_revision, expected_generation_revision, expected_mode_revision):
        if (type(enabled) is not bool or any(type(value) is not int or value < 0 for value in
                (expected_revision, expected_generation_revision, expected_mode_revision))):
            raise ModelConfigurationError('provider_store_invalid')
        initial = self.models.provider_store_capability()
        with self.records.begin() as tx:
            capability = self.models.provider_store_capability(reader=tx)
            binding = capability['binding']
            if (binding['configuration']['revision'] != expected_generation_revision
                    or binding['mode_revision'] != expected_mode_revision or capability != initial):
                raise ModelConfigurationError('provider_store_configuration_changed')
            if enabled and not capability['available']:
                raise ModelConfigurationError('provider_store_unavailable')
            tx.put(COLLECTION, 'default', {'enabled': enabled, 'binding': binding if enabled else None},
                expected_revision=expected_revision)
            result = self._view(tx, capability)
            tx.commit()
        return result
