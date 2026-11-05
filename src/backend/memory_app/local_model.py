"""Optional loopback OpenAI-compatible generation using a local Qwen model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from backend.providers import ProviderRegistry
from backend.security.device_identity import server_mode, server_authorized
from backend.shared.server_resources import RESOURCE_POOL
from .local_vectors import LocalVectorError, encode_request, model_directory, validate_request
from .local_vector_assets import installed_assets
from .local_generation_readiness import (
    inspect_local_generation, LocalGenerationError, require_current, mark_runtime,
)


_MODEL_NAME = "qwen2.5-1.5b-instruct"
_lock = RLock()
_loaded = None


def install_local_model_routes(
    application: FastAPI, *, runtime_root: Path,
    generation_allowed: Callable[[], bool],
    embedding_policy_reader: Callable[[], dict] | None = None,
    generation_selected: Callable[[], bool] | None = None,
) -> None:
    resources = RESOURCE_POOL.get()
    model_dir = resources.model_path(_MODEL_NAME) if resources is not None else runtime_root / "data" / "models" / _MODEL_NAME
    router = APIRouter(prefix="/local-model/v1")

    @router.get("/models")
    def models():
        return {"object": "list", "data": [{"id": _MODEL_NAME, "object": "model"}]
            if inspect_local_generation(model_dir).available else []}

    @router.post("/chat/completions")
    async def complete(request: Request):
        if server_mode(request):
            if not server_authorized(request) and not request.scope.get('state', {}).get('server_internal_model'):
                raise HTTPException(401, 'device_unauthorized')
        elif request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "local_model_loopback_only")
        selected = generation_selected() if generation_selected is not None else generation_allowed()
        if not selected and not _legacy_local_provider_selected(runtime_root):
            raise HTTPException(403, "local_model_disabled")
        body = await request.json()
        messages = body.get("messages") if isinstance(body, dict) else None
        if (not isinstance(messages, list) or not messages or len(messages) > 40
                or body.get("model") not in {_MODEL_NAME, "openai/" + _MODEL_NAME}):
            raise HTTPException(400, "invalid_local_model_request")
        if any(not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}
               or not isinstance(message.get("content"), str) for message in messages):
            raise HTTPException(400, "invalid_local_model_messages")
        if sum(len(message["content"]) for message in messages) > 60_000:
            raise HTTPException(413, "local_model_input_too_large")
        try:
            inspect_local_generation(model_dir).require_available()
        except LocalGenerationError as error:
            raise HTTPException(503, str(error)) from error
        max_tokens = body.get("max_tokens", 768)
        if type(max_tokens) is not int or max_tokens < 1:
            raise HTTPException(400, "invalid_max_tokens")
        try:
            content, prompt_tokens, output_tokens, finished = await run_in_threadpool(
                _generate, model_dir, messages, min(max_tokens, 1536)
            )
        except LocalGenerationError as error:
            raise HTTPException(503, str(error)) from error
        return {
            "id": "local-" + uuid4().hex,
            "object": "chat.completion",
            "model": _MODEL_NAME,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop" if finished else "length"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": output_tokens,
                      "total_tokens": prompt_tokens + output_tokens},
        }

    @router.post('/embeddings')
    async def embeddings(request: Request):
        if server_mode(request):
            if not server_authorized(request):
                raise HTTPException(401, 'device_unauthorized')
        elif request.client is None or request.client.host not in {'127.0.0.1', '::1'}:
            raise HTTPException(403, 'local_model_loopback_only')
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'invalid_local_vector_request') from None
        models_root = resources.model_root if resources is not None else runtime_root / 'data' / 'models'
        try:
            if embedding_policy_reader is None:
                raise LocalVectorError('local_vector_not_installed')
            policy = dict(embedding_policy_reader())
            validate_request(body, policy=policy)
            directory = model_directory(models_root)
            if not installed_assets(directory, model=policy['model']):
                raise LocalVectorError('local_vector_not_installed')
            return await run_in_threadpool(encode_request, directory, body, policy=policy)
        except LocalVectorError as error:
            code = str(error)
            status = (413 if code == 'local_vector_input_too_large' else
                      429 if code == 'local_vector_queue_full' else
                      503 if code in {'local_vector_not_installed', 'local_vector_dependencies_missing',
                                      'local_vector_worker_closed', 'local_vector_not_loaded',
                                      'local_vector_unloading', 'local_vector_runtime_changed'} else 400)
            raise HTTPException(status, code) from None

    application.include_router(router)


def _legacy_local_provider_selected(runtime_root: Path) -> bool:
    fallback = {
        "name": "默认供应商", "llm_provider": "openai", "base_url": "",
        "api_path": "/chat/completions", "model": "", "models": [], "enabled": True,
    }
    providers = ProviderRegistry(runtime_root).list_readonly(fallback=fallback)
    active = next((item for item in providers if item.get("is_active") is True), None)
    if (not active or active.get("enabled") is not True
            or active.get("model") not in {_MODEL_NAME, "openai/" + _MODEL_NAME}):
        return False
    url = urlsplit(str(active.get("base_url") or ""))
    return (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            and url.path.rstrip("/") == "/local-model/v1" and not url.query and not url.fragment)


def _generate(model_dir: Path, messages: list[dict], max_tokens: int) -> tuple[str, int, int, bool]:
    global _loaded
    current = inspect_local_generation(model_dir)
    current.require_available()
    identity = current.identity
    def load():
        require_current(model_dir, identity)
        mark_runtime(identity, 'loading')
        try:
            engine = _load(model_dir)
            require_current(model_dir, identity)
        except Exception as error:
            code = str(error) if isinstance(error, LocalGenerationError) else 'local_model_load_failed'
            mark_runtime(identity, 'load_failed', reason_code=code)
            raise LocalGenerationError(code) from error
        mark_runtime(identity, 'loaded', loaded=True)
        return engine
    def infer(engine):
        require_current(model_dir, identity)
        try:
            result = _infer(engine, messages, max_tokens)
            require_current(model_dir, identity)
            return result
        except HTTPException:
            raise
        except Exception as error:
            code = str(error) if isinstance(error, LocalGenerationError) else 'local_model_inference_failed'
            mark_runtime(identity, 'inference_failed', reason_code=code, loaded=True)
            raise LocalGenerationError(code) from error
    resources = RESOURCE_POOL.get()
    if resources is not None:
        loaded = resources.acquire('qwen', (identity, 'float32'), load)
        with loaded.lock:
            return infer(loaded.engine)
    with _lock:
        if _loaded is None or _loaded[0] != identity:
            if _loaded is not None:
                mark_runtime(_loaded[0], 'available')
            _loaded = None
            _loaded = (identity, load())
        return infer(_loaded[1])


def _load(model_dir):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    torch.set_num_threads(min(8, os.cpu_count() or 4))
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    model, loading = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True,
        dtype=torch.float32, use_safetensors=True, trust_remote_code=False, output_loading_info=True)
    if any(loading.get(key) for key in ('missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs')):
        raise LocalGenerationError('local_model_weights_incomplete')
    model.eval()
    return tokenizer, model


def _infer(loaded, messages, max_tokens):
    import torch
    tokenizer, model = loaded
    encoded = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, return_tensors="pt")
    inputs = encoded["input_ids"] if isinstance(encoded, Mapping) else encoded
    if inputs.shape[-1] > 12_000:
        raise HTTPException(413, "local_model_context_too_large")
    with torch.inference_mode():
        output = model.generate(inputs, max_new_tokens=max_tokens, do_sample=False, pad_token_id=tokenizer.eos_token_id)
    tokens = output[0, inputs.shape[-1]:]
    finished = bool(len(tokens) < max_tokens or tokens[-1].item() == tokenizer.eos_token_id)
    return tokenizer.decode(tokens, skip_special_tokens=True).strip(), inputs.shape[-1], len(tokens), finished
