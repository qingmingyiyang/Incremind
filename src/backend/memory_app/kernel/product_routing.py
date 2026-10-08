"""Freeze the configured product model without copying its secret."""
from urllib.parse import urlsplit

from ..model_config import ModelConfigurationError
from ..turn_routing import CONFIGURATION_FIELDS, SNAPSHOT_KIND, RecognitionRoutingSnapshot, _revision


class ProductGenerationRouting:
    def __init__(self, models, payloads, *, records=None, answer_owner=False, agent_binding_verifier=None,
                 steward_parent_check=None):
        self.models, self.payloads = models, payloads
        self.records, self.answer_owner = records, answer_owner
        self.agent_binding_verifier = agent_binding_verifier
        self.steward_parent_check = steward_parent_check

    def _main_owner(self, request, capability_ids):
        if request.get('desired_outcome') == 'project.answer':
            exact = request.get('capability_request', {})
            return (self.answer_owner is True and request.get('execution_policy', {}).get('template_version') == 2
                and exact.get('mode') == 'execute_exact_v1'
                and exact.get('capability_id') == 'workbench.answer.execute'
                and tuple(capability_ids) == ('workbench.answer.execute',)
                and 'agent_binding' not in request)
        binding = request.get('agent_binding')
        if (request.get('desired_outcome') != 'project.task' or not isinstance(binding, dict)
                or not callable(self.agent_binding_verifier)):
            return False
        return self.agent_binding_verifier(request, binding).get('role') == 'main'

    def _provider_store_owner(self, request, capability_ids):
        if self._main_owner(request, capability_ids):
            return True
        binding = request.get('agent_binding')
        if (request.get('desired_outcome') != 'agent.steward.plan' or not isinstance(binding, dict)
                or not callable(self.agent_binding_verifier) or not callable(self.steward_parent_check)
                or self.steward_parent_check(request) is not True):
            return False
        verified = self.agent_binding_verifier(request, binding)
        return (isinstance(verified, dict) and verified == binding and verified.get('role') == 'subagent'
            and verified.get('profile_id') == 'steward.scheduler'
            and type(verified.get('depth')) is int and verified['depth'] >= 1
            and isinstance(verified.get('parent_run_id'), str) and bool(verified['parent_run_id']))

    def acquire(self, *, request, project_id, project_profile_id, project_profile_revision,
                boundary_profile_id, boundary_profile_revision, capability_ids):
        turn_id = request["turn_id"]
        public = self.models.public()["generation"]
        configuration = {key: public.get(key) for key in CONFIGURATION_FIELDS}
        if public.get("subscription_binding"):
            configuration["subscription_binding"] = dict(public["subscription_binding"])
        address = str(configuration["base_url"] or "")
        url = urlsplit(address)
        local = not address or url.hostname in {"localhost", "127.0.0.1", "::1"}
        if address and (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.scheme not in ({"http", "https"} if local else {"https"})):
            raise ModelConfigurationError("model_routing_snapshot_invalid")
        # Freezing a public choice does not authorize its use. A read-only
        # Turn may return no matches even when no model is configured/enabled.
        # Source/config guards still run before every actual wire attempt.
        identity = dict(turn_id=turn_id, project_id=project_id, context_packet_id="product-" + turn_id,
            project_profile_id=project_profile_id, project_profile_revision=project_profile_revision,
            boundary_profile_id=boundary_profile_id, boundary_profile_revision=boundary_profile_revision,
            capability_ids=list(capability_ids), configuration=configuration,
            execution_location="local_loopback" if local else "remote", agent=None)
        payload = {"schema_version": "1.0.0", "kind": SNAPSHOT_KIND, **identity,
            "catalog_revision": _revision({"configuration": configuration}),
            "prompt_cache_scope_identity": _revision(identity)}
        if self.records is None:
            first_route = self.payloads.get_immutable_payload(turn_id, SNAPSHOT_KIND) is None
            ref = self.payloads.get_or_create_immutable_payload(turn_id, SNAPSHOT_KIND, payload)
        else:
            from .provider_store_binding import stage_main_provider_store_binding, validate_main_provider_store_binding
            primary_owner = self._provider_store_owner(request, capability_ids)
            with self.records.begin() as tx:
                ref, first_route = self.payloads.reserve_immutable_payload(turn_id, SNAPSHOT_KIND, payload)
                saved = self.payloads.get_immutable_payload(turn_id, SNAPSHOT_KIND)
                if saved is None or saved[0] != ref or saved[1] != payload:
                    raise ModelConfigurationError('model_routing_snapshot_invalid')
                payload = saved[1]
                if primary_owner:
                    if first_route:
                        stage_main_provider_store_binding(self.models, tx, request, payload)
                    else:
                        validate_main_provider_store_binding(self.models, tx, request, payload)
                tx.commit()
        if first_route and request.get('desired_outcome') == 'project.answer':
            from .aux_routing import freeze_auxiliary_choice
            # Routing acquisition runs during first admission, before any
            # auxiliary call can observe a later settings choice. Keep this
            # ModelConfiguration lock outside the records transaction.
            freeze_auxiliary_choice(self.models, self.payloads, turn_id)
        return RecognitionRoutingSnapshot(ref, _revision(payload), payload)
