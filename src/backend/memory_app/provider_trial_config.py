from collections.abc import Mapping
from backend.shared.server_resources import RESOURCE_POOL

from .model_config import ModelConfiguration, ModelConfigurationError, _validate_generation_egress
from .model_binding import public_model_configuration


class ProviderTrialConfiguration(ModelConfiguration):
    def __init__(self, owner, *, target, registry, draft=None):
        if not isinstance(owner, ModelConfiguration) or registry._connection_owner is not owner:
            raise ModelConfigurationError('provider_trial_owner_invalid')
        object.__setattr__(self, 'owner', owner)
        object.__setattr__(self, 'registry', registry)
        object.__setattr__(self, 'parent', owner._generation_authority_binding())
        object.__setattr__(self, 'mode_revision', owner.generation_mode()['revision'])
        object.__setattr__(self, 'target', target)
        auxiliary = owner.freeze_auxiliary_binding() if target == 'fast' else None
        if target == 'fast' and auxiliary['model'] is None:
            raise ModelConfigurationError('provider_trial_fast_unconfigured')
        object.__setattr__(self, 'auxiliary', auxiliary)
        object.__setattr__(self, 'selected', owner.for_auxiliary(auxiliary) if auxiliary else owner)
        object.__setattr__(self, 'draft', None)
        object.__setattr__(self, '_draft_key', None)
        object.__setattr__(self, 'connection', None)
        object.__setattr__(self, '_disposed', False)
        if target not in {'main', 'fast', 'draft'} or (target == 'draft') != (draft is not None):
            raise ModelConfigurationError('provider_trial_invalid')
        if draft is not None:
            if (not isinstance(draft, Mapping) or set(draft) != {'base_url', 'model', 'api_key', 'connection_id', 'expected_connection_revision'}
                    or not isinstance(draft['api_key'], str) or len(draft['api_key']) > 8192
                    or not isinstance(draft['model'], str) or not 1 <= len(draft['model'].strip()) <= 200):
                raise ModelConfigurationError('provider_trial_invalid')
            owner._require_independent('generation')
            if owner.generation_mode()['mode'] != 'api':
                raise ModelConfigurationError('select_api_before_provider_trial')
            normalized = registry.validate_connection({'name': '试用', 'base_url': draft['base_url'], 'models': [draft['model']]})
            public = owner.public_for_purpose('generation')
            configuration = {**public, 'provider': 'openai', 'base_url': normalized['base_url'],
                'model': normalized['models'][0], 'configured': True, 'has_api_key': True}
            object.__setattr__(self, 'draft', configuration)
            if draft['connection_id'] is not None:
                with owner.records.begin_read() as reader:
                    row, generation = registry.connection_snapshot(reader, draft['connection_id'], draft['expected_connection_revision'])
                object.__setattr__(self, 'connection', (row.object_id, row.revision, generation, row.payload['secret_ref']))
                if not draft['api_key'].strip() and normalized['base_url'] != row.payload['base_url']:
                    raise ModelConfigurationError('provider_connection_new_key_required')
            if draft['api_key'].strip():
                object.__setattr__(self, '_draft_key', draft['api_key'])
            elif self.connection is None:
                raise ModelConfigurationError('provider_connection_key_required')
        self._validate_trial()

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def __getattribute__(self, name):
        if name.startswith(('update', 'select_', 'bind_')) or name in {'close', 'release_connection_secret'}:
            raise ModelConfigurationError('provider_trial_readonly')
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        raise ModelConfigurationError('provider_trial_readonly')

    def dispose(self):
        object.__setattr__(self, '_disposed', True)
        object.__setattr__(self, '_draft_key', None)
        object.__setattr__(self, 'draft', None)

    def _validate_trial(self):
        if self._disposed:
            raise ModelConfigurationError('provider_trial_closed')
        if (self.owner._generation_authority_binding() != self.parent
                or self.owner.generation_mode()['revision'] != self.mode_revision):
            raise ModelConfigurationError('provider_trial_configuration_changed')
        public = self.owner.public_for_purpose('generation')
        if public.get('enabled') is not True:
            raise ModelConfigurationError('provider_trial_generation_disabled')
        if self.owner.server_model_access is not None:
            resources = RESOURCE_POOL.get()
            live_owner = getattr(resources, 'server_model_wire_owner_for', None)
            if not callable(live_owner):
                raise ModelConfigurationError('provider_trial_owner_invalid')
            try:
                records, models = live_owner(self.owner.root)
            except ValueError:
                raise ModelConfigurationError('provider_trial_identity_changed') from None
            if records is not self.owner.records or models is not self.owner:
                raise ModelConfigurationError('provider_trial_owner_invalid')
            binding = self.parent['configuration'].get('server_model_binding')
            if binding is None:
                raise ModelConfigurationError('provider_trial_configuration_changed')
            self.owner.server_model_access.validate_binding(binding)
        if self.auxiliary is not None and self.owner.freeze_auxiliary_binding() != self.auxiliary:
            raise ModelConfigurationError('provider_trial_configuration_changed')
        if self.draft is not None:
            self.owner._require_independent('generation')
        self.selected.public_for_purpose('generation')
        if self.connection is not None:
            identity, revision, generation, ref = self.connection
            with self.records.begin_read() as reader:
                row, current = self.registry.connection_snapshot(reader, identity, revision)
                if current != generation or row.payload['secret_ref'] != ref:
                    raise ModelConfigurationError('provider_connection_changed')

    def _public_one(self, purpose):
        if purpose != 'generation':
            return self.owner.public_for_purpose(purpose)
        self._validate_trial()
        return dict(self.draft) if self.draft is not None else self.selected.public_for_purpose(purpose)

    def public(self):
        return {**self.owner.public(), 'generation': self._public_one('generation')}

    def snapshot(self, purpose):
        if purpose != 'generation':
            raise ModelConfigurationError('provider_trial_purpose_invalid')
        self._validate_trial()
        if self.draft is None:
            return self.selected.snapshot(purpose)
        public = self._public_one(purpose)
        _validate_generation_egress(public)
        key = self._draft_key
        if key is None and self.connection is not None:
            saved = self.secrets.get_snapshot(self.connection[3])
            if saved.generation != self.connection[2] or not saved.value:
                raise ModelConfigurationError('provider_connection_changed')
            key = saved.value
        if not key:
            raise ModelConfigurationError('provider_connection_key_required')
        return {**public, 'api_key': key}

    def freeze_auxiliary_binding(self):
        return {'model': None, 'selection_revision': 0, 'parent': self._generation_authority_binding()}

    def for_auxiliary(self, binding):
        if binding != self.freeze_auxiliary_binding():
            raise ModelConfigurationError('provider_trial_configuration_changed')
        return self

    def _provider_store_selection(self, reader):
        return {'configuration': public_model_configuration(self._public_one('generation')),
            'mode_revision': self.mode_revision}, None
