"""Derived relevance edges and human-reviewed semantic connections."""

from datetime import datetime, timezone
from hashlib import sha1
import json
import logging
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from backend.recognition_retrieval import retrieve, SQLiteEmbeddingCache
from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.search_and_recall.evidence_windows import query_terms
from ..relations import RelationProposalService
from ..source_egress import SourceEgressService
from ..generation_sources import generation_source_guard
from .memory_turn import MemoryTurn, embedding_request
from ..retrieval_models import configured_adapter
from ..recall_state import is_recall_excluded
from ..workspace_contracts import _json, _project
from .privacy import is_private_project, egress_allowed
from .transaction_records import TransactionRecords
from .interference import mark_interference

RELATED = "v2_insight_links"
SUGGESTIONS = "v2_link_suggestions"
# Calibrated against the fixed T10.0 synonym/conflict/supersession pairs.
KEYWORD_THRESHOLD = 0.35
VECTOR_THRESHOLD = 0.80
_LOGGER = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def link_id(a, b):
    # Identifier derivation required by plan; no file content is hashed.
    return "link-" + sha1("\0".join(sorted((a, b))).encode("utf8")).hexdigest()[:16]


def similarity(a, b):
    left, right = dict(query_terms(a)), dict(query_terms(b))
    common = set(left) & set(right)
    total = sum(max(left.get(t, 0), right.get(t, 0)) for t in set(left) | set(right))
    return sum(min(left[t], right[t]) for t in common) / total if total else 0.0


class RelationSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    other_id: StrictStr
    kind: Literal["supports", "refutes", "supersedes"]
    evidence: StrictStr = Field(min_length=1, max_length=300)


class RelationSuggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: list[RelationSuggestion] = Field(max_length=3)


class InsightLinks:
    def __init__(self, records, service, models=None, *, maintenance_guard=None):
        self.records, self.service, self.models = records, service, models
        self.maintenance_guard = maintenance_guard

    def endpoint(self, project, identity, *, reader=None):
        reader = reader or self.records
        row = reader.read("recognitions", identity)
        allowed = [{"user_id": "local-user", "project_id": p} for p in {project, "me"}]
        if row is None or row.payload.get("scope") not in allowed or row.payload.get("state") != "active":
            return None
        return row

    def eligible(self, project, identity):
        row = self.endpoint(project, identity)
        if row is None or is_private_project(self.records, row.payload["project_id"]):
            return None
        scope = WorkScope("local-user", row.payload["project_id"])
        if is_recall_excluded(self.records, scope, identity):
            return None
        entry = self.service.get_recognition(scope=scope, recognition_id=identity)
        return row if entry is not None and entry.authorized else None

    def _edges(self, project, identity=None):
        for collection in (RELATED, "recognition_relations"):
            for row in self.records.list(collection):
                p = row.payload
                if collection == RELATED:
                    a, b, kind = p.get("a"), p.get("b"), "related"
                else:
                    if p.get("scope") not in [{"user_id": "local-user", "project_id": p} for p in {project, "me"}]:
                        continue
                    a, b, kind = p.get("from_id"), p.get("to_id"), p.get("relation")
                if identity is not None and identity not in {a, b}:
                    continue
                if kind not in {"related", "supports", "refutes", "supersedes"}:
                    continue
                if self.eligible(project, a) is None or self.eligible(project, b) is None:
                    continue
                yield row, a, b, kind

    def list(self, project, identity):
        if self.endpoint(project, identity) is None:
            return []
        result = []
        for row, a, b, kind in self._edges(project, identity):
            if identity in {a, b}:
                result.append(
                    {
                        "id": row.object_id,
                        "other_id": b if a == identity else a,
                        "kind": kind,
                        "state": "active",
                        "score": row.payload.get("score"),
                    }
                )
        for marker in self.records.list(SUGGESTIONS):
            if marker.payload.get("project_id") != project:
                continue
            row = self.records.read("recognition_relation_proposals", marker.object_id)
            if row is None or row.payload.get("state") != "pending":
                continue
            p = row.payload
            a, b = p["from_id"], p["to_id"]
            if identity not in {a, b} or self.eligible(project, a) is None or self.eligible(project, b) is None:
                continue
            result.append(
                {
                    "id": row.object_id,
                    "other_id": b if a == identity else a,
                    "kind": p["relation"],
                    "state": "suggested",
                    "score": None,
                }
            )
        return sorted(result, key=lambda r: (r["kind"], r["state"], r["id"]))

    def expansion_ids(self, project, anchors):
        """Cheap candidate prefilter; the query still qualifies and snapshots every retained endpoint."""
        result = set()
        if not anchors:
            return result
        for collection in (RELATED, "recognition_relations"):
            for row in self.records.list(collection):
                p = row.payload
                if collection == RELATED:
                    a, b = p.get("a"), p.get("b")
                else:
                    if p.get("scope") not in [
                        {"user_id": "local-user", "project_id": p} for p in {project, "me"}
                    ] or p.get("relation") not in {"supports", "refutes"}:
                        continue
                    a, b = p.get("from_id"), p.get("to_id")
                if self.endpoint(project, a) is None or self.endpoint(project, b) is None:
                    continue
                if a in anchors:
                    result.add(b)
                if b in anchors:
                    result.add(a)
        return result

    def neighbors(self, project, identity):
        return [r for r in self.list(project, identity) if r["state"] == "active"]

    def propose(self, project, a, b, kind, evidence, *, expected_revisions=None):
        if kind not in {"supports", "refutes", "supersedes"}:
            raise RecognitionError("invalid_link_kind")
        if self.eligible(project, a) is None or self.eligible(project, b) is None:
            raise RecognitionConflict("link_endpoint_unavailable")
        snapshots = []
        if expected_revisions is not None:
            for identity in (a, b):
                row = self.eligible(project, identity)
                wanted = expected_revisions.get(identity, row.revision if row else None)
                if row is None or type(wanted) is not int or row.revision != wanted:
                    raise RecognitionConflict("link_endpoint_changed")
                scope = WorkScope("local-user", row.payload["project_id"])
                snapshots.append((scope, SourceEgressService(self.records).snapshot(scope,
                    [{"type": "recognition", "id": identity, "revision": row.revision}])))
        with self.records.begin() as tx:
            for scope, snapshot in snapshots:
                SourceEgressService(TransactionRecords(tx)).validate_snapshot(scope, snapshot)
            result = RelationProposalService(TransactionRecords(tx), allow_persona=True).propose(
                WorkScope("local-user", project), a, b, kind, evidence, source="model"
            )
            tx.put(SUGGESTIONS, result["id"], {"project_id": project}, expected_revision=0)
            tx.commit()
        return result

    def review(self, project, identity, revision, accept):
        if type(revision) is not int or revision < 1:
            raise RecognitionError("invalid_expected_revision")
        snapshots = []
        if accept:
            proposal = self.records.read("recognition_relation_proposals", identity)
            if proposal is None:
                raise RecognitionConflict("link_suggestion_unavailable")
            for endpoint_id in (proposal.payload["from_id"], proposal.payload["to_id"]):
                row = self.eligible(project, endpoint_id)
                if row is None:
                    raise RecognitionConflict("link_endpoint_unavailable")
                scope = WorkScope("local-user", row.payload["project_id"])
                snapshots.append(
                    (
                        scope,
                        SourceEgressService(self.records).snapshot(
                            scope, [{"type": "recognition", "id": row.object_id, "revision": row.revision}]
                        ),
                    )
                )
        with self.records.begin() as tx:
            for scope, snapshot in snapshots:
                SourceEgressService(TransactionRecords(tx)).validate_snapshot(scope, snapshot)
            marker = tx.read(SUGGESTIONS, identity)
            if marker is None or marker.payload.get("project_id") != project:
                raise RecognitionConflict("link_suggestion_unavailable")
            result = RelationProposalService(TransactionRecords(tx), allow_persona=True).review(
                WorkScope("local-user", project), identity, revision, "approved" if accept else "rejected"
            )
            if accept:
                # The proposal review, active edge and recall interference commit together.
                edge_id = "relation-" + uuid4().hex
                tx.put(
                    "recognition_relations",
                    edge_id,
                    {
                        "id": edge_id,
                        "scope": {"user_id": "local-user", "project_id": project},
                        "project_id": project,
                        "from_id": result["from_id"],
                        "to_id": result["to_id"],
                        "relation": result["relation"],
                        "created_at": now(),
                    },
                    expected_revision=0,
                )
                if result["relation"] == "supersedes":
                    old = self.endpoint(project, result["to_id"], reader=tx)
                    from .insight_validity import close
                    close(tx, old.object_id, result["from_id"], tx.read('recognition_relations', edge_id).payload['created_at'])
                    previous = tx.read("recognition_recall_preferences", old.object_id)
                    if previous is None or previous.payload.get("state") != "forgotten":
                        mark_interference(tx, old.object_id, result["from_id"], now())
                        tx.put(
                            "recognition_recall_preferences",
                            old.object_id,
                            {
                                "id": old.object_id,
                                "user_id": "local-user",
                                "project_id": old.payload["project_id"],
                                "state": "cooled",
                                "by": "auto",
                                "changed_at": now(),
                            },
                            expected_revision=previous.revision if previous else 0,
                        )
                        activity = "activity-" + uuid4().hex
                        tx.put(
                            "v2_activity",
                            activity,
                            {
                                "kind": "cool",
                                "by": "auto",
                                "project_id": old.payload["project_id"],
                                "object_kind": "insight",
                                "object_id": old.object_id,
                                "created_at": now(),
                            },
                            expected_revision=0,
                        )
            tx.commit()
        return result

    def _rank(self, project, anchor, entries, *, source_refs=None, validate_current=None, limit=5, keyword_threshold=KEYWORD_THRESHOLD):
        scores = {row.object_id: similarity(anchor.payload["content"], row.payload["content"]) for row in entries}
        threshold = keyword_threshold
        public = self.models.public() if self.models is not None else {}
        configured = public.get("embedding", {})
        remote = urlsplit(str(configured.get("base_url", ""))).hostname not in {"localhost", "127.0.0.1", "::1"}
        if configured.get("configured") and configured.get("enabled"):
            # Source snapshots and the exact model config are checked on both sides of every wire.
            rows = [anchor, *entries]
            egress = SourceEgressService(self.records)
            snapshots = []
            for row in rows:
                refs = source_refs(row) if source_refs else [{"type": "recognition", "id": row.object_id, "revision": row.revision}]
                if not refs:
                    if validate_current is None:
                        raise RecognitionConflict("empty_vector_sources_require_guard")
                    continue
                own_scope = WorkScope("local-user", row.payload["project_id"])
                snapshots.append((own_scope, egress.snapshot(own_scope, refs)))

            def validate():
                if self.maintenance_guard:
                    self.maintenance_guard(self.records)
                if validate_current:
                    validate_current()
                for own_project in {row.payload["project_id"] for row in rows}:
                    if remote and not egress_allowed(self.records, self.models, own_project, "embedding"):
                        raise RecognitionConflict("embedding_remote_disabled")
                for scope, snapshot in snapshots:
                    egress.validate_snapshot(scope, snapshot)
                    if remote:
                        egress.require(snapshot, "embedding")

            try:
                validate()
                provider = configured_adapter(self.models, "embedding", validate_current=validate)
                original = provider.client

                class ObservedTransport:
                    def post_json(inner, **kwargs):
                        materials = [
                            {**ref, "project_id": scope.project_id}
                            for scope, snapshot in snapshots for ref in snapshot["roots"]
                        ]
                        # Reuse the actual provider batch as the idempotency input;
                        # no credentials or transport headers enter the key.
                        return embedding_request(self.records, self.models, project, materials,
                            {"embedding": kwargs["payload"]["input"], "materials": materials,
                             "configuration_revision": configured.get("revision")}, validate,
                            original, kwargs)

                from dataclasses import replace

                provider = replace(provider, client=ObservedTransport())
                # Existing retrieval owns vector validation, cosine scoring and revision-bound caching.
                cache = SQLiteEmbeddingCache(str(self.records.database_path.parent / "recognition-vectors.sqlite3"))
                actual_scopes = {r.object_id: r.payload["project_id"] for r in entries}

                class ScopedCache:
                    # Retrieval ranks the combined view; cache ownership remains each endpoint's real scope.
                    def read(inner, *, model_id, entry):
                        return cache.read(model_id=model_id, entry=replace(entry, project_id=actual_scopes[entry.id]))

                    def write(inner, *, model_id, entry, vector):
                        return cache.write(
                            model_id=model_id, entry=replace(entry, project_id=actual_scopes[entry.id]), vector=vector
                        )

                try:
                    result = retrieve(
                        project,
                        anchor.payload["content"],
                        [
                            {
                                "id": r.object_id,
                                "revision": r.revision,
                                "content": r.payload["content"],
                                "project_id": project,
                                "source_refs": [],
                            }
                            for r in entries
                        ],
                        limit=limit,
                        vector_candidate_limit=limit,
                        keyword_enabled=False,
                        embedding_provider=provider,
                        embedding_cache=ScopedCache(),
                    )
                    validate()
                    if result.trace.get("vector", {}).get("status") != "used":
                        raise RecognitionError("link_vectors_unavailable")
                    scores = {hit.id: hit.vector_score for hit in result.hits}
                    entries = [r for r in entries if r.object_id in scores]
                finally:
                    cache.close()
                threshold = VECTOR_THRESHOLD
            except Exception as error:
                _LOGGER.warning("link_vector_fallback exception_type=%s", type(error).__name__)
        ranked = sorted(entries, key=lambda r: (-scores[r.object_id], r.object_id))[:limit]
        return [(r, scores[r.object_id]) for r in ranked], threshold

    def extraction_neighbors(self, project, experience, scene):
        """Read the real active scene/project/persona neighborhood before extraction."""
        from .insights import insight_view
        from .policies import get
        from .policies.types import ScopeInput
        from types import SimpleNamespace
        entries = []
        for row in self.records.list('recognitions'):
            if self.eligible(project, row.object_id) is None:
                continue
            own = row.payload['project_id']
            view = insight_view(self.records, WorkScope('local-user', own), row.object_id, service=self.service)
            if own != 'me' and not get('scope')(ScopeInput(scene, view['scene'])):
                continue
            try:
                authority = SourceEgressService(self.records)
                authority.require(authority.snapshot(WorkScope('local-user', own),
                    [{'type': 'recognition', 'id': row.object_id, 'revision': row.revision}]), 'generation')
            except RecognitionConflict:
                continue
            entries.append(row)
        anchor = SimpleNamespace(object_id=experience.id, revision=experience.revision,
            payload={'project_id': project, 'content': experience.content})
        refs = lambda row: [{'type': 'experience' if row.object_id == experience.id else 'recognition',
            'id': row.object_id, 'revision': row.revision}]
        ranked, _ = self._rank(project, anchor, entries, source_refs=refs)
        return [row for row, _ in ranked]

    def propose_extraction_support(self, project, experience, document, target, evidence, identity):
        """Reuse human-reviewed evidence support; no recognition or edge is published."""
        from .consolidation import evidence_guard
        scope = WorkScope('local-user', project)
        authority = SourceEgressService(self.records)
        target_row = self.eligible(project, target)
        if target_row is None:
            raise RecognitionConflict('link_endpoint_unavailable')
        return RelationProposalService(self.records, allow_persona=True, evidence_guard=evidence_guard).propose_evidence(
            scope, target, [experience.id], [document], evidence, proposal_id=identity,
            snapshots=[{'project_id': project, 'snapshot': authority.snapshot(scope,
                [{'type': 'experience', 'id': experience.id, 'revision': experience.revision}])},
                {'project_id': target_row.payload['project_id'], 'snapshot': authority.snapshot(
                    WorkScope('local-user', target_row.payload['project_id']),
                    [{'type': 'recognition', 'id': target, 'revision': target_row.revision}])}])

    def propose_candidate_hint(self, project, candidate_id, recognition_id):
        """After publication, reuse only the still-matching frozen neighbor."""
        candidate = self.records.read('recognition_candidates', candidate_id)
        hint = self.records.read('v2_candidate_hints', candidate_id)
        if candidate is None or hint is None or hint.payload.get('project_id') != project:
            return None
        kind = {'supplement': 'supports', 'differs': 'refutes', 'may_supersede': 'supersedes'}.get(hint.payload.get('relation'))
        if kind is None or candidate.payload.get('recognition_id') != recognition_id:
            return None
        try:
            turn_id = 'memory-' + UUID(candidate.payload['generation']['id']).hex
        except (KeyError, TypeError, ValueError):
            return None
        frozen = self.records.read('v2_extract_inputs', turn_id)
        wanted = next((row for row in frozen.payload['neighbors']
            if row['id'] == hint.payload.get('target_id')), None) if frozen else None
        if wanted is None:
            return None
        target = self.eligible(project, wanted['id'])
        if (target is None or type(wanted['revision']) is not int or target.revision != wanted['revision']
                or target.payload['project_id'] != wanted['project_id']):
            return None
        return self.propose(project, recognition_id, target.object_id,
            kind, candidate.payload['content'], expected_revisions={target.object_id: wanted['revision']})

    def discover(self, project, identity):
        if self.maintenance_guard:
            self.maintenance_guard(self.records)
        anchor = self.eligible(project, identity)
        if anchor is None:
            return []
        entries = [
            r
            for r in self.records.list("recognitions")
            if r.object_id != identity and self.eligible(project, r.object_id)
        ]
        ranked, threshold = self._rank(project, anchor, entries)
        relevant = {r.object_id: (r, score) for r, score in ranked if score >= threshold}
        with self.records.begin() as tx:
            if self.maintenance_guard:
                self.maintenance_guard(tx)
            current = tx.read("recognitions", identity)
            if current != anchor:
                raise RecognitionConflict("link_anchor_changed")
            for row in tx.list(RELATED):
                if identity in {row.payload.get("a"), row.payload.get("b")}:
                    other = row.payload["b"] if row.payload["a"] == identity else row.payload["a"]
                    # Only replace this project's eligible neighborhood; other projects keep theirs.
                    if self.endpoint(project, other, reader=tx) is not None and other not in relevant:
                        tx.delete(RELATED, row.object_id, expected_revision=row.revision)
            for other, (row, score) in relevant.items():
                if tx.read("recognitions", other) != row:
                    raise RecognitionConflict("link_neighbor_changed")
                edge_id = link_id(identity, other)
                old = tx.read(RELATED, edge_id)
                a, b = sorted((identity, other))
                tx.put(
                    RELATED,
                    edge_id,
                    {"a": a, "b": b, "kind": "related", "score": score, "computed_at": now()},
                    expected_revision=old.revision if old else 0,
                )
            tx.commit()
        self._suggest(project, anchor, [r for r, _ in ranked[:3]])
        return self.list(project, identity)

    def _suggest(self, project, anchor, others):
        if not others or self.models is None:
            return
        public = self.models.public()
        config = public.get("generation", {})
        if not config.get("configured") or config.get("enabled") is False:
            return
        rows = [anchor, *others]
        # Each endpoint pair/revision is judged once, including an empty or dismissed result.
        signature = [[r.object_id, r.revision] for r in rows]

        def already_run(reader):
            return any(
                r.payload.get("project_id") == project and r.payload.get("endpoints") == signature
                for r in reader.list("v2_link_discovery_runs")
            )

        if already_run(self.records):
            return
        run_id = "link-run-" + uuid4().hex
        try:
            guards = [
                generation_source_guard(
                    SourceEgressService(self.records),
                    self.models,
                    WorkScope("local-user", r.payload["project_id"]),
                    [{"type": "recognition", "id": r.object_id, "revision": r.revision}],
                )
                for r in rows
            ]

            def validate():
                if self.maintenance_guard:
                    self.maintenance_guard(self.records)
                for guard in guards:
                    guard()
                remote = urlsplit(str(config.get("base_url", ""))).hostname not in {"localhost", "127.0.0.1", "::1"}
                if remote and any(
                    not egress_allowed(self.records, self.models, r.payload["project_id"], "generation") for r in rows
                ):
                    raise RecognitionConflict("generation_remote_disabled")
                for r in rows:
                    if self.eligible(project, r.object_id) != r:
                        raise RecognitionConflict("link_endpoint_changed")

            messages = [
                {
                    "role": "system",
                    "content": "判断认识之间是否支持、矛盾或取代。from为当前认识，supersedes表示当前认识取代other。仅提出建议，不确认。"
                    '只返回JSON {"suggestions":[{"other_id":"邻居id","kind":"supports|refutes|supersedes","evidence":"一句依据"}]}。无关系返回空数组。',
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "from": {
                                "id": anchor.object_id,
                                "text": anchor.payload["content"],
                                "conditions": anchor.payload.get("conditions", []),
                            },
                            "others": [
                                {
                                    "id": r.object_id,
                                    "text": r.payload["content"],
                                    "conditions": r.payload.get("conditions", []),
                                }
                                for r in others
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            turn = MemoryTurn(self.records, self.models, kind="memory.link_suggest", project=project,
                key=signature, materials=[{"type": "recognition", "id": r.object_id,
                    "revision": r.revision, "project_id": r.payload["project_id"]} for r in rows], validate=validate)
            output, _ = turn.generate(messages, response_model=RelationSuggestions, max_tokens=900)
            validate()
            allowed = {r.object_id for r in others}
            decisions = output.model_dump()["suggestions"]
            if len({r["other_id"] for r in decisions}) != len(decisions) or any(
                r["other_id"] not in allowed or not r["evidence"].strip() for r in decisions
            ):
                raise RecognitionError("invalid_link_suggestion")
            def commit():
                with self.records.begin() as tx:
                    if already_run(tx):
                        return
                    for r in rows:
                        if tx.read("recognitions", r.object_id) != r:
                            raise RecognitionConflict("link_endpoint_changed")
                    if self.maintenance_guard:
                        self.maintenance_guard(tx)
                    domain = RelationProposalService(TransactionRecords(tx), allow_persona=True)
                    for decision in decisions:
                        result = domain.propose(
                            WorkScope("local-user", project),
                            anchor.object_id,
                            decision["other_id"],
                            decision["kind"],
                            decision["evidence"],
                            source="model",
                        )
                        tx.put(SUGGESTIONS, result["id"], {"project_id": project}, expected_revision=0)
                    tx.put(
                        "v2_link_discovery_runs",
                        run_id,
                        {"project_id": project, "endpoints": signature, "computed_at": now()},
                        expected_revision=0,
                    )
                    tx.commit()
                return True
            turn.propose(key="links", write=commit,
                existing=lambda: True if already_run(self.records) else None)
        except Exception as error:
            _LOGGER.warning("link_suggestion_failed exception_type=%s", type(error).__name__)

    def run(self):
        """Shared daily scheduler entry; failures do not block other projects."""
        completed = 0
        for row in self.records.list("recognitions"):
            scope = row.payload.get("scope", {})
            if row.payload.get("state") != "active" or scope.get("user_id") != "local-user":
                continue
            try:
                self.discover(scope["project_id"], row.object_id)
                completed += 1
            except Exception as error:
                _LOGGER.warning("link_discovery_failed exception_type=%s", type(error).__name__)
        return completed

    def spread(self, project, identity, usage, *, only_project=None):
        seen = set()
        for edge in self.neighbors(project, identity):
            other = edge["other_id"]
            if other in seen:
                continue
            seen.add(other)
            row = self.eligible(project, other)
            if row is not None and (only_project is None or row.payload["project_id"] == only_project):
                usage.record_usage("insight", other, row.payload["project_id"], 0.2, count=False)


def install_link_routes(application, *, records, service):
    links = InsightLinks(records, service)
    router = APIRouter(prefix="/api/v2/library")

    @router.get("/insights/{identity}/links")
    async def listing(identity: str, project_id: str = "default"):
        return {"links": links.list(_project(project_id), _project(identity))}

    async def review(identity, request, accept):
        body = await _json(request)
        if (
            set(body) != {"project_id", "expected_revision"}
            or type(body.get("expected_revision")) is not int
            or body["expected_revision"] < 1
        ):
            raise HTTPException(400, "invalid_link_fields")
        try:
            return links.review(_project(body["project_id"]), _project(identity), body["expected_revision"], accept)
        except RecognitionConflict:
            raise HTTPException(409, "link_revision_conflict") from None
        except RecognitionError:
            raise HTTPException(400, "invalid_link_suggestion") from None

    @router.post("/link-suggestions/{identity}/accept")
    async def accept(identity: str, request: Request):
        return await review(identity, request, True)

    @router.post("/link-suggestions/{identity}/dismiss")
    async def dismiss(identity: str, request: Request):
        return await review(identity, request, False)

    application.include_router(router)
