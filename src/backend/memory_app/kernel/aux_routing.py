"""Freeze an auxiliary choice alongside, without changing, the primary route."""
from urllib.parse import urlsplit

from ..turn_routing import CONFIGURATION_FIELDS, _revision
from ..model_config import ModelConfigurationError

CHOICE_KIND = 'product-aux-configuration-v1'
ROUTE_KIND = 'product-aux-model-routing-v1'
CHOICES = 'v2_aux_model_bindings'


def freeze_auxiliary_choice(models, store, turn_id):
    freeze = getattr(models, 'freeze_auxiliary_binding', None)
    if callable(freeze):
        store.get_or_create_immutable_payload(turn_id, CHOICE_KIND, freeze())


def stage_auxiliary_choice(models, tx, turn_id):
    freeze = getattr(models, 'freeze_auxiliary_binding', None)
    if callable(freeze):
        tx.put(CHOICES, turn_id, freeze(), expected_revision=0)


def auxiliary_models(models, store, turn_id, *, records=None):
    saved = store.get_immutable_payload(turn_id, CHOICE_KIND)
    if saved is None and records is not None:
        staged = records.read(CHOICES, turn_id)
        if staged is not None:
            if staged.revision != 1:
                raise ModelConfigurationError('fast_model_binding_changed')
            ref = store.get_or_create_immutable_payload(turn_id, CHOICE_KIND, staged.payload)
            saved = (ref, staged.payload)
    # Historical Turns retain their original route, including when a fast
    # model has since been configured. Missing history is never re-frozen.
    return models.for_auxiliary(saved[1]) if saved is not None else models


def auxiliary_route(models, store, turn_id, project_id, primary):
    selected = auxiliary_models(models, store, turn_id)
    if selected is models:
        return models, primary
    public = selected.public()['generation']
    configuration = {key: public.get(key) for key in CONFIGURATION_FIELDS}
    if public.get('subscription_binding'):
        configuration['subscription_binding'] = dict(public['subscription_binding'])
    local = urlsplit(str(configuration.get('base_url') or '')).hostname in {'localhost', '127.0.0.1', '::1'}
    payload = {'turn_id': turn_id, 'project_id': project_id, 'configuration': configuration,
        'primary_route_ref': primary['payload_ref'],
        'choice_ref': store.get_immutable_payload(turn_id, CHOICE_KIND)[0],
        'execution_location': 'local_loopback' if local else 'remote', 'purpose': 'aux'}
    ref = store.get_or_create_immutable_payload(turn_id, ROUTE_KIND, payload)
    return selected, {'payload_ref': ref, 'revision': _revision(payload),
        'prompt_cache_scope_identity': _revision(payload), 'configuration': configuration,
        'execution_location': payload['execution_location']}
