"""Explicit model adapters with no environment-key or hidden remote fallback."""
from pathlib import Path
import json
import sqlite3
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from backend.recognition_retrieval import HttpEmbeddingProvider, HttpReranker, SQLiteEmbeddingCache, SqliteVecCandidateIndex, RecognitionRetrievalError, retrieve
from backend.recognition import RecognitionConflict
from .local_vectors import encode_request, model_directory


class ConfiguredTransport:
    """A single-purpose wire adapter guarded by the captured authority.

    ``post_json`` deliberately checks on both sides of a provider request.  A
    caller can therefore revoke a recognition or replace its configured model
    while an embedding request is in flight without permitting a subsequent
    rerank request, or accepting the response that raced the change.
    """

    def __init__(self, key: str, validate_current: Callable[[], None] | None = None):
        self._key = key
        self._validate_current = validate_current

    def post_json(self, *, endpoint, payload):
        self._check_current()
        try:
            with httpx.Client(timeout=35, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", endpoint, json=payload, headers={"Authorization": "Bearer " + self._key}) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 8 * 1024 * 1024:
                            raise ValueError("response too large")
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError("invalid response")
        except Exception:
            raise RecognitionRetrievalError("configured_model_request_failed") from None
        # Keep this outside the transport-error wrapper.  A changed source or
        # configuration is an authority conflict, never a provider failure
        # which the retrieval pipeline may safely degrade around.
        self._check_current()
        return result

    def _check_current(self) -> None:
        if self._validate_current is not None:
            self._validate_current()


class LocalVectorTransport(ConfiguredTransport):
    """保留原请求两侧资格检查，仅把本机编码交给同进程工作线程。"""
    def __init__(self, models_root, validate_current, *, policy):
        super().__init__('local-vector', validate_current)
        self.directory = model_directory(models_root)
        self.policy = dict(policy)

    def post_json(self, *, endpoint, payload):
        self._check_current()
        if endpoint != 'http://127.0.0.1:8001/local-model/v1/embeddings':
            raise RecognitionRetrievalError('local_vector_endpoint_invalid')
        if payload.get('input_type') in {'query', 'document'}:
            result = encode_request(self.directory, payload, policy=self.policy)
        else:
            texts = payload['input']
            query = encode_request(self.directory, {**payload, 'input': texts[:1], 'input_type': 'query'}, policy=self.policy)
            self._check_current()
            documents = (encode_request(self.directory, {**payload, 'input': texts[1:], 'input_type': 'document'}, policy=self.policy)
                         if len(texts) > 1 else {'data': [], 'usage': {'prompt_tokens': 0}})
            vectors = [*query['data'], *documents['data']]
            tokens = query['usage']['prompt_tokens'] + documents['usage']['prompt_tokens']
            result = {**query, 'data': [{**item, 'index': index} for index, item in enumerate(vectors)],
                      'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}
        self._check_current()
        return result


def configured_adapter(models, purpose, *, validate_current: Callable[[], None] | None = None):
    """Build an adapter from an exact public configuration snapshot.

    The optional authority validator is intentionally separate from model
    configuration: the app supplies it to re-read selected recognitions.  The
    model guard is composed here so every wire call has both checks.
    """
    cfg, expected_public = _capture_remote_config(models, purpose)
    local = purpose == 'embedding' and cfg.get('mode') == 'local'
    policy = models.embedding_policy() if local else None

    def check_current() -> None:
        _assert_public_config_unchanged(models, purpose, expected_public)
        if local and models.embedding_policy() != policy:
            raise RecognitionRetrievalError('configured_model_changed_before_request')
        _validate_retrieval_egress(cfg, policy=policy)
        if validate_current is not None:
            validate_current()

    endpoint = cfg["base_url"].rstrip("/")
    suffix = "embeddings" if purpose == "embedding" else "rerank"
    if not endpoint.endswith("/" + suffix):
        endpoint += "/" + suffix
    transport = (LocalVectorTransport(models._local_models_root, check_current, policy=policy) if local else
                 ConfiguredTransport(cfg["api_key"], check_current))
    if purpose == "embedding":
        return HttpEmbeddingProvider(transport, endpoint, cfg.get('model_key', cfg['model']),
            config_revision=(f"{cfg['revision']}:{cfg['mode_revision']}" if local else str(cfg['revision'])))
    if purpose == "rerank":
        return HttpReranker(transport, endpoint, cfg["model"])
    raise RecognitionRetrievalError("unsupported retrieval purpose")


def retrieval_options(models, runtime_root: Path, *, validate_current: Callable[[], None] | None = None):
    public = models.public()
    options = {}
    if public["embedding"].get("configured") and public["embedding"].get("enabled"):
        options.update(embedding_provider=configured_adapter(models, "embedding", validate_current=validate_current),
                       sqlite_vec_index=SqliteVecCandidateIndex())
    if public["rerank"].get("configured") and public["rerank"].get("enabled"):
        options["reranker"] = configured_adapter(models, "rerank", validate_current=validate_current)
    if "embedding_provider" in options:
        options["embedding_cache"] = SQLiteEmbeddingCache(str(runtime_root / "recognition-vectors.sqlite3"))
    return options


def configured_retrieve(models, runtime_root: Path, project_id, query, entries, *, limit=8,
                         validate_current: Callable[[], None] | None = None,
                         source_egress=None, source_scope=None):
    """Own the cache connection in the worker performing the complete query."""
    cache = None
    expected_model_public = _enabled_remote_public(models)
    filters, source_snapshots = _source_filters(models, entries, source_egress, source_scope)

    def check_current():
        _check_current(validate_current)
        _assert_enabled_remote_public_unchanged(models, expected_model_public)
        for snapshot in source_snapshots:
            source_egress.validate_snapshot(source_scope, snapshot)

    try:
        check_current()
        options = retrieval_options(models, runtime_root, validate_current=check_current)
        _assert_enabled_remote_public_unchanged(models, expected_model_public)
        cache = options.get("embedding_cache")
        result = retrieve(project_id, query, entries, limit=limit, **options, **filters)
    except (sqlite3.Error, OSError):
        check_current()
        result = retrieve(project_id, query, entries, limit=limit)
        fallback = {"status": "degraded", "reason": "local_vector_cache_unavailable"}
        trace = {**result.trace, "vector": dict(fallback)}
        if "rerank" in expected_model_public:
            # This fallback intentionally makes no remote requests, including
            # a configured reranker. Do not report it as never configured.
            trace["rerank"] = dict(fallback)
        result = type(result)(hits=result.hits, trace=trace)
    finally:
        if cache is not None:
            cache.close()
    # ``retrieve`` deliberately degrades vector/rerank failures.  This final
    # check prevents such a degraded result from being accepted after its
    # selection authority changed during that work.
    check_current()
    return result


def _source_filters(models, entries, authority, scope):
    """Keep local lexical access and exclude private sources from remote models."""
    if authority is None:
        return {}, []
    if scope is None:
        raise RecognitionRetrievalError("source scope is required")
    public = models.public()
    remote = [purpose for purpose in ("embedding", "rerank")
              if public[purpose].get("configured") and public[purpose].get("enabled")
              and urlsplit(str(public[purpose].get("base_url", ""))).hostname
              not in {"localhost", "127.0.0.1", "::1"}]
    filters = {purpose + "_allowed_ids": set() for purpose in remote}
    snapshots = []
    if not remote:
        return filters, snapshots
    for entry in entries:
        try:
            snapshot = authority.snapshot(scope, [{"type": "recognition", "id": entry["id"], "revision": entry["revision"]}])
        except RecognitionConflict:
            # Unresolved legacy provenance cannot grant remote access. It may
            # still participate in the independently authorized local search.
            continue
        snapshots.append(snapshot)
        try:
            authority.require(snapshot, remote[0])
        except RecognitionConflict:
            continue
        for purpose in remote:
            filters[purpose + "_allowed_ids"].add(entry["id"])
    return filters, snapshots


def _check_current(validate_current: Callable[[], None] | None) -> None:
    if validate_current is not None:
        validate_current()


def _capture_remote_config(models, purpose: str) -> tuple[dict, tuple[object, ...]]:
    public = _public_purpose(models, purpose)
    cfg = models.snapshot(purpose)
    snapshot_public = _public_from_snapshot(cfg, purpose)
    expected = _public_identity(public)
    if _public_identity(snapshot_public) != expected:
        raise RecognitionRetrievalError("configured_model_changed_before_request")
    return cfg, expected


def _assert_public_config_unchanged(models, purpose: str, expected: tuple[object, ...]) -> None:
    try:
        current = _public_purpose(models, purpose)
    except Exception:
        raise RecognitionRetrievalError("configured_model_changed_before_request") from None
    if _public_identity(current) != expected:
        raise RecognitionRetrievalError("configured_model_changed_before_request")


def _public_purpose(models, purpose: str) -> Mapping[str, object]:
    try:
        public = models.public()
        item = public[purpose]
    except Exception:
        raise RecognitionRetrievalError("configured_model_changed_before_request") from None
    if not isinstance(item, Mapping):
        raise RecognitionRetrievalError("configured_model_changed_before_request")
    return item


def _public_from_snapshot(cfg: Mapping[str, object], purpose: str) -> dict[str, object]:
    public = {
        "purpose": cfg.get("purpose", purpose), "provider": cfg.get("provider", "openai"),
        "base_url": cfg.get("base_url"), "model": cfg.get("model"),
        "allow_remote": cfg.get("allow_remote"), "enabled": cfg.get("enabled"),
        "revision": cfg.get("revision"), "configured": bool(cfg.get("api_key") and cfg.get("base_url") and cfg.get("model")),
        "has_api_key": bool(cfg.get("api_key")),
    }
    if purpose == 'embedding' and 'mode_revision' in cfg:
        public.update(mode=cfg['mode'], mode_revision=cfg['mode_revision'])
        if cfg['mode'] == 'local':
            public.update(configured=cfg['configured'], has_api_key=False, model_key=cfg['model_key'])
    return public


def _public_identity(value: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(value.get(key) for key in (
        "purpose", "provider", "base_url", "model", "allow_remote", "enabled",
        "revision", "configured", "has_api_key",
        'mode', 'mode_revision',
        'model_key',
    ))


def _enabled_remote_public(models) -> dict[str, tuple[object, ...]]:
    """Capture exactly the retrieval purposes that can make wire requests."""
    return {
        purpose: _public_identity(_public_purpose(models, purpose))
        for purpose in ("embedding", "rerank")
        if _public_purpose(models, purpose).get("configured") is True
        and _public_purpose(models, purpose).get("enabled") is True
    }


def _assert_enabled_remote_public_unchanged(models, expected: Mapping[str, tuple[object, ...]]) -> None:
    if _enabled_remote_public(models) != expected:
        raise RecognitionRetrievalError("configured_model_changed_before_request")
    for purpose, identity in expected.items():
        _assert_public_config_unchanged(models, purpose, identity)


def _validate_retrieval_egress(cfg: Mapping[str, object], *, policy=None) -> None:
    """Repeat the stored remote-consent check at the wire boundary."""
    if cfg.get('mode') == 'local':
        if (policy is None or cfg.get('provider') != 'local' or cfg.get('purpose') != 'embedding'
                or cfg.get('base_url') != 'http://127.0.0.1:8001/local-model/v1'
                or cfg.get('model') != policy['model'] or cfg.get('model_key') != policy['model_key']
                or cfg.get('allow_remote') is not False):
            raise RecognitionRetrievalError('local_vector_configuration_invalid')
        return
    if cfg.get("provider") != "openai" or not isinstance(cfg.get("base_url"), str) or not str(cfg["base_url"]).strip():
        raise RecognitionRetrievalError("configured_model_egress_not_configured")
    try:
        parsed = urlsplit(str(cfg["base_url"]))
    except ValueError:
        raise RecognitionRetrievalError("configured_model_egress_endpoint_invalid") from None
    host = parsed.hostname.lower() if parsed.hostname else ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not host:
        raise RecognitionRetrievalError("configured_model_egress_endpoint_invalid")
    if host in {"localhost", "127.0.0.1", "::1"}:
        if parsed.scheme not in {"http", "https"}:
            raise RecognitionRetrievalError("configured_model_egress_endpoint_invalid")
        return
    if parsed.scheme != "https" or cfg.get("allow_remote") is not True:
        raise RecognitionRetrievalError("configured_model_egress_remote_not_consented")
