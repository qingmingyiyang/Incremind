"""Fail-closed source egress authority for local recognition records.

This is a storage-level primitive.  Callers must take and validate a snapshot
around a model operation.  A retained model artifact is a special derived
source: it retains the exact task/document/packet evidence that produced it,
then inherits privacy from the frozen packet and the original roots.
Descriptive provenance alone never grants authority.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from contextlib import ExitStack
from .source_snapshot import _closure_identity
from .source_graph import SourceGraph, owner_metadata, validate_graph
from .original_sources import ORIGINAL_TYPES, original, source_store, document_roots, resolve

from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from backend.recognition.external_turn_facts import source_is_committed
from backend.recognition.external_evidence_json import encoded
from backend.recognition_retrieval.cache_invalidation import VectorCacheInvalidator, chunk_cache_parent
from core.search_and_recall.vector_cache_invalidation import vector_cache_path
from .transaction_records import TransactionRecords
from backend.recognition.artifact_dependencies import ArtifactDependencyError, read_artifact_dependencies
from backend.recognition.experience_origins import ExperienceOriginError, read_experience_origin
from backend.recognition.document_filings import (
    DocumentFilingError, FILED_EDIT_FIELDS, FILED_EDIT_OWNER_FIELDS, read_filed_edit_dependencies,
    validate_filed_edit_owner,
)
from backend.recognition.product_draft_dependencies import (
    PRODUCT_DRAFT_FIELDS, ProductDraftDependencyError, read_product_draft_dependencies,
)
from backend.recognition.external_input_dependencies import (
    FIELDS as EXTERNAL_INPUT_FIELDS, read_external_input_dependencies, metadata as external_input_metadata,
    ExternalInputDependencyError,
)
from .privacy_state import is_private_project, privacy_revision


_EXPERIENCES = "recognition_experiences"
_RECOGNITIONS = "recognitions"
_TYPES = {"experience": _EXPERIENCES, "recognition": _RECOGNITIONS,
          "original_item": "workspace_items", "original_source": "sources"}
POLICY_COLLECTIONS = {
    "original_item": "source_egress_original_item_policies",
    "original_source": "source_egress_original_source_policies",
    "experience": "source_egress_experience_policies",
    "recognition": "source_egress_recognition_policies",
}
_PURPOSES = ("generation", "embedding", "rerank")
_MAX_NODES = 256
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


def validate_product_draft_source(reader, scope, source):
    """在调用方事务内核验原成果闭包，私密材料仍可在本机确认。"""
    return SourceEgressService(TransactionRecords(reader)).snapshot(scope,
        [{'type':'experience', 'id':source.object_id, 'revision':source.revision}])


class SourcePrivacyInputError(RecognitionError):
    """The compatibility policy input must express exactly one privacy state."""


class SourceEgressService:
    """Version-bound egress policy and immutable, body-free source snapshots."""

    def __init__(self, records, *, _memo=None) -> None:
        self._records = records
        self._memo = _memo

    def set_policy(
        self, scope: WorkScope, source_type: str, source_id: str,
        expected_source_revision: int, expected_policy_revision: int,
        allowed_purposes: Sequence[str],
    ) -> dict[str, object]:
        source_type, source_id = _source_key(source_type, source_id)
        _revision(expected_source_revision, "source revision")
        _nonnegative_revision(expected_policy_revision, "policy revision")
        try:
            purposes = _purposes(allowed_purposes)
        except RecognitionError as exc:
            raise SourcePrivacyInputError(str(exc)) from exc
        if purposes and purposes != set(_PURPOSES):
            raise SourcePrivacyInputError("source privacy requires no purposes or all purposes")
        with self._records.begin() as uow, ExitStack() as locks:
            if source_type == "original_source":
                locks.enter_context(source_store(uow).locked("sources",source_id))
            source = _source(uow, scope, source_type, source_id)
            if source.revision != expected_source_revision:
                raise RecognitionConflict("source revision conflicted")
            policy_id = source_id
            collection = _policy_collection(source_type)
            current = uow.read(collection, policy_id)
            if current is not None and _policy_scope(current.payload) != scope:
                raise RecognitionConflict("source policy is unavailable in this work scope")
            if (current.revision if current is not None else 0) != expected_policy_revision:
                raise RecognitionConflict("source policy revision conflicted")
            inherited = self._effective(
                uow, scope, source_type, source_id, {}, (), (source_type, source_id, purposes)
            )
            if purposes and not inherited:
                raise RecognitionError("source policy cannot broaden its inherited authority")
            invalidate_sources(uow, scope, ((source_type, source_id),))
            saved = uow.put(
                collection, policy_id,
                {"id": policy_id, "source_type": source_type, "source_id": source_id,
                 "scope": _scope(scope), "user_id": scope.user_id, "project_id": scope.project_id,
                 "source_revision": source.revision, "allowed_purposes": sorted(purposes),
                 **({"incarnation": source.payload["_original_incarnation"]} if source_type == "original_source" else {})},
                expected_revision=expected_policy_revision,
            )
            uow.commit()
        return {"source_type": source_type, "source_id": source_id,
                "source_revision": source.revision, "policy_revision": saved.revision,
                "allowed_purposes": sorted(purposes)}

    def private_project(self, scope):
        return is_private_project(self._records,scope.project_id)

    def policy(self, scope, source_type, source_id):
        with self._records.begin() as uow:
            source = _source(uow,scope,source_type,source_id)
            nodes = {}
            effective = self._effective(uow,scope,source_type,source_id,nodes,(),None)
            inherited = not self._effective(uow,scope,source_type,source_id,{},(),
                (source_type,source_id,set(_PURPOSES)))
            node = nodes[(source_type,source_id)]
            return {"source_type":source_type,"source_id":source_id,
                "source_revision":source.revision,"policy_revision":node["policy_revision"],
                "allowed_purposes":sorted(effective),"inherited":inherited}

    def snapshot(self, scope: WorkScope, refs: Sequence[Mapping[str, object]]) -> dict[str, object]:
        return self._snapshot(scope, refs, (), set())

    def _snapshot(self, scope, refs, trail, budget, memo=None):
        memo = self._memo if memo is None else memo
        memo = {} if memo is None else memo
        roots = _refs(refs)
        nodes: dict[tuple[str, str], dict[str, object]] = {}
        closures: dict[tuple[str, str], set[tuple[str, str]]] = {}
        with self._records.begin() as uow:
            current_privacy_revision = privacy_revision(uow)
            for source_type, source_id, wanted_revision in roots:
                source = _source(uow, scope, source_type, source_id)
                if source.revision != wanted_revision:
                    raise RecognitionConflict("source revision conflicted")
                self._effective(uow, scope, source_type, source_id, nodes, trail, None, closures, budget, memo)
        return {"schema_version": 1, "scope": _scope(scope), "privacy_revision": current_privacy_revision,
                "roots": [{"type": kind, "id": item, "revision": revision} for kind, item, revision in roots],
                "nodes": [nodes[key] for key in sorted(nodes)]}

    def snapshot_original_content(self, scope, identity, *, span, input_binding=None):
        """Freeze only an admitted original's immutable input and its authority."""
        from .original_sources import original_content_identity
        with self._records.begin() as reader:
            content = original_content_identity(reader, scope, identity, span, input_binding=input_binding)
            source = original(reader, scope, 'original_item', identity)
            policy = reader.read(POLICY_COLLECTIONS['original_item'], identity)
            nodes, closures = {}, {}
            self._effective(reader, scope, 'original_item', identity, nodes, (), None, closures)
            snapshot = {'nodes':[nodes[key] for key in sorted(nodes)], 'privacy_revision':privacy_revision(reader)}
            self.require(snapshot, 'generation')
            return {'scope':_scope(scope), 'id':identity, 'content':content,
                    'privacy_revision':snapshot['privacy_revision'],
                    'policy':dict(policy.payload) if policy else None,
                    'policy_revision':policy.revision if policy else 0,
                    'nodes':[{key:value for key, value in node.items() if key != 'source_revision'}
                             for node in snapshot['nodes']]}

    def validate_original_content(self, scope, snapshot, purpose):
        from .original_sources import original_content_identity
        if not isinstance(snapshot, Mapping) or snapshot.get('scope') != _scope(scope):
            raise RecognitionConflict('original input binding is invalid')
        content = snapshot['content']
        frozen = content['identity']['source_text']
        binding = content.get('input_binding')
        span = content['span'] if binding is not None else frozen[content['start']:content['end']]
        with self._records.begin() as reader:
            identity = snapshot['id']
            if original_content_identity(reader, scope, identity, span, input_binding=binding) != content:
                raise RecognitionConflict('original input identity changed')
            if privacy_revision(reader) != snapshot['privacy_revision']:
                raise RecognitionConflict('original input privacy changed')
            policy = reader.read(POLICY_COLLECTIONS['original_item'], identity)
            if ((policy.revision if policy else 0) != snapshot['policy_revision']
                    or (dict(policy.payload) if policy else None) != snapshot['policy']):
                raise RecognitionConflict('original input authority changed')
            source = original(reader, scope, 'original_item', identity)
            nodes_by_id, closures = {}, {}
            self._effective(reader, scope, 'original_item', identity, nodes_by_id, (), None, closures)
            current = {'nodes':[nodes_by_id[key] for key in sorted(nodes_by_id)]}
            nodes = [{key:value for key, value in node.items() if key != 'source_revision'} for node in current['nodes']]
            if nodes != snapshot['nodes']:
                raise RecognitionConflict('original input provenance changed')
            self.require(current, purpose)

    def validate_snapshot(self, scope: WorkScope, snapshot: Mapping[str, object]) -> None:
        if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != 1 or snapshot.get("scope") != _scope(scope):
            raise RecognitionConflict("source egress snapshot is invalid")
        raw_nodes = snapshot.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)) or not raw_nodes:
            raise RecognitionConflict("source egress snapshot is invalid")
        seen: set[tuple[str, str]] = set()
        expected_privacy_revision = snapshot.get("privacy_revision")
        if type(expected_privacy_revision) is not int or expected_privacy_revision < 0:
            raise RecognitionConflict("source egress snapshot is invalid")
        with self._records.begin() as uow:
            if privacy_revision(uow) != expected_privacy_revision:
                raise RecognitionConflict("source egress snapshot conflicted")
            for raw in raw_nodes:
                if not isinstance(raw, Mapping):
                    raise RecognitionConflict("source egress snapshot is invalid")
                source_type, source_id = _source_key(raw.get("type"), raw.get("id"))
                key = (source_type, source_id)
                if key in seen:
                    raise RecognitionConflict("source egress snapshot is invalid")
                seen.add(key)
                expected_source = raw.get("source_revision")
                expected_policy = raw.get("policy_revision")
                _revision(expected_source, "snapshot source revision")
                _nonnegative_revision(expected_policy, "snapshot policy revision")
                source = _source(uow, scope, source_type, source_id)
                policy = uow.read(_policy_collection(source_type), source_id)
                if (source.revision != expected_source
                        or (policy.revision if policy is not None else 0) != expected_policy):
                    raise RecognitionConflict("source egress snapshot conflicted")
                if policy is not None and _policy_scope(policy.payload) != scope:
                    raise RecognitionConflict("source egress snapshot conflicted")
        # Re-taking the graph catches malformed or incomplete snapshots and also
        # verifies effective permissions without trusting caller-provided fields.
        fresh = self.snapshot(scope, snapshot.get("roots"))
        if fresh != snapshot:
            raise RecognitionConflict("source egress snapshot conflicted")

    def require(self, snapshot: Mapping[str, object], purpose: str) -> None:
        if purpose not in _PURPOSES or not isinstance(snapshot, Mapping):
            raise RecognitionError("source egress purpose is invalid")
        nodes = snapshot.get("nodes")
        if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)) or not nodes:
            raise RecognitionConflict("source egress snapshot is invalid")
        for node in nodes:
            if not isinstance(node, Mapping):
                raise RecognitionConflict("source egress snapshot is invalid")
            try:
                effective = _purposes(node.get("effective_purposes"))
            except RecognitionError as exc:
                raise RecognitionConflict("source egress snapshot is invalid") from exc
            if not effective:
                raise RecognitionConflict("source egress is not authorized")

    def _effective(self, uow, scope, source_type, source_id, nodes, trail, override, closures=None, budget=None, memo=None):
        if closures is None:
            closures = {}
        key = (source_type, source_id)
        visit = ('material', scope.user_id, scope.project_id, source_type, source_id)
        budget = set() if budget is None else budget
        if visit in trail:
            raise RecognitionConflict("source provenance contains a cycle")
        if visit not in budget and len(budget) >= _MAX_NODES:
            raise RecognitionConflict('source provenance is too large')
        budget.add(visit)
        if key not in nodes and len(nodes) + len(trail) >= _MAX_NODES:
            raise RecognitionConflict("source provenance is too large")
        source = _source(uow, scope, source_type, source_id)
        if key in nodes:
            if key not in closures:
                raise RecognitionConflict("source provenance closure is unavailable")
            return set(nodes[key]["effective_purposes"])
        memo = {} if memo is None else memo
        owner = getattr(uow, '_transaction', uow)
        qualification = None if override is None else (*override[:2], frozenset(override[2]))
        memo_key = (owner, visit, source.revision, source.payload.get('_original_incarnation'), qualification)
        cached = memo.get(memo_key)
        if cached is not None:
            complete_closure, cached_nodes, cached_closures = cached
            if complete_closure.intersection(trail) or len(budget | complete_closure) > _MAX_NODES:
                raise RecognitionConflict('source provenance is cyclic or too large')
            if any(item in nodes and nodes[item] != value for item, value in cached_nodes.items()):
                raise RecognitionConflict('source provenance memo conflicted')
            budget.update(complete_closure)
            nodes.update(cached_nodes)
            closures.update(cached_closures)
            return set(nodes[key]['effective_purposes'])
        next_trail = trail + (visit,)
        artifact = _artifact_authority(self, uow, scope, source_type, source, nodes, next_trail, closures, budget, memo)
        if artifact is None:
            parents = _parents(source_type, source.payload)
            inherited = set(_PURPOSES)
            for parent_type, parent_id, parent_revision in parents:
                parent = _source(uow, scope, parent_type, parent_id)
                if parent.revision != parent_revision:
                    raise RecognitionConflict("source provenance revision conflicted")
                parent_effective = self._effective(uow, scope, parent_type, parent_id, nodes, next_trail, None, closures, budget, memo)
                if not parent_effective:
                    inherited = set()
        else:
            parents = artifact[0]
            inherited = artifact[1]
        policy = uow.read(_policy_collection(source_type), source_id)
        if policy is not None and _policy_scope(policy.payload) != scope:
            raise RecognitionConflict("source policy is unavailable in this work scope")
        if source_type == "original_source" and policy is not None and policy.payload.get("incarnation") != source.payload["_original_incarnation"]:
            raise RecognitionConflict("original incarnation conflicted")
        if override is not None and key == override[:2]:
            local = override[2]
        else:
            local = _policy_purposes(policy, source.revision)
        if is_private_project(uow, scope.project_id):
            local = set()
        effective = set(_PURPOSES) if inherited and local else set()
        nodes[key] = {"type": source_type, "id": source_id, "source_revision": source.revision,
                      "policy_revision": policy.revision if policy is not None else 0,
                      "effective_purposes": sorted(effective)}
        if source_type == "original_source":
            nodes[key]["incarnation"] = source.payload["_original_incarnation"]
        if artifact is not None:
            nodes[key]["dependency_revisions"] = artifact[2]
            if len(artifact)>3 and artifact[3]:
                nodes[key]["research_style_sources"] = artifact[3]
        reachable = {key}
        for parent_type, parent_id, _ in parents:
            parent_key = (parent_type, parent_id)
            if parent_key not in closures:
                raise RecognitionConflict("source provenance closure is unavailable")
            reachable.update(closures[parent_key])
        closures[key] = reachable
        metadata = SourceGraph()
        metadata.snapshot({'scope': _scope(scope), 'nodes': [nodes[item] for item in sorted(reachable)],
            'roots': [{'type': source_type, 'id': source_id, 'revision': source.revision}]})
        memo[memo_key] = (set(metadata.nodes), {item: nodes[item] for item in reachable},
            {item: closures[item] for item in reachable})
        return effective


def _parents(source_type: str, payload: Mapping[str, object]) -> tuple[tuple[str, str, int], ...]:
    if source_type == "recognition":
        return _linked(payload, "source_experience_ids", "source_experience_revisions", "experience") + _linked(payload, "source_recognition_ids", "source_recognition_revisions", "recognition")
    provenance = payload.get("provenance")
    if provenance is None:
        return ()
    if not isinstance(provenance, Mapping):
        raise RecognitionConflict("experience provenance is invalid")
    refs = provenance.get("source_refs", ())
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise RecognitionConflict("experience provenance is invalid")
    result = []
    for ref in refs:
        if not isinstance(ref, Mapping) or set(ref).difference({"type", "id", "revision"}):
            raise RecognitionConflict("experience provenance is invalid")
        kind, item = _source_key(ref.get("type"), ref.get("id"))
        revision = ref.get("revision")
        _revision(revision, "provenance source revision")
        result.append((kind, item, revision))
    return tuple(result)


def _artifact_authority(authority, uow, scope, source_type, source, nodes, trail, closures, budget, memo=None):
    """Resolve an auditable retained task result without trusting its refs.

    The task, selected document revision and consumed context packet are all
    exact, local records.  Document edits deliberately do not invalidate a
    retained result: the referenced historical revision remains the evidence.
    """
    if source_type != "experience":
        return None
    payload = source.payload
    try:
        external = read_external_input_dependencies(uow, scope, source, _trail=trail, _budget=budget,
            _sql_validator=validate_external_number)
    except ExternalInputDependencyError as error:
        raise RecognitionConflict('external input evidence is unavailable') from error
    if external is not None:
        from .transaction_records import TransactionRecords
        graph, allowed = SourceGraph(), True
        for frozen in external.snapshots:
            own = WorkScope(frozen['scope']['user_id'], frozen['scope']['project_id'])
            roots = [(root['type'], root['id'], root['revision']) for root in frozen['roots']]
            parsed = _frozen_packet_authority(own, {'source_egress': frozen}, roots, _nodes=budget)
            snapshot = SourceEgressService(TransactionRecords(uow), _memo=memo)._snapshot(own, frozen['roots'], trail, budget)
            if {_closure_identity(node) for node in parsed['nodes']} != {_closure_identity(node) for node in snapshot['nodes']}:
                raise RecognitionConflict('external input source closure changed')
            allowed = allowed and all(node['effective_purposes'] for node in [*parsed['nodes'], *snapshot['nodes']])
            graph.snapshot(snapshot)
        dependencies = {**external.revisions, 'current_source_graph': graph.result()}
        external_input_metadata(external.revisions)
        validate_graph(dependencies['current_source_graph'], scope.user_id, trail=trail, budget=budget)
        return (), set(_PURPOSES) if allowed else set(), dependencies
    try:
        origin = read_experience_origin(uow, source)
    except ExperienceOriginError as exc:
        raise RecognitionConflict(str(exc)) from exc
    if origin is not None:
        marker, original = origin
        own = WorkScope(scope.user_id, marker.payload['source_project_id'])
        from .transaction_records import TransactionRecords
        snapshot = SourceEgressService(TransactionRecords(uow), _memo=memo)._snapshot(own,
            [{'type': 'experience', 'id': original.object_id, 'revision': original.revision}], trail, budget)
        dependencies = {'origin_marker_revision': marker.revision, 'source_project_id': own.project_id,
            'source_experience_id': original.object_id, 'source_revision': original.revision,
            'source_snapshot': snapshot}
        allowed = all(node['effective_purposes'] for node in snapshot['nodes'])
        return (), set(_PURPOSES) if allowed else set(), dependencies
    try:
        filing = read_filed_edit_dependencies(uow, scope, payload, experience_id=source.object_id)
    except DocumentFilingError as error:
        raise RecognitionConflict(str(error)) from error
    if filing is not None:
        from .transaction_records import TransactionRecords
        copied, owner = filing
        snapshot = SourceEgressService(TransactionRecords(uow), _memo=memo)._snapshot(scope,
            [{'type': 'experience', 'id': copied.object_id, 'revision': copied.revision}], trail, budget)
        dependencies = {**owner, 'source_snapshot': snapshot}
        _validate_frozen_filed_edit(scope, dependencies, trail, budget)
        return (), set(_PURPOSES) if all(node['effective_purposes'] for node in snapshot['nodes']) else set(), dependencies
    try:
        draft = read_product_draft_dependencies(uow, scope, payload)
    except ProductDraftDependencyError as error:
        raise RecognitionConflict(str(error)) from error
    if draft is not None:
        from .transaction_records import TransactionRecords
        from .research_packets import authority_stores
        from .research_reads import product_read_sources
        records = TransactionRecords(uow)
        turns, agents = authority_stores(records)
        proofs, read_snapshots = product_read_sources(records, turns, agents, draft.request, scope.project_id)
        graph, allowed = SourceGraph(), True
        for frozen in (*draft.snapshots, *read_snapshots):
            own = WorkScope(frozen['scope']['user_id'], frozen['scope']['project_id'])
            if own.user_id != scope.user_id:
                raise RecognitionConflict('product draft source user changed')
            roots = [(root['type'], root['id'], root['revision']) for root in frozen['roots']]
            parsed = _frozen_packet_authority(own, {'source_egress': frozen}, roots, _nodes=budget)
            snapshot = SourceEgressService(records, _memo=memo)._snapshot(own, frozen['roots'], trail, budget)
            if {_closure_identity(node) for node in parsed['nodes']} != {
                    _closure_identity(node) for node in snapshot['nodes']}:
                raise RecognitionConflict('product draft frozen source closure changed')
            allowed = allowed and all(node['effective_purposes'] for node in [*parsed['nodes'], *snapshot['nodes']])
            graph.snapshot(snapshot)
        for proof in proofs:
            graph.add(proof, root=True)
        dependencies = {**draft.revisions, 'current_source_graph': graph.result()}
        _validate_frozen_product_draft(scope, dependencies, trail, budget)
        return (), set(_PURPOSES) if allowed else set(), dependencies
    provenance = payload.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get("kind") == "workspace_confirmed_document":
        bound = _workspace_document_authority(uow, scope, provenance)
        ref = provenance["source_refs"][0]
        record, _ = _review_document_revision(uow,ref["id"],ref["revision"])
        refs = record.payload.get("source_snapshot",{}).get("source_refs")
        roots = document_roots(uow,scope,refs,optional=not any(r.get("locator","").startswith("workspace://") for r in refs))
        effective = set(_PURPOSES)
        for kind,identity,_ in roots:
            if not authority._effective(uow,scope,kind,identity,nodes,trail,None,closures,budget,memo):
                effective = set()
        return roots,effective,bound[2]
    try:
        artifact = read_artifact_dependencies(uow, scope, payload)
    except ArtifactDependencyError as exc:
        raise RecognitionConflict(str(exc)) from exc
    if artifact is None:
        return None
    from .research_packets import validate_research_packet, research_style_sources
    from .transaction_records import TransactionRecords
    research_roots = validate_research_packet(TransactionRecords(uow),scope,artifact.packet,authority=SourceEgressService(TransactionRecords(uow), _memo=memo),inherit=True)
    style_sources = research_style_sources(TransactionRecords(uow),artifact.packet,authority=SourceEgressService(TransactionRecords(uow), _memo=memo))
    style_private = any(not node["effective_purposes"] for snapshot in style_sources for node in snapshot["nodes"])
    roots = artifact.roots
    frozen = _frozen_packet_authority(scope, artifact.packet, roots)
    if not roots:
        effective=set() if style_private else set(_PURPOSES)
        for kind,identity,_ in research_roots:
            if not authority._effective(uow,scope,kind,identity,nodes,trail,None,closures,budget,memo):
                effective=set()
        return research_roots,effective,dict(artifact.revisions),style_sources
    current = set(_PURPOSES)
    for root_type, root_id, root_revision in roots:
        root = _source(uow, scope, root_type, root_id)
        if root.revision != root_revision:
            raise RecognitionConflict("model artifact source provenance is stale")
        # Share the outer graph collector.  This preserves all parent policy
        # revisions in the result snapshot and applies the global node bound
        # across nested retained artifacts too.
        root_effective = authority._effective(uow, scope, root_type, root_id, nodes, trail, None, closures, budget,memo)
        if not root_effective:
            current = set()
    reachable = set().union(*(closures[(root_type, root_id)] for root_type, root_id, _ in roots))
    if {_closure_identity(node) for node in frozen["nodes"]} != {
            _closure_identity(nodes[key]) for key in reachable
            if nodes[key]["type"] not in ORIGINAL_TYPES or any(n["type"] in ORIGINAL_TYPES for n in frozen["nodes"])}:
        raise RecognitionConflict("model artifact context source closure changed")
    frozen_private = any(not node["effective_purposes"] for node in frozen["nodes"])
    for kind,identity,_ in research_roots:
        if not authority._effective(uow,scope,kind,identity,nodes,trail,None,closures,budget,memo):
            current=set()
    combined=tuple(dict.fromkeys((*roots,*research_roots)))
    return combined, set(_PURPOSES) if current and not frozen_private and not style_private else set(), dict(artifact.revisions),style_sources


def _workspace_document_authority(uow, scope, provenance):
    """Verify a user-confirmed workspace document without inventing Turn lineage."""
    refs = provenance.get("source_refs")
    if (not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or len(refs) != 1
            or not isinstance(refs[0], Mapping) or set(refs[0]) != {"type", "id", "revision"}
            or refs[0].get("type") != "document"):
        raise RecognitionConflict("workspace document provenance is invalid")
    document_id, revision = refs[0]["id"], refs[0]["revision"]
    if not isinstance(document_id, str) or not document_id:
        raise RecognitionConflict("workspace document provenance is invalid")
    _revision(revision, "workspace document revision")
    document = uow.read("documents", document_id)
    if document is None or document.payload.get("project_id") != scope.project_id:
        raise RecognitionConflict("workspace document provenance is unavailable")
    document_revision, markdown = _review_document_revision(uow, document_id, revision)
    snapshot = document_revision.payload.get("source_snapshot")
    source_refs = snapshot.get("source_refs") if isinstance(snapshot, Mapping) else None
    workspace_refs = [ref for ref in source_refs if isinstance(ref, Mapping)
                      and isinstance(ref.get("source_id"), str)
                      and ref.get("locator") == "workspace://" + ref["source_id"]] if isinstance(source_refs, Sequence) else []
    if not workspace_refs:
        # A malformed workspace locator must never fall through to another
        # publication authority. Existing valid workspace bindings stay strict.
        if isinstance(source_refs, Sequence) and any(
            isinstance(ref, Mapping) and isinstance(ref.get("locator"), str)
            and ref["locator"].startswith("workspace://") for ref in source_refs
        ):
            raise RecognitionConflict("workspace document source is unavailable")
        return _legacy_document_authority(uow, scope, document_id, revision,
                                          document_revision, markdown, source_refs)
    if len(workspace_refs) != 1:
        raise RecognitionConflict("workspace document source is unavailable")
    item_id = workspace_refs[0]["source_id"]
    item = uow.read("workspace_items", item_id)
    if (item is None or item.payload.get("project_id") != scope.project_id
            or item.payload.get("status") != "confirmed" or item.payload.get("document_id") != document_id):
        raise RecognitionConflict("workspace document source is unavailable")
    return (), set(_PURPOSES), {
        "workspace_item_revision": item.revision,
        "document_revision": revision,
        "document_revision_record_revision": document_revision.revision,
        "document_markdown_revision": markdown.revision,
    }


def _review_document_revision(uow, document_id, revision):
    """Read exact historical evidence for either human review entry point."""
    key = _document_revision_key(document_id, revision)
    record = uow.read("document_revisions", key)
    markdown = uow.read("document_markdown", key)
    if (record is None or markdown is None
            or record.payload.get("document_id") != document_id
            or type(record.payload.get("revision")) is not int
            or record.payload.get("revision") != revision
            or markdown.payload.get("document_id") != document_id
            or type(markdown.payload.get("revision")) is not int
            or markdown.payload.get("revision") != revision
            or not isinstance(markdown.payload.get("markdown"), str)):
        raise RecognitionConflict("workspace document provenance is unavailable")
    return record, markdown


def _legacy_document_authority(uow, scope, document_id, revision, record, markdown, source_refs):
    """Use the frozen legacy confirmation, not a locator scheme, as authority.

    Existing generated documents retain text/video locators. Multiple segments
    of one source are valid; multiple source identities are ambiguous here.
    Original JSON Source state is outside this historical SQLite boundary.
    """
    if (not isinstance(source_refs, (list, tuple)) or not source_refs
            or any(not isinstance(ref, Mapping)
                   or not isinstance(ref.get("source_id"), str) or not ref["source_id"]
                   or not isinstance(ref.get("locator"), str) or not ref["locator"]
                   or (ref["locator"].startswith("source://")
                       and ref["locator"] != "source://" + ref["source_id"])
                   for ref in source_refs)):
        raise RecognitionConflict("legacy document source is unavailable")
    source_ids = {ref["source_id"] for ref in source_refs}
    if len(source_ids) != 1:
        raise RecognitionConflict("legacy document source is ambiguous")
    source_id = next(iter(source_ids))
    intent_id = "review-" + source_id
    intent = uow.read("workspace_review_intents", intent_id)
    if (intent is None or intent.payload.get("id") != intent_id
            or intent.payload.get("source_id") != source_id
            or intent.payload.get("project_id") != scope.project_id
            or intent.payload.get("state") != "confirmed"
            or intent.payload.get("document_id") != document_id):
        raise RecognitionConflict("legacy document confirmation is unavailable")
    confirmed = intent.payload
    confirmed_revision = confirmed.get("document_revision")
    if (type(confirmed_revision) is not int or confirmed_revision < 1
            or revision < confirmed_revision):
        raise RecognitionConflict("legacy document confirmation revision is unavailable")
    expected_id = confirmed.get("expected_document_id")
    expected_revision = confirmed.get("expected_document_revision")
    if ("expected_document_id" not in confirmed or "expected_document_revision" not in confirmed
            or (expected_id is None and (expected_revision is not None or confirmed_revision != 1))
            or (expected_id is not None and (expected_id != document_id
                or type(expected_revision) is not int or expected_revision < 1
                or confirmed_revision != expected_revision + 1))):
        raise RecognitionConflict("legacy document confirmation revision is unavailable")
    confirmed_record, confirmed_markdown = _review_document_revision(uow, document_id, confirmed_revision)
    frozen = confirmed_record.payload.get("source_snapshot")
    if (not isinstance(frozen, Mapping) or frozen.get("source_refs") != source_refs
            or not isinstance(confirmed.get("confirmed_markdown"), str)
            or not confirmed["confirmed_markdown"].strip()
            or confirmed_markdown.payload["markdown"] != confirmed["confirmed_markdown"]):
        raise RecognitionConflict("legacy document confirmation evidence is unavailable")
    # Document edits and archive revisions retain historical evidence. They do
    # not override egress: the experience still observes private scopes and policy.
    return (), set(_PURPOSES), {
        "legacy_review_id": intent_id, "legacy_review_revision": intent.revision,
        "confirmed_document_revision": confirmed_revision,
        "confirmed_document_record_revision": confirmed_record.revision,
        "confirmed_document_markdown_revision": confirmed_markdown.revision,
        "document_revision": revision, "document_revision_record_revision": record.revision,
        "document_markdown_revision": markdown.revision,
    }


def _frozen_packet_authority(scope, packet: Mapping[str, object], roots, *, _trail=(), _nodes=None):
    """Return a packet's captured ceiling after validating its body-free shape."""
    roots_payload = [{"type": kind, "id": item, "revision": revision} for kind, item, revision in roots]
    frozen = packet.get("source_egress")
    if not roots:
        if frozen is not None:
            raise RecognitionConflict("model artifact context source egress is invalid")
        return {"nodes": []}
    if (not isinstance(frozen, Mapping) or frozen.get("schema_version") != 1
            or frozen.get("scope") != _scope(scope) or frozen.get("roots") != roots_payload):
        raise RecognitionConflict("model artifact context source egress is unavailable")
    raw_nodes = frozen.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)) or not raw_nodes:
        raise RecognitionConflict("model artifact context source egress is invalid")
    seen: set[tuple[str, str]] = set()
    budget = set() if _nodes is None else _nodes
    normalized = []
    for raw in raw_nodes:
        if not isinstance(raw, Mapping) or set(raw).difference({"type", "id", "source_revision", "policy_revision", "effective_purposes", "dependency_revisions", "incarnation", "research_style_sources"}):
            raise RecognitionConflict("model artifact context source egress is invalid")
        try:
            source_type, source_id = _source_key(raw.get("type"), raw.get("id"))
            _revision(raw.get("source_revision"), "frozen source revision")
            _nonnegative_revision(raw.get("policy_revision"), "frozen policy revision")
            purposes = _purposes(raw.get("effective_purposes"))
        except RecognitionError as exc:
            raise RecognitionConflict("model artifact context source egress is invalid") from exc
        if (source_type, source_id) in seen:
            raise RecognitionConflict("model artifact context source egress is invalid")
        key = ('material', scope.user_id, scope.project_id, source_type, source_id)
        if key in _trail or key not in budget and len(budget) >= _MAX_NODES:
            raise RecognitionConflict('model artifact context source egress is cyclic or too large')
        budget.add(key)
        seen.add((source_type, source_id))
        dependency_revisions = raw.get("dependency_revisions")
        if dependency_revisions is not None:
            artifact_fields = {"task_revision", "document_revision", "document_revision_record_revision", "document_markdown_revision", "context_packet_revision"}
            workspace_fields = {"workspace_item_revision", "document_revision", "document_revision_record_revision", "document_markdown_revision"}
            legacy_fields = {"legacy_review_id", "legacy_review_revision", "confirmed_document_revision",
                             "confirmed_document_record_revision", "confirmed_document_markdown_revision",
                             "document_revision", "document_revision_record_revision", "document_markdown_revision"}
            origin_fields = {'origin_marker_revision', 'source_project_id', 'source_experience_id', 'source_revision', 'source_snapshot'}
            if isinstance(dependency_revisions, Mapping) and set(dependency_revisions) == origin_fields:
                if source_type != 'experience':
                    raise RecognitionConflict('model artifact origin dependency is invalid')
                _validate_frozen_origin(scope, dependency_revisions, (*_trail, key), budget)
            elif isinstance(dependency_revisions, Mapping) and set(dependency_revisions) == PRODUCT_DRAFT_FIELDS:
                if source_type != 'experience':
                    raise RecognitionConflict('model artifact product draft dependency is invalid')
                _validate_frozen_product_draft(scope, dependency_revisions, (*_trail, key), budget)
            elif isinstance(dependency_revisions, Mapping) and set(dependency_revisions) == FILED_EDIT_FIELDS:
                if source_type != 'experience':
                    raise RecognitionConflict('filed edit dependency is invalid')
                _validate_frozen_filed_edit(scope, dependency_revisions, (*_trail, key), budget)
            elif isinstance(dependency_revisions, Mapping) and set(dependency_revisions) == EXTERNAL_INPUT_FIELDS:
                if source_type != 'experience':
                    raise RecognitionConflict('external input dependency is invalid')
                try:
                    external_input_metadata({k: v for k, v in dependency_revisions.items() if k != 'current_source_graph'})
                    validate_graph(dependency_revisions['current_source_graph'], scope.user_id, trail=(*_trail, key), budget=budget)
                except ExternalInputDependencyError as error:
                    raise RecognitionConflict('external input dependency is invalid') from error
            elif (not isinstance(dependency_revisions, Mapping)
                    or set(dependency_revisions) not in (artifact_fields, workspace_fields, legacy_fields)):
                raise RecognitionConflict("model artifact context source egress is invalid")
            try:
                for field, value in (() if set(dependency_revisions) in (origin_fields, PRODUCT_DRAFT_FIELDS, FILED_EDIT_FIELDS, EXTERNAL_INPUT_FIELDS)
                                     else dependency_revisions.items()):
                    if field == "legacy_review_id":
                        if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                            raise RecognitionError("frozen legacy review id is invalid")
                    else:
                        _revision(value, f"frozen {field}")
            except RecognitionError as exc:
                raise RecognitionConflict("model artifact context source egress is invalid") from exc
        node = {"type": source_type, "id": source_id, "source_revision": raw["source_revision"],
                "policy_revision": raw["policy_revision"], "effective_purposes": sorted(purposes)}
        if dependency_revisions is not None:
            node["dependency_revisions"] = dict(dependency_revisions)
        if "incarnation" in raw:
            if source_type != "original_source" or not isinstance(raw["incarnation"],str):
                raise RecognitionConflict("model artifact context source egress is invalid")
            node["incarnation"] = raw["incarnation"]
        if "research_style_sources" in raw:
            if not isinstance(raw["research_style_sources"],list):
                raise RecognitionConflict("model artifact style authority is invalid")
            node["research_style_sources"] = raw["research_style_sources"]
        normalized.append(node)
    if not {(kind, item) for kind, item, _ in roots}.issubset(seen):
        raise RecognitionConflict("model artifact context source egress is invalid")
    return {"nodes": normalized}


def _validate_frozen_origin(scope, dependency, trail, budget):
    """Validate only the fixed origin form, preserving the three old contracts."""
    try:
        _revision(dependency['origin_marker_revision'], 'frozen origin marker revision')
        _revision(dependency['source_revision'], 'frozen origin source revision')
        for key in ('source_project_id', 'source_experience_id'):
            value = dependency[key]
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise RecognitionError('frozen origin identity is invalid')
        own = WorkScope(scope.user_id, dependency['source_project_id'])
        snapshot = dependency['source_snapshot']
        expected = {'type': 'experience', 'id': dependency['source_experience_id'], 'revision': dependency['source_revision']}
        if (not isinstance(snapshot, Mapping)
                or set(snapshot) != {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
                or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
                or snapshot['scope'] != _scope(own) or own.project_id == scope.project_id
                or snapshot['roots'] != [expected] or type(snapshot['roots'][0]['revision']) is not int
                or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0):
            raise RecognitionError('frozen origin snapshot is invalid')
        _frozen_packet_authority(own, {'source_egress': snapshot},
            [('experience', dependency['source_experience_id'], dependency['source_revision'])],
            _trail=trail, _nodes=budget)
    except RecognitionError as exc:
        raise RecognitionConflict('model artifact origin dependency is invalid') from exc


def _validate_frozen_product_draft(scope, dependency, trail, budget):
    """Parse fixed metadata; existing owners retain the permission ceilings."""
    owner_metadata({field: value for field, value in dependency.items() if field != 'current_source_graph'})
    validate_graph(dependency['current_source_graph'], scope.user_id, trail=trail, budget=budget)


def _validate_frozen_filed_edit(scope, dependency, trail, budget):
    try:
        validate_filed_edit_owner({key: value for key, value in dependency.items() if key != 'source_snapshot'})
        snapshot = dependency['source_snapshot']
        root = {'type': 'experience', 'id': dependency['copied_experience_id'],
            'revision': dependency['copied_experience_revision']}
        if (not isinstance(snapshot, Mapping)
                or set(snapshot) != {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
                or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
                or snapshot['scope'] != _scope(scope) or snapshot['roots'] != [root]
                or type(snapshot['roots'][0]['revision']) is not int
                or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0):
            raise DocumentFilingError('filed edit snapshot is invalid')
        _frozen_packet_authority(scope, {'source_egress': snapshot},
            [('experience', root['id'], root['revision'])], _trail=trail, _nodes=budget)
    except DocumentFilingError as error:
        raise RecognitionConflict(str(error)) from error



def _document_revision_key(document_id: str, revision: int) -> str:
    return f"{document_id}~r{revision}"




def _linked(payload, ids_key, revisions_key, source_type):
    ids, revisions = payload.get(ids_key), payload.get(revisions_key)
    if not isinstance(ids, Sequence) or isinstance(ids, (str, bytes)) or not isinstance(revisions, Mapping):
        raise RecognitionConflict("recognition source references are invalid")
    result = []
    for item in ids:
        _, item = _source_key(source_type, item)
        revision = revisions.get(item)
        _revision(revision, "recognition source revision")
        result.append((source_type, item, revision))
    return tuple(result)


def _source(uow, scope, source_type, source_id):
    if source_type in ORIGINAL_TYPES:
        return original(uow,scope,source_type,source_id)
    record = uow.read(_TYPES[source_type], source_id)
    if record is None or _scope_of(record.payload) != scope or record.payload.get("state") != "active":
        raise RecognitionConflict("source is unavailable in this work scope")
    return record


def _policy_purposes(policy, source_revision):
    if policy is None:
        return set(_PURPOSES)
    payload = policy.payload
    # Preserve the compatibility field without migrating stored records.
    # Privacy survives source edits; every other legacy value is non-private.
    return set() if payload.get("allowed_purposes") == [] else set(_PURPOSES)


def _refs(refs):
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs:
        raise RecognitionError("source egress references are required")
    result, seen = [], set()
    for raw in refs:
        if not isinstance(raw, Mapping) or set(raw).difference({"type", "id", "revision"}):
            raise RecognitionError("source egress reference is invalid")
        kind, item = _source_key(raw.get("type"), raw.get("id"))
        revision = raw.get("revision")
        _revision(revision, "source reference revision")
        if (kind, item) in seen:
            raise RecognitionError("source egress references contain duplicates")
        seen.add((kind, item)); result.append((kind, item, revision))
    return tuple(result)


def _source_key(source_type, source_id):
    if not isinstance(source_type, str) or source_type not in _TYPES or not isinstance(source_id, str) or _SAFE_ID.fullmatch(source_id) is None:
        raise RecognitionError("source reference is invalid")
    return source_type, source_id


def _policy_collection(source_type):
    return POLICY_COLLECTIONS[source_type]


def _scope(scope):
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _scope_of(payload):
    try:
        return WorkScope(payload["scope"]["user_id"], payload["scope"].get("project_id"))
    except (KeyError, TypeError, RecognitionError) as exc:
        raise RecognitionConflict("stored source scope is invalid") from exc


def _policy_scope(payload):
    return _scope_of({"scope": payload.get("scope")})


def _purposes(value):
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or any(not isinstance(item, str) or item not in _PURPOSES for item in value)
            or len(value) != len(set(value))):
        raise RecognitionError("source egress purposes are invalid")
    return set(value)


def _revision(value, label):
    if type(value) is not int or value < 1:
        raise RecognitionError(f"{label} is invalid")


def _nonnegative_revision(value, label):
    if type(value) is not int or value < 0:
        raise RecognitionError(f"{label} is invalid")


def _parent(reader, namespace, identity):
    parent = chunk_cache_parent(namespace)
    if parent is None:
        row = reader.read('recognitions', identity)
        if row is None or row.payload.get('state') != 'active':
            return None
        raw = row.payload.get('scope', {})
        scope = WorkScope(raw.get('user_id'), raw.get('project_id'))
        if row.payload.get('project_id') != namespace or scope.project_id != namespace:
            return None
        return scope, [{'type': 'recognition', 'id': identity, 'revision': row.revision}], None
    project, kind, identity = parent
    scope = WorkScope('local-user', project)
    if kind == 'source':
        bound = resolve(reader, scope, identity, kind='source')
        roots = ((*bound, original(reader, scope, *bound).revision),)
    else:
        row = reader.read('documents', identity)
        if row is None or row.payload.get('project_id') != project:
            return None
        from backend.recognition.document_filings import filing_experience
        try:
            copied = filing_experience(reader, scope, identity)
        except DocumentFilingError as error:
            raise RecognitionConflict(str(error)) from error
        roots = ((('experience', copied.object_id, copied.revision),) if copied is not None
            else document_roots(reader, scope, row.payload.get('source_refs', []), optional=True))
    if not roots:
        return None  # No surviving L0 cannot fill the current chunk cache.
    return scope, [{'type': kind, 'id': identity, 'revision': revision}
                   for kind, identity, revision in roots], parent


def prepare_sources(tx, scope=None, identities=(), *, project_id=None):
    """Qualify a fixed plan now; the write owner decides when to apply it."""
    if project_id is None and not isinstance(scope, WorkScope):
        raise RecognitionConflict('cache invalidation scope is invalid')
    changed = {(scope.user_id, scope.project_id, kind, identity) for kind, identity in identities}
    reader = TransactionRecords(tx)
    invalidator = VectorCacheInvalidator(vector_cache_path(tx))
    authority = SourceEgressService(reader)
    recognitions, materials, seen_materials = set(), set(), set()
    for namespace, identity in invalidator.parents():
        material = chunk_cache_parent(namespace)
        if material is not None:
            if material in seen_materials:
                continue
            seen_materials.add(material)
        selected = _parent(reader, namespace, identity)
        if selected is None:
            continue  # Already missing/inactive rows belong to daily cleanup.
        own, refs, material = selected
        graph = SourceGraph()
        graph.snapshot(authority.snapshot(own, refs))
        affected = any(node['kind'] == 'material' and (
            node['scope']['project_id'] == project_id if project_id is not None else
            (node['scope']['user_id'], node['scope']['project_id'], node['type'], node['id']) in changed
        ) for node in graph.result()['nodes'])
        if affected:
            if material is None:
                recognitions.add((namespace, identity))
            else:
                materials.add(material)
    recognitions, materials = tuple(sorted(recognitions)), tuple(sorted(materials))
    return lambda: invalidator.targets(recognitions=recognitions, materials=materials)


def invalidate_sources(tx, scope=None, identities=(), *, project_id=None):
    return prepare_sources(tx, scope, identities, project_id=project_id)()


def recognition_service(records, *, cache_invalidation=None, product_draft_validator=validate_product_draft_source):
    """沿同一原领域实例同时注入成果闭包与外部编号的固定校验接点。"""
    return RecognitionService(records, cache_invalidation=cache_invalidation,
        product_draft_validator=product_draft_validator,
        _external_input_validator=validate_external_number)


def _external_originals_committed(reader, snapshot, *, trail, budget):
    graph = SourceGraph()
    graph.snapshot(snapshot)
    expanded = validate_graph(graph.result(), snapshot['scope']['user_id'],
        trail=trail, budget=budget)
    store = source_store(reader)
    for node in expanded['nodes']:
        if node['kind'] != 'material' or node['type'] != 'original_source':
            continue
        if not isinstance(node.get('incarnation'), str) or not re.fullmatch('[0-9a-f]{32}', node['incarnation']):
            raise ExternalInputDependencyError('external_context_evidence_invalid')
        with store.locked('sources', node['id']):
            body = store.read('sources', node['id'])
            project = node['scope']['project_id']
            if (body is None or body.get('id') != node['id'] or body.get('project_id') != project
                    or store.revision('sources', node['id']) != node['source_revision']
                    or store.incarnation('sources', node['id']) != node['incarnation']
                    or not source_is_committed(store, node['id'], project,
                        node['source_revision'], node['incarnation'])):
                raise ExternalInputDependencyError('external_context_evidence_invalid')


def validate_external_number(reader, scope, entry, *, trail, budget):
    from backend.memory_app.original_sources import resolve_turn_material
    from backend.memory_app.transaction_records import TransactionRecords
    from core.document_engine import SQLiteDocumentRepository

    from core.storage_provider.record_lineage import verify_lineage
    from backend.recognition.sql_source_identities import CollectingReader

    proof = entry['proof']
    material, frozen = proof['material'], proof['snapshot']
    if material['project_id'] not in {scope.project_id, 'me'}:
        raise ExternalInputDependencyError('external_context_evidence_invalid')
    own = WorkScope(scope.user_id, material['project_id'])
    if isinstance(reader, TransactionRecords):
        reader = reader._transaction
    collector = CollectingReader(reader)
    resolved, roots = resolve_turn_material(collector, own, material)
    if encoded(roots) != encoded(frozen['roots']):
        raise ExternalInputDependencyError('external_context_evidence_invalid')
    if material['type'] == 'recognition':
        recognition_service(TransactionRecords(collector))._assert_sources_active(collector, own, (), [material['id']],
            recognition_revisions={material['id']: material['revision']})
    state = {'trail': trail, 'budget': budget}
    parsed = _frozen_packet_authority(own, {'source_egress': frozen},
        [(root['type'], root['id'], root['revision']) for root in roots],
        _trail=state['trail'], _nodes=state['budget'])
    authority = SourceEgressService(TransactionRecords(collector), _memo={})
    fresh = authority._snapshot(own, roots, state['trail'], state['budget'])
    authority.validate_snapshot(own, fresh)
    if {_closure_identity(node) for node in parsed['nodes']} != {_closure_identity(node) for node in fresh['nodes']}:
        raise ExternalInputDependencyError('external_context_evidence_invalid')
    if resolved['type'] == 'document':
        documents = SQLiteDocumentRepository(TransactionRecords(collector), namespace_id=source_store(reader).namespace_id)
        if (documents.revision(material['id'], material['revision']) is None
                or documents.markdown(material['id'], revision=material['revision']) is None):
            raise ExternalInputDependencyError('external_context_evidence_invalid')
    _external_originals_committed(reader, frozen, trail=trail, budget=budget)
    selected = set(collector.selected_keys())
    identities = entry['sql_identities']
    if selected != {(identity['collection'], identity['object_id']) for identity in identities}:
        raise ExternalInputDependencyError('external_context_evidence_invalid')
    # Birth metadata is checked after collecting domain reads and through the
    # original reader. Facts/heads/witnesses are never selected domain rows.
    for identity in identities:
        verify_lineage(reader, identity)
    return frozen
