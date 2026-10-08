"""Developer prompt catalog ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.product_core.developer_studio_config import GetDeveloperStudioConfig
from core.product_core.prompt_activation import resolve_active_prompt, serialize_prompt_activation


def _developer_studio_prompt(store: object, prompt_id: str) -> Mapping[str, object] | None:
    try:
        config = GetDeveloperStudioConfig(store).execute()  # type: ignore[arg-type]
    except Exception:
        return None
    prompt = resolve_active_prompt(config, prompt_id)
    if prompt is None:
        return None
    content = prompt.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    prompt_revision = prompt.get("version")
    prompt_ref: dict[str, object] = {
        "id": prompt_id,
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
        if prompt_id in unit["prompt_ids"]:
            prompt_ref["activation_revision"] = activation["activation_revision"]
            prompt_ref["activation_unit"] = unit["unit_id"]
            prompt_ref["activation_unit_revision"] = unit["unit_revision"]
            break
    return prompt_ref


def _developer_studio_draft_prompt(store: object, prompt_id: str) -> Mapping[str, object] | None:
    try:
        config = GetDeveloperStudioConfig(store).execute()  # type: ignore[arg-type]
    except Exception:
        return None
    for prompt in config.prompts:
        if prompt.get("id") != prompt_id:
            continue
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        prompt_revision = prompt.get("version")
        prompt_ref: dict[str, object] = {
            "id": prompt_id,
            "revision": prompt_revision if isinstance(prompt_revision, int) else config.revision,
            "source": "developer_studio",
            "content": content,
        }
        stage_id = prompt.get("stageId")
        if isinstance(stage_id, str) and stage_id.strip():
            prompt_ref["stage_id"] = stage_id.strip()
        model_profile_id = prompt.get("modelProfileId")
        if isinstance(model_profile_id, str) and model_profile_id.strip():
            prompt_ref["model_profile_id"] = model_profile_id.strip()
        return prompt_ref
    return None


def _developer_studio_prompt_refs(store: object, prompt_ids: Sequence[str]) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = []
    for prompt_id in prompt_ids:
        prompt = _developer_studio_draft_prompt(store, prompt_id)
        if prompt is None:
            continue
        refs.append(
            {
                key: value
                for key, value in prompt.items()
                if key in {"id", "revision", "source", "stage_id", "model_profile_id"}
            }
        )
    return tuple(refs)


def _developer_studio_prompts(store: object, prompt_ids: Sequence[str]) -> tuple[Mapping[str, object], ...]:
    prompts: list[Mapping[str, object]] = []
    for prompt_id in prompt_ids:
        prompt = _developer_studio_prompt(store, prompt_id)
        if prompt is not None:
            prompts.append(prompt)
    return tuple(prompts)


def _developer_studio_draft_prompts(store: object, prompt_ids: Sequence[str]) -> tuple[Mapping[str, object], ...]:
    prompts: list[Mapping[str, object]] = []
    for prompt_id in prompt_ids:
        prompt = _developer_studio_draft_prompt(store, prompt_id)
        if prompt is not None:
            prompts.append(prompt)
    return tuple(prompts)
