"""Synthetic model transport that exercises real nested kernel lifecycles."""

class GovernedModel:
    def public(self):
        return {"generation": {"provider": "openai", "purpose": "generation", "model": "fake",
            "base_url": "http://127.0.0.1:8001/local-model/v1", "configured": True, "has_api_key": False,
            "revision": 1, "allow_remote": False}, "generation_mode": {"revision": 1}}

    def complete_governed(self, messages, *, routing_snapshot, execution_control, metadata_sink,
                          wire_attempt_sink, response_model=None, purpose="primary", on_delta=None, on_retry=None, **kwargs):
        metadata_sink.model_call_routed(snapshot_ref=routing_snapshot["payload_ref"],
            snapshot_revision=routing_snapshot["revision"], prompt_cache_scope_identity=routing_snapshot["prompt_cache_scope_identity"],
            provider="openai", model="fake", execution_location=routing_snapshot["execution_location"], purpose=purpose)
        metadata_sink.model_call_started(provider="openai", model="fake")
        from backend.memory_app.structured_generation import generate_structured
        import inspect
        owns_wire = "wire_attempt_sink" in inspect.signature(self.complete).parameters
        wire = None if owns_wire else wire_attempt_sink.begin_model_wire_attempt()
        def calculate(**options):
            kwargs["validate_current"]()
            if response_model is None:
                return self.complete(messages, **kwargs, **options)
            return generate_structured(self, messages, response_model=response_model,
                on_delta=on_delta, **kwargs, **options)
        def invoke():
            try:
                output, meta = calculate()
                wire.succeeded(usage=meta.get("usage", {}), cache_observation=None)
                return output, meta
            except BaseException:
                wire.failed_transport(error_code="synthetic_failure")
                raise
        try:
            if owns_wire:
                output, meta = calculate(wire_attempt_sink=wire_attempt_sink)
            else:
                output, meta = wire.invoke_wire(invoke)
            metadata_sink.model_call_completed(usage=meta.get("usage", {}))
            return output, meta
        except BaseException:
            metadata_sink.model_call_failed()
            raise

