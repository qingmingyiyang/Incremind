"""Synthetic model transport that exercises real nested kernel lifecycles."""

import json


def evidence_first_payload(messages, *, answer="Synthetic answer", numbers=None):
    """从本次真实证据目录生成首稿，末稿原样采用首稿的证据。"""
    for message in reversed(messages):
        content = message.get("content", "")
        if not content.startswith("资料：\n{"):
            continue
        data, _ = json.JSONDecoder().raw_decode(content[len("资料：\n"):])
        if "evidence_catalog" not in data:
            continue
        if "untrusted_first_fact_plan" in data:
            return {"statements": [{**fact, "first_fact_uses": [
                {"fact_index": index, "action": "adopt", "reason": "synthetic review"}]
                } for index, fact in enumerate(data["untrusted_first_fact_plan"]["facts"], 1)],
                "unused": {"scope": "all_unreferenced", "reason": "synthetic review"},
                "uncertain": [], "excluded_facts": []}
        catalog = data["evidence_catalog"]
        selected = numbers if numbers is not None else [source["source_number"] for source in catalog["sources"]]
        evidence = []
        for number in selected:
            unit = next((unit for unit in catalog["units"] if unit["source_number"] == number), None)
            if unit is None:
                raise ValueError(f"synthetic evidence source missing: {number}")
            evidence.append({"unit_id": unit["id"], "quote": unit["text"], "role": "support"})
        return {"facts": [{"kind": "fact", "text": answer, "evidence": evidence, "date_refs": []}],
                "unreviewed": {"scope": "all_unreferenced", "reason": "synthetic first draft"}}
    return None


class GovernedModel:
    @property
    def secrets(self):
        from backend.security.secrets import InMemorySecretStore
        if not hasattr(self, "_fixture_secrets"):
            self._fixture_secrets = InMemorySecretStore()
        return self._fixture_secrets

    @secrets.setter
    def secrets(self, value):
        self._fixture_secrets = value

    def generation_budget_limits(self, *, expected_revision, max_tokens=None):
        if self.public()["generation"]["revision"] != expected_revision:
            return None
        return {"window": 32000, "reserve": 4000}

    def public(self):
        return {"generation": {"provider": "openai", "purpose": "generation", "model": "fake",
            "base_url": "http://127.0.0.1:8001/local-model/v1", "configured": True, "has_api_key": False,
            "revision": 1, "allow_remote": False}, "generation_mode": {"revision": 1}}

    def complete_governed(self, messages, *, routing_snapshot, execution_control, metadata_sink,
                          wire_attempt_sink, response_model=None, purpose="primary", on_delta=None, on_retry=None,
                          retry_policy=None, **kwargs):
        # 与实际网关一样在传输层消费重试选项，不转给只负责生成正文的样本模型。
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

