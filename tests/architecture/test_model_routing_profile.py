from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from core.product_core.model_route_registry import _ADAPTER_KINDS
from core.product_core.model_route_runtime import SUPPORTED_ROUTES


ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "src/core/ai_tooling/model_routing.py"


def test_model_routing_profile_schema_is_strict_and_allows_a_dedicated_image_route() -> None:
    schema = json.loads(
        (ROOT / "core-contracts/ai/model-routing-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tier_routes"]["additionalProperties"] is False
    assert schema["properties"]["authority_binding"]["oneOf"][1]["additionalProperties"] is False
    assert schema["properties"]["tier_routes"]["properties"]["image_generation"] == {
        "$ref": "#/$defs/route",
    }


def test_turn_snapshot_schema_keeps_execution_location_and_image_contract_in_parity() -> None:
    schema = json.loads(
        (ROOT / "core-contracts/ai/turn-model-routing-snapshot.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    requirement = schema["$defs"]["requirement"]["properties"]
    assert "image_generation" in requirement["required_capability"]["enum"]
    assert "image_generation" in requirement["modality"]["enum"]
    assert "image_asset" in requirement["output_contract"]["enum"]
    assert "execution_location" in schema["$defs"]["tier"]["required"]
    assert "execution_location" in schema["$defs"]["selected"]["required"]


def test_model_router_is_provider_backend_and_model_name_neutral() -> None:
    text = DOMAIN.read_text(encoding="utf-8")
    for forbidden in (
        "backend.", "ProviderRegistry", "LiteLLM", "base_url", "api_key",
        "model_name", "openai", "deepseek",
    ):
        assert forbidden not in text


def test_dedicated_image_generation_route_is_activatable_without_reusing_text_adapter() -> None:
    assert {"tier.fast", "tier.standard", "tier.deep", "tier.vision", "tier.image_generation"} <= SUPPORTED_ROUTES
    assert "openai-compatible-image-generation" in _ADAPTER_KINDS
