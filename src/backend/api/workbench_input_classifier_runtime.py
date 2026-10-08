from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.model_route_registry import ModelRouteRegistry
from core.product_core.processing_recipe import (
    ProcessingRecipeError,
    ProcessingRecipeRegistry,
    ProcessingRecipeRuntime,
)
from core.product_core.prompt_activation import resolve_active_prompt, serialize_prompt_activation
from core.product_core.workbench_input_classifier import (
    ClassifyWorkbenchInput,
)
from core.product_core.workbench_input_classifier_endpoint import (
    ServeWorkbenchInputClassifierEndpoint,
    WorkbenchInputClassifierEndpointResponse,
)


INPUT_CLASSIFIER_PROMPT_ID = "pt-input-understanding"
INPUT_CLASSIFIER_MODEL_ROUTE = "intake.classification"


class WorkbenchInputClassifierContainerPort(Protocol):
    """Minimal backend composition contract; deliberately not a FastAPI contract."""

    root_dir: Path
    settings_service: object
    secret_store: object


@dataclass(frozen=True, slots=True)
class WorkbenchInputClassifierRuntime:
    """Compose the classifier's deterministic, Prompt, Recipe and Provider boundaries."""

    runtime_root: Path
    object_store: object
    container: WorkbenchInputClassifierContainerPort

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
    ) -> WorkbenchInputClassifierEndpointResponse:
        recipe_trace = self.recipe_preflight(body)
        classifier_prompt = self.active_prompt()
        response = ServeWorkbenchInputClassifierEndpoint().execute(
            method=method,
            path=path,
            body=body,
            classify_input=ClassifyWorkbenchInput().execute,
            enhance_input=self.enhance,
            classifier_prompt=classifier_prompt,
        )
        return _with_recipe_trace(response, recipe_trace)

    def recipe_preflight(
        self,
        body: Mapping[str, object] | None,
        *,
        trigger: str = "workbench.input-classifier",
    ) -> dict[str, object] | None:
        if not isinstance(body, Mapping):
            return None
        content = body.get("content", "")
        if not isinstance(content, str):
            return None
        return ProcessingRecipeRuntime(self._recipe_registry()).preflight(
            content_type=_processing_recipe_content_type(body),
            text=content,
            trigger=trigger,
        )

    def _recipe_registry(self) -> ProcessingRecipeRegistry:
        def validate_authorities(recipe: Mapping[str, object]) -> None:
            prompt_ref = recipe.get("prompt_ref")
            if not isinstance(prompt_ref, Mapping):
                raise ProcessingRecipeError("processing recipe prompt authority is missing")
            try:
                config = GetDeveloperStudioConfig(self.object_store).execute()  # type: ignore[arg-type]
                activation = serialize_prompt_activation(config)
            except ValueError as error:
                raise ProcessingRecipeError("processing recipe prompt authority is unreadable") from error
            unit = next(
                (item for item in activation["units"] if item["unit_id"] == prompt_ref.get("unit_id")),
                None,
            )
            if unit is None or unit["unit_revision"] != prompt_ref.get("unit_revision"):
                raise ProcessingRecipeError("processing recipe prompt authority revision drifted")
            if prompt_ref.get("prompt_id") not in unit["active_prompt_ids"]:
                raise ProcessingRecipeError("processing recipe active prompt is missing")
            try:
                route = ModelRouteRegistry(self.runtime_root).get(
                    str(recipe.get("model_route_key") or "")
                )["route"]
            except ValueError as error:
                raise ProcessingRecipeError("processing recipe model route authority is missing") from error
            if route.get("enabled") is not True:
                raise ProcessingRecipeError("processing recipe model route authority is disabled")
            if route.get("revision") != recipe.get("model_route_revision"):
                raise ProcessingRecipeError("processing recipe model route revision drifted")

        return ProcessingRecipeRegistry(self.object_store, authority_validator=validate_authorities)  # type: ignore[arg-type]

    def active_prompt(self) -> Mapping[str, object] | None:
        try:
            config = GetDeveloperStudioConfig(self.object_store).execute()  # type: ignore[arg-type]
            prompt = resolve_active_prompt(config, INPUT_CLASSIFIER_PROMPT_ID)
        except Exception:
            return None
        if prompt is None:
            return None
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        prompt_revision = prompt.get("version")
        prompt_ref: dict[str, object] = {
            "id": INPUT_CLASSIFIER_PROMPT_ID,
            "revision": prompt_revision if isinstance(prompt_revision, int) else config.revision,
            "source": "developer_studio_active",
            "content": content,
        }
        stage_id = prompt.get("stageId")
        if isinstance(stage_id, str) and stage_id.strip():
            prompt_ref["stage_id"] = stage_id.strip()
        model_profile_id = prompt.get("modelProfileId")
        if isinstance(model_profile_id, str) and model_profile_id.strip():
            prompt_ref["model_profile_id"] = model_profile_id.strip()
        activation = serialize_prompt_activation(config)
        for unit in activation["units"]:
            if INPUT_CLASSIFIER_PROMPT_ID in unit["prompt_ids"]:
                prompt_ref["activation_revision"] = activation["activation_revision"]
                prompt_ref["activation_unit"] = unit["unit_id"]
                prompt_ref["activation_unit_revision"] = unit["unit_revision"]
                break
        return prompt_ref

    def enhance(self, **kwargs: object):
        del kwargs
        raise RuntimeError(
            "Legacy classifier provider enhancement is retired; use the AI Turn classifier"
        )


def build_workbench_input_classifier_runtime(
    container: WorkbenchInputClassifierContainerPort,
    object_store: object,
) -> WorkbenchInputClassifierRuntime:
    return WorkbenchInputClassifierRuntime(
        runtime_root=container.root_dir,
        object_store=object_store,
        container=container,
    )


def _processing_recipe_content_type(body: Mapping[str, object]) -> str:
    media_type = body.get("media_type")
    if isinstance(media_type, str) and media_type.strip():
        normalized = media_type.casefold()
        for content_type in ("video", "audio", "image"):
            if content_type in normalized:
                return content_type
        return "file"
    urls = body.get("urls")
    if isinstance(urls, Sequence) and not isinstance(urls, (str, bytes)) and urls:
        return "link"
    if body.get("child_inputs") or body.get("file_name") or body.get("original_asset_ref"):
        return "file"
    return "text"


def _with_recipe_trace(
    response: WorkbenchInputClassifierEndpointResponse,
    trace: Mapping[str, object] | None,
) -> WorkbenchInputClassifierEndpointResponse:
    body = dict(response.body)
    if trace is not None:
        body["processing_recipe_trace"] = dict(trace)
    return WorkbenchInputClassifierEndpointResponse(
        status_code=response.status_code,
        body=body,
        headers=response.headers,
    )
