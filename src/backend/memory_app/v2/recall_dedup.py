"""Near-duplicate checks over authorized evidence, without generating embeddings."""
from ..retrieval_models import configured_adapter, _public_identity
from math import hypot, isfinite

DUPLICATE_THRESHOLD = 0.8


def identity(candidate):
    return candidate.get("kind"), candidate["id"], candidate["layer"]


def _documents(candidate):
    ids = set(candidate.get("document_ids", ()))
    if candidate.get("document_id"):
        ids.add(candidate["document_id"])
    for node in (candidate.get("snapshot") or {}).get("nodes", ()):
        if node.get("type") == "document":
            ids.add(node["id"])
    return ids


def _text(candidate):
    if candidate.get("kind") == "recognition":
        return candidate.get("entry", {}).get("content", candidate["excerpt"])
    return candidate["excerpt"]


def similarity(left, right, vectors):
    first, second = vectors.get(identity(left)), vectors.get(identity(right))
    if first and second and len(first) == len(second):
        first_norm, second_norm = hypot(*first), hypot(*second)
        if first_norm and second_norm and isfinite(first_norm) and isfinite(second_norm):
            return sum((a / first_norm) * (b / second_norm) for a, b in zip(first, second))
    a, b = _text(left), _text(right)
    aa, bb = set(zip(a, a[1:])), set(zip(b, b[1:]))
    union = aa | bb
    return len(aa & bb) / len(union) if union else float(bool(a) and a == b)


def is_duplicate(candidate, chosen, *, vectors, refutes):
    if any(frozenset((candidate["id"], old["id"])) in refutes for old in chosen):
        return False
    for previous in chosen:
        if candidate["layer"] != previous["layer"] and _documents(candidate) & _documents(previous):
            continue
        if similarity(candidate, previous, vectors) >= DUPLICATE_THRESHOLD:
            return True
    return False


def cached_candidate_vectors(query, candidates):
    """Read matching existing embeddings only; incomplete windows use text.

    Recognition caches cover the statement. Chunk caches cover one exact
    window plus its contextual prefix, so multi-window candidates cannot
    borrow a single chunk's vector. No provider or transport is constructed.
    """
    from types import SimpleNamespace
    import sqlite3
    from backend.recognition import RecognitionError
    from backend.recognition_retrieval import SQLiteEmbeddingCache, HttpEmbeddingProvider
    from core.search_and_recall.evidence_windows import split_evidence_chunks
    from ..source_egress import SourceEgressService
    from .layers import summary_of
    from .contextual_chunk_vectors import chunk_cache_namespace, chunk_cache_id
    from .privacy import is_private_project

    public = getattr(query.models, "public", None)
    path = query.records.database_path.parent / "recognition-vectors.sqlite3"
    if not callable(public) or not path.is_file():
        return {}
    configured = public().get("embedding", {})
    if not configured.get("configured") or not configured.get("enabled"):
        return {}
    endpoint = str(configured.get("base_url", "")).rstrip("/")
    if not endpoint.endswith("/embeddings"):
        endpoint += "/embeddings"
    local = configured.get('mode') == 'local' and configured.get('provider') == 'local'
    if local:
        model = configured_adapter(query.models, 'embedding').cache_identity
    else:
        model = HttpEmbeddingProvider(None, endpoint, configured.get("model", ""),
                                     config_revision=str(configured.get("revision"))).cache_identity
    result = {}
    cache = None
    validated = []
    try:
        cache = SQLiteEmbeddingCache(str(path), read_only=True)
        authority = SourceEgressService(query.records)
        def validate(candidate):
            entry, scope, snapshot = candidate["entry"], candidate["scope"], candidate["snapshot"]
            if not local and is_private_project(query.records, scope.project_id):
                raise ValueError("private material")
            authority.validate_snapshot(scope, snapshot)
            if not local:
                authority.require(snapshot, "generation")
            if candidate.get("kind") != "recognition":
                current = next((row for row in query.query_entries(scope.project_id, selected=[entry])
                                if row["kind"] == entry["kind"] and row["id"] == entry["id"]), None)
                if current is None or any(current.get(key) != entry.get(key) for key in
                                          ("revision", "item_revision", "title", "content", "item_id")):
                    raise ValueError("changed material")
                if query.original_snapshot(scope, current, authority) != snapshot:
                    raise ValueError("changed original")
        for candidate in candidates:
            entry, scope = candidate.get("entry"), candidate.get("scope")
            snapshot = candidate.get("snapshot")
            if not entry or scope is None or snapshot is None or (not local and is_private_project(query.records, scope.project_id)):
                continue
            validate(candidate)
            namespace, key = scope.project_id, entry["id"]
            if candidate.get("kind") != "recognition":
                windows = candidate.get("windows", ())
                if len(windows) != 1:
                    continue
                if entry["kind"] == "document":
                    markdown = query.documents.markdown(entry["id"])
                    _, start, end = summary_of(markdown)
                    spans = []
                    if start != end:
                        spans.append(("L2", markdown, start, end))
                    spans.extend(("L1", markdown, left, right) for left, right in
                                 ((0, start), (end, len(markdown))) if right > left)
                    if entry.get("item_id"):
                        item = query.records.read("workspace_items", entry["item_id"])
                        original = str(item.payload.get("source_text") or "") if item else ""
                        if original:
                            spans.append(("L0", original, 0, len(original)))
                else:
                    spans = [("L0", entry["content"], 0, len(entry["content"]))]
                matches = [chunk_cache_id(entry, layer, slot, index)
                           for slot, (layer, text, start, end) in enumerate(spans)
                           if layer == candidate["layer"] and layer != "L2"
                           for index, chunk in enumerate(split_evidence_chunks(text[start:end]))
                           if (chunk.start + start, chunk.end + start, chunk.text) ==
                              (windows[0].start, windows[0].end, windows[0].text)]
                if len(matches) != 1:
                    continue
                namespace = chunk_cache_namespace(scope.project_id, entry)
                key = matches[0]
            try:
                vector = cache.read(model_id=model, entry=SimpleNamespace(
                    project_id=namespace, id=key, revision=entry["revision"]))
            except (ValueError, OverflowError, RecognitionError):
                continue
            if vector:
                result[identity(candidate)] = vector
                validated.append(candidate)
        for candidate in validated:
            validate(candidate)
        if (_public_identity(public().get('embedding', {})) != _public_identity(configured)
                if local else public().get("embedding", {}) != configured):
            return {}
        return result
    except (sqlite3.Error, RecognitionError, ValueError):
        return {}
    finally:
        if cache is not None:
            cache.close()
