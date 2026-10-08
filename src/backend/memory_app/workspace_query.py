"""Project-visible search and single-consumption question previews."""

from __future__ import annotations

import json
import logging
import re
import time
from functools import partial
from .v2.policies import get, override
from .v2.policies.pipelines import versions_for_turn
from .v2.policies.types import RankInput, ScopeInput
from .v2.policies.scope import prefer_scene_ties, scene_priority
from collections import OrderedDict
from functools import partial
from datetime import datetime, timedelta, timezone
from threading import RLock
from urllib.parse import urlsplit
from uuid import uuid4
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from .model_config import ModelConfigurationError
from .structured_generation import AskOutput
from .kernel.answer_turns import ACTIVE_ANSWER, generate_answer
from .kernel.answer_turns import answer_observation
from backend.shared.llm.model_transport import ModelInterrupted
from .context_adapter import ContextSelectionError, format_recognition_content
from .recall_state import is_recall_excluded
from .v2.usage import recall_weight
from .v2.privacy import egress_allowed, external_egress_allowed, is_private_project
from .v2.insights import insight_view
from .v2.projects import scene_of
from .v2.layers import summary_of
from .v2.request_reads import DocumentReadSet
from .v2.contextual_chunks import select_indexed_contextual_windows
from .v2.contextual_chunk_vectors import chunk_vector_scores
from .v2.ladder import candidate_order, plan_ladder
from .v2.budget import WINDOW_SCAN_CHARS, structured_prompt_overhead, ask_instruction, source_texts as format_source_texts, user_text as _ask_user_text
from backend.recognition import RecognitionError, RecognitionConflict, WorkScope
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.document_visibility import LegacyDocumentVisibility
from core.search_and_recall.evidence_windows import EvidenceWindow, select_evidence_windows
from core.storage_provider.observability import stage, current_observation
from .workspace_contracts import _COLLECTION, _project, _text, _now, _ASK_PART_KEYS, _empty_ask_context
from .workspace_generation import _strip_fence
from backend.shared.llm.message_metadata import _estimate_input_tokens


_ASK_LAYERS = {"L3": "insight", "L2": "summary", "L1": "note", "L0": "source", "inspiration": "inspiration"}


def _timed_evidence_windows(*args, **kwargs):
    with stage("keyword"):
        return select_evidence_windows(*args, **kwargs)


def _record_elapsed(name, started):
    # Instrumentation must never interrupt generation or a delivered delta.
    try:
        observation = current_observation()
        if observation is not None:
            observation.mark_elapsed(name, started)
    except Exception:
        logging.getLogger(__name__).warning("timing_elapsed_observer_failed")


def _ask_context(chosen, source_texts, messages, budget, history="", history_count=0, profile=None, part_context=None):
    """Attribute the exact sent fragments; schema and framing belong to instructions."""
    result = _empty_ask_context()
    parts = {part["key"]: part for part in result["parts"]}
    if any(candidate.get('inspiration') for candidate in chosen):
        part = {'key': 'inspiration', 'count': 0, 'tokens': 0}
        result['parts'].append(part)
        parts['inspiration'] = part
    counted_messages = [dict(messages[-2]), {"role": "user", "content": _ask_user_text([], "")}]
    previous = _estimate_input_tokens(counted_messages)
    parts["instruction"].update(count=1, tokens=previous)
    if profile and profile.get("text"):
        counted_messages.insert(0, dict(messages[0]))
        estimate = _estimate_input_tokens(counted_messages)
        parts["persona"].update(count=profile["count"], tokens=estimate - previous)
        from .v2.budget import text_tokens
        for item in profile['items']:
            result['entries'].append({'layer':'insight', 'id':item['id'], 'title':item.get('title', '已确认画像'),
                                      'tokens':text_tokens(item['content']), 'persona':True,
                                      **({'stale':True} if item.get('stale') is True else {})})
        previous = estimate
    if part_context:
        counted_messages.insert(1 if profile and profile.get('text') else 0,
            {'role':'user', 'content':part_context['text']})
        estimate = _estimate_input_tokens(counted_messages)
        parts['history'].update(count=len(part_context['originals']) + len(part_context['answers']), tokens=estimate-previous)
        previous = estimate
    for index, candidate in enumerate(chosen, 1):
        key = "persona" if candidate.get("persona") else _ASK_LAYERS[candidate["layer"]]
        counted_messages[-1]["content"] = _ask_user_text(source_texts[:index], "")
        estimate = _estimate_input_tokens(counted_messages)
        parts[key]["count"] += 1
        parts[key]["tokens"] += estimate - previous
        layer = _ASK_LAYERS[candidate["layer"]]
        entry = candidate["entry"]
        identity = (entry["id"] if layer in {"insight", "inspiration"} else entry["document_id"] if layer in {"summary", "note"}
                    else entry.get("item_id") or entry["source_id"])
        result["entries"].append({"layer": layer, "id": identity, "title": candidate["title"],
                                  "tokens": estimate - previous, "persona": bool(candidate.get("persona")),
                                  **({'url':candidate['href']} if candidate['kind'] == 'search' else {}),
                                  **({'supplemented': True, 'object_revision': entry['revision']}
                                     if candidate.get('supplemented') else {})})
        previous = estimate
    counted_messages[-1]["content"] = _ask_user_text(source_texts, "", history)
    estimate = _estimate_input_tokens(counted_messages)
    parts["history"].update(count=parts['history']['count'] + history_count,
                            tokens=parts['history']['tokens'] + estimate - previous)
    previous = estimate
    original_estimate = _estimate_input_tokens(messages)
    parts["question"].update(count=1, tokens=original_estimate - previous)
    if isinstance(budget, dict):
        result.update(window=budget["window"], reserve=budget["reserve"])
        # The gateway measured the actual structured/schema-enriched messages.
        # Attribute that additional framing once, instead of inventing another
        # estimate per fragment or counting the user's profile twice.
        parts["instruction"]["tokens"] += budget["estimated_input_tokens"] - original_estimate
    return result


def _recognition_evidence_basis(candidate: dict) -> tuple | None:
    """Compare exact derivations, retaining every evidence and policy revision.

    Only the selected recognition's own ID is irrelevant to budget use. Its
    sources, body, conditions, lifecycle and complete frozen closure must match.
    This is a read-time comparison, never a stored merge or a truth judgement.
    """
    if candidate["kind"] != "recognition":
        return None
    entry, snapshot = candidate["entry"], candidate["snapshot"]
    own, ancestors = None, []
    for node in snapshot["nodes"]:
        if node["type"] == "recognition" and node["id"] == entry["id"]:
            own = {key: value for key, value in node.items() if key != "id"}
        else:
            ancestors.append(node)
    basis = (entry["project_id"], entry["content"], entry["conditions"], entry["source_refs"],
            entry["revision"], entry["status"], entry["authorized"], entry["evidence_eligible"],
            entry.get("source_evidence"), entry.get("source_evidence_complete"),
            snapshot["scope"], own, ancestors)
    return (*basis, candidate['validity']) if candidate.get('time_scope') else basis


def _documents_forgotten(records, document_ids):
    return bool(document_ids) and all(recall_weight(records, "document", identity) == 0 for identity in document_ids)


class WorkspaceQuery:
    def __init__(self, records, documents, source_store, models, service, *, read_only=False, source_index_records=None):
        self.records = records
        self.documents = documents
        self.source_store = source_store
        self.models = models
        self.service = service
        self.ask_previews = OrderedDict()
        self.ask_preview_lock = RLock()
        from .v2.retrieval_index import RetrievalIndex
        self.retrieval_index = RetrievalIndex(self, read_only=read_only, source_records=source_index_records)
        self.retrieval_index.bootstrap()

    def local_reader(self):
        """保持原查询独立，复用相同资料入口建立本机只读查询。"""
        return WorkspaceQuery(self.records, self.documents, self.source_store,
            self.models, self.service, read_only=True,
            source_index_records=self.retrieval_index.source_records)

    def query_entries(self, project_id: str, *, selected: list[dict] | None = None, prepared=None) -> list[dict]:
        """Use current shared Documents and published Sources for both search and ask."""
        selected_documents = None if selected is None else {
            entry["id"] for entry in selected if entry["kind"] == "document"}
        selected_sources = None if selected is None else {
            entry["id"] for entry in selected if entry["kind"] == "source"}
        visibility = LegacyDocumentVisibility.from_repository(
            self.documents, project_id=project_id, document_ids=None if selected_sources else selected_documents,
            metadata_only=True)
        pending_sources = visibility.pending_sources
        item_rows = (prepared.items.values() if prepared is not None else
                    self.records.list_matching(_COLLECTION, project_id=project_id, status="confirmed")
                     if selected_documents is None else
                     (row for document_id in selected_documents for row in self.records.list_matching(
                         _COLLECTION, project_id=project_id, status="confirmed", document_id=document_id)))
        confirmed = {str(row.payload["document_id"]): row for row in item_rows if row.payload.get("document_id")}
        # A selected source still needs archive/link visibility across documents;
        # document-only revalidation reads only its frozen IDs and current bodies.
        all_documents = (tuple(prepared.documents.values()) if prepared is not None else
                         tuple(DocumentReadSet.load(self.records, self.documents, project_id,
                               metadata_only=True).documents.values()) if selected_sources else
                         self.documents.list(include_archived=True)
                         if selected_documents is None or selected_sources else
                         tuple(document for identity in selected_documents
                               if (document := self.documents.read(identity)) is not None))
        entries = []
        linked_sources = set()
        archived_sources = {
            str(ref["source_id"])
            for document in all_documents
            if document.get("project_id") == project_id and document.get("status") == "archived"
            for ref in document.get("source_refs", [])
            if isinstance(ref, dict) and isinstance(ref.get("source_id"), str)
        }
        for document in all_documents:
            if (document.get("project_id") != project_id or document.get("status") == "archived"
                    or not visibility.allows(document)):
                continue
            document_id = str(document["id"])
            for ref in document.get("source_refs", []):
                if isinstance(ref, dict) and isinstance(ref.get("source_id"), str):
                    linked_sources.add(ref["source_id"])
            if selected_documents is not None and document_id not in selected_documents:
                continue
            markdown = prepared.markdown.get(document_id) if prepared is not None else self.documents.markdown(document_id)
            if markdown is None:
                continue
            item_row = confirmed.get(document_id)
            original = str(item_row.payload.get("source_text") or "") if item_row else ""
            entries.append({
                "kind": "document", "id": document_id, "document_id": document_id,
                "item_id": item_row.object_id if item_row else None,
                "citation_id": item_row.object_id if item_row else document_id,
                "title": str(document.get("title") or document_id),
                "content": markdown + ("\n原文摘录：\n" + original if original else ""),
                "href": f"#view=rebuild-library-overview&project_id={project_id}&item_id={document_id}&action=inspect",
                "revision": int(document["revision"]),
                "item_revision": item_row.revision if item_row else None,
            })
        sources = (self.source_store.list("sources") if selected_sources is None else
                   (source for identity in selected_sources
                    if (source := self.source_store.read("sources", identity)) is not None))
        for source in sources:
            source_id = str(source.get("id") or "")
            if (not source_id or source.get("project_id", "default") != project_id
                    or source_id in pending_sources
                    or (source_id in archived_sources and source_id not in linked_sources)
                    or (source_id in linked_sources and source.get("identity_method") == "workspace_confirmation")):
                continue
            metadata = source.get("metadata")
            content = (metadata.get("content_snapshot") or metadata.get("content") or "") if isinstance(metadata, dict) else ""
            if not isinstance(content, str) or not content.strip():
                continue
            entries.append({
                "kind": "source", "id": source_id, "source_id": source_id,
                "title": str(source.get("title") or source_id), "content": content,
                "href": f"#view=rebuild-library-overview&project_id={project_id}&item_id={source_id}&action=inspect",
                "revision": self.source_store.revision("sources", source_id),
            })
        return entries

    def search(self, project_id: str = "default", q: str = ""):
        project_id = _project(project_id)
        query = q.strip().casefold()
        if not query or len(query) > 200:
            raise HTTPException(400, "invalid_query")
        items = []
        for entry in self.query_entries(project_id):
            if query not in (entry["title"] + " " + entry["content"]).casefold():
                continue
            selection = _timed_evidence_windows(entry["content"], q, title=entry["title"],
                                                max_chars=280, max_windows=1)
            windows = [{"start": window.start, "end": window.end} for window in selection.windows]
            items.append({key: value for key, value in entry.items() if key != "content"} | {
                "snippet": selection.excerpt, "windows": windows,
                "start": windows[0]["start"] if windows else 0,
                "end": windows[0]["end"] if windows else 0,
                "match_in": selection.match_in,
                "coordinate_space": "workspace_query_content_v1",
            })
            if len(items) >= 50:
                break
        return {"items": items}

    def freeze_answer_privacy(self, project, *, local_only):
        from .v2.privacy import freeze_turn_materials
        _, privacy = freeze_turn_materials(self.records, self.models, project, (),
            authority=SourceEgressService(self.records), local_only=local_only)
        return privacy

    def validate_answer_request(self, models, request):
        from .v2.turn_requests import validate_frozen_inputs
        validate_frozen_inputs(self.records, models, request, query=self)

    def ask_target(self) -> dict:
        public = getattr(self.models, "public", None)
        settings = public() if callable(public) else {}
        generation = settings.get("generation", {}) if isinstance(settings, dict) else {}
        generation = generation if isinstance(generation, dict) else {}
        mode = settings.get("generation_mode", {}) if isinstance(settings, dict) else {}
        mode = mode if isinstance(mode, dict) else {}
        base_url = str(generation.get("base_url") or "")
        try:
            host = urlsplit(base_url).hostname
        except ValueError:
            host = None
        # An unrecognised nonempty endpoint is never treated as safe local use.
        location = "local" if not base_url or host in {"localhost", "127.0.0.1", "::1"} else "remote"
        return {
            "provider": generation.get("provider") or "local",
            "base_url": base_url,
            "model": generation.get("model") or "",
            "revision": generation.get("revision") or 0,
            "mode_revision": mode.get("revision") or 0,
            "execution_location": location,
            **({"subscription_binding": generation["subscription_binding"]} if generation.get("subscription_binding") else {}),
        }

    def collect_candidates(self, project_id: str, question: str, *, scene=None, situation=None, local_only=False, rank_reference=None, external_client=None) -> dict:
        if type(local_only) is not bool:
            raise ValueError('local_only_must_be_bool')
        if local_only and not self.retrieval_index.read_only:
            raise ValueError('local_coverage_requires_readonly_index')
        def validate_external():
            if external_client is not None and not external_egress_allowed(self.records, project_id, external_client):
                raise RecognitionError('external_agent_remote_blocked')
        validate_external()
        selections = versions_for_turn('project.answer' if external_client is None else 'external.context')
        entrypoint = partial(self._collect_candidates, local_only=local_only,
            rank_reference=rank_reference, external_client=external_client)
        with override(**selections), stage("build_entries"):
            result = get('retrieve')(entrypoint, project_id, question, scene=scene,
                                     **({'situation': situation} if situation is not None else {}))
        validate_external()
        return {**result, 'policy_versions': selections}

    def collect_lower_candidates(self, project_id: str, question: str, *, scene=None,
                                 policy_versions=None, time_scope=None, time_documents=None,
                                 time_candidates=(), rank_reference=None) -> dict:
        """Reuse the real collector, limiting work to the two unfinished layers."""
        selections = policy_versions or versions_for_turn('project.answer')
        with override(**selections), stage("build_entries"):
            result = self._collect_candidates(project_id, question, scene=scene, only_layers=('L1', 'L0'),
                rank_reference=rank_reference)
            if time_scope:
                scope = WorkScope('local-user', project_id)
                if any(row['layer'] != 'L3' or row['kind'] != 'recognition' or row['scope'] != scope
                       or not get('scope')(ScopeInput(scene, row['scene'])) for row in time_candidates):
                    raise RecognitionError('drilldown_time_basis_outside_scope')
                # These frozen owners qualify document history only. They are
                # validated by ID, never recollected or included in RRF votes.
                self.validate_ask_plan({'project_id': project_id, 'scope': scope,
                    'target': self.ask_target(), 'chosen': time_candidates})
                qualified = self.filter_time_candidates([*time_candidates, *result['candidates']],
                    time_scope, time_documents or {})
                result['candidates'] = [row for row in qualified if row['layer'] in {'L1', 'L0'}]
                result.update(time_scope=time_scope, time_documents=time_documents or {})
        return {**result, 'policy_versions': selections}

    def filter_time_candidates(self, candidates, time_scope, time_documents):
        """Use the same frozen date meaning for variants and authorized bookshelf evidence."""
        if time_scope is None:
            return candidates
        from .v2.insight_validity import read_validity, COLLECTION
        prepared = []
        for row in candidates:
            if row['kind'] == 'recognition':
                marker = self.records.read(COLLECTION, row['id'])
                row = {**row, 'validity': read_validity(self.records, row['id']) or {},
                    'validity_revision': marker.revision if marker else 0,
                    'time_match_score': getattr(get('rank'), 'rescore', lambda score, candidate: score)(
                        _timed_evidence_windows(row['entry']['content'], time_scope['query'], max_chars=1800).score, row)}
            elif row.get('sort_time') is None:
                record = self.records.read('documents', row['entry']['id']) if row['kind'] == 'document' else None
                if record:
                    row = {**row, 'sort_time': record.payload.get('created_at')}
            prepared.append(row)
        return get('retrieve')(None, prepared, time_scope, time_documents, operation='time_candidates')

    def _collect_candidates(self, project_id: str, question: str, *, scene=None, method_query=None, situation=None, time_query=None, local_only=False, rank_reference=None, only_layers=None, external_client=None) -> dict:
        if local_only and not self.retrieval_index.read_only:
            raise ValueError('local_coverage_requires_readonly_index')
        scope = WorkScope("local-user", project_id)
        rank_policy = get('rank')
        if callable(getattr(rank_policy, 'decorate', None)):
            from .v2.insight_validity import now
            rank_reference = rank_reference or now()
        else:
            rank_reference = None
        time_scope = None
        if time_query is not None:
            from .v2.insight_validity import now
            time_scope = get('retrieve')(None, time_query, rank_reference or now(), operation='time')
            if time_scope:
                question = time_scope['query']
        authority = SourceEgressService(self.records)
        candidates, methods = [], []
        excluded = []
        entries, prepared, indexed_documents, indexed_sources, indexed_originals = self.retrieval_index.indexed_entries(project_id)
        from .v2.outcomes import hidden_outcome_ids
        hidden = hidden_outcome_ids(self.records, project_id)
        entries = [entry for entry in entries if entry['kind'] != 'document' or entry['id'] not in hidden]
        if not local_only and is_private_project(self.records, project_id):
            entries = []
        source_documents = {}
        documents_by_id = {identity: document for identity, document in prepared.documents.items()
                           if document.get('status') != 'archived'}
        for document in documents_by_id.values():
            if document.get("project_id") != project_id:
                continue
            assignment = scene_of(self.records, "document", document["id"])
            assigned = assignment["scene"] if assignment and assignment.get("project_id") == project_id else None
            for ref in document.get("source_refs", []):
                if isinstance(ref, dict) and ref.get("source_id"):
                    source_documents.setdefault(ref["source_id"], []).append((document["id"], assigned))
        for entry in entries:
            try:
                snapshot = self.original_snapshot(scope,entry,authority)
                if snapshot is not None and not local_only:
                    authority.require(snapshot,"generation")
            except RecognitionError:
                excluded.append({"type":entry["kind"],"id":entry["id"],"revision":entry["revision"],
                                 "reason":"source_unavailable" if local_only else "private"})
                continue
            assignment = scene_of(self.records, "document", entry["id"]) if entry["kind"] == "document" else None
            if assignment is None and entry.get("item_id"):
                assignment = scene_of(self.records, "item", entry["item_id"])
            linked = sorted(source_documents.get(entry["id"], [])) if entry["kind"] == "source" else []
            if scene is not None:
                linked = [(identity, assigned) for identity, assigned in linked if get('scope')(ScopeInput(scene, assigned))]
                # A source linked only to sibling documents is not project-level material.
                if entry["kind"] == "source" and source_documents.get(entry["id"]) and not linked:
                    continue
                linked.sort(key=lambda value: (scene_priority(ScopeInput(scene, value[1]), get('scope')), value[0]))
            recall_documents = [entry["id"]] if entry["kind"] == "document" else [identity for identity, _ in linked]
            if _documents_forgotten(self.records, recall_documents):
                continue
            assigned_scene = (assignment["scene"] if assignment and assignment.get("project_id") == project_id
                              else linked[0][1] if linked else None)
            if not get('scope')(ScopeInput(scene, assigned_scene)):
                continue
            spans = []
            sort_times = {}
            context_summary = ""
            if entry["kind"] == "document":
                document = documents_by_id[entry["id"]]
                sort_times.update(L1=document.get("updated_at") or document.get("created_at"),
                                  L2=document.get("updated_at") or document.get("created_at"))
                projection = indexed_documents[entry['id']]
                context_summary = projection['summary']
                spans.extend((span['layer'], span['start'], span['end'], 'document_markdown_v1',
                              tuple(EvidenceWindow(**value) for value in span['chunks']))
                             for span in projection['spans'])
                if entry.get("item_id"):
                    item = prepared.items.get(entry["item_id"])
                    original = indexed_originals.get(entry['item_id'])
                    sort_times["L0"] = item.payload.get("created_at") if item else None
                    if original and original['length']:
                        spans.append(('L0', 0, original['length'], 'workspace_source_text_v1',
                                      tuple(EvidenceWindow(**value) for value in original['chunks'])))
            else:
                source = next((value for value in indexed_sources if value['source_id'] == entry['id']), None)
                sort_times["L0"] = source.get("created_at") if source else None
                spans.append(('L0', 0, source['length'], 'source_content_v1',
                              tuple(EvidenceWindow(**value) for value in source['chunks'])))
            layers = {}
            chunk_inventory = [(layer, slot, chunks) for slot, (layer, _, _, _, chunks) in enumerate(spans)
                               if layer != 'L2']
            for span_slot, (layer, start, end, coordinates, chunks) in enumerate(spans):
                if only_layers is not None and layer not in only_layers:
                    continue
                if layer == "L2":
                    selection = _timed_evidence_windows(context_summary, question,
                        title=entry["title"], max_chars=WINDOW_SCAN_CHARS)
                else:
                    vectors = {}
                    with stage("vector"):
                        if not local_only and external_client is None:
                            vectors = chunk_vector_scores(self, project_id, entry, scope, snapshot, chunks,
                                layer=layer, span_start=start, span_slot=span_slot,
                                summary=context_summary, question=question, chunk_inventory=chunk_inventory)
                    # An optional provider can fail, but changed privacy never
                    # becomes permission to return the old lexical material.
                    if not local_only and is_private_project(self.records, project_id):
                        layers.clear()
                        break
                    if snapshot is not None:
                        try:
                            authority.validate_snapshot(scope, snapshot)
                            if not local_only:
                                authority.require(snapshot, "generation")
                        except RecognitionError:
                            layers.clear()
                            break
                    with stage("keyword"):
                        selection = select_indexed_contextual_windows(end-start, question,
                            title=entry["title"], summary=context_summary, chunks=chunks,
                            vector_scores=vectors, max_chars=WINDOW_SCAN_CHARS)
                if selection.score:
                    windows = tuple(EvidenceWindow(w.start+start, w.end+start, w.text) for w in selection.windows)
                    layers.setdefault(layer, []).append((selection, windows, coordinates))
            if layers:
                current, hydrated = self.retrieval_index.hydrate(project_id, entry)
                if current is None or any(current.get(key) != entry.get(key) for key in
                        ('revision', 'item_revision', 'title', 'item_id')):
                    self.retrieval_index.schedule(entry['kind'], entry['id'])
                    continue
                def current_text(coordinates):
                    if coordinates == 'document_markdown_v1':
                        return hydrated.markdown[entry['id']]
                    if coordinates == 'workspace_source_text_v1':
                        item = hydrated.items.get(entry['item_id'])
                        return str(item.payload.get('source_text') or '') if item else ''
                    return current['content']
                if any(current_text(coordinates)[window.start:window.end] != window.text
                       for selections in layers.values() for _, windows, coordinates in selections for window in windows):
                    self.retrieval_index.schedule(entry['kind'], entry['id'])
                    if entry.get('item_id'):
                        self.retrieval_index.schedule('original', entry['item_id'])
                    continue
                entry = current
            for layer, selections in layers.items():
                # Keep a single bounded candidate per object/layer, preserving exact slices.
                windows = []
                for selection, pieces, coordinates in sorted(selections, key=lambda value: -value[0].score):
                    for window in pieces:
                        windows.append(window)
                windows.sort(key=lambda w: w.start)
                if not windows:
                    continue
                selection = max(selections, key=lambda value: value[0].score)[0]
                usage_weight = recall_weight(self.records, 'document', entry['id']) if entry['kind'] == 'document' else 1.0
                candidates.append({"score": selection.score * usage_weight, "kind": entry["kind"],
                                   "id": entry.get("citation_id", entry["id"]),
                                   "title": entry["title"], "excerpt": "\n…\n".join(w.text for w in windows),
                                   "windows": tuple(windows), "match_in": selection.match_in,
                                   "href": entry["href"], "entry": entry, "snapshot": snapshot,
                                   "layer": layer, "scope": scope, "scene": assigned_scene,
                                   "sort_time": sort_times.get(layer),
                                   "document_id": entry.get("document_id"),
                                   "document_ids": [identity for identity, _ in linked], "coordinate_space": coordinates})
        recognition_entries, time_documents = [], {}
        for own_scope in (() if project_id == "me" or (only_layers is not None and 'L3' not in only_layers) else (scope,)):
            if local_only or not is_private_project(self.records, own_scope.project_id):
                recognition_entries.extend((entry, own_scope) for entry in self.service.retrieval_entries(scope=own_scope))
        from .v2.links import InsightLinks
        anchors = {entry['id'] for entry, _ in recognition_entries
                   if _timed_evidence_windows(entry['content'], question, max_chars=1800).score}
        expansion_ids = (InsightLinks(self.records, self.service).expansion_ids(project_id, anchors)
                         if only_layers is None or 'L3' in only_layers else set())
        for entry, own_scope in recognition_entries:
            scope = own_scope
            if is_recall_excluded(self.records, scope, entry["id"]):
                continue
            view = insight_view(self.records, scope, entry["id"], service=self.service)
            if view is None or view["state"] != "active" or not get('scope')(ScopeInput(scene, view['scene'])):
                continue
            if time_scope:
                for identity in view['document_ids']:
                    time_documents.setdefault(identity, []).append(entry['id'])
            selection = _timed_evidence_windows(entry["content"], question, max_chars=1800)
            from .v2.policies.retrieve import condition_score
            method_score = (condition_score(entry['conditions'], method_query)
                            if method_query and ((local_only or egress_allowed(self.records, self.models, project_id, 'generation'))
                                if external_client is None else external_egress_allowed(self.records, project_id, external_client)) else 0)
            if not selection.score and entry["id"] not in expansion_ids and not method_score:
                continue
            try:
                snapshot = authority.snapshot(scope, [{"type": "recognition", "id": entry["id"],
                                                       "revision": entry["revision"]}])
                if not local_only:
                    authority.require(snapshot, "generation")
            except RecognitionError:
                if local_only:
                    excluded.append({'type': 'recognition', 'id': entry['id'],
                                     'revision': entry['revision'], 'reason': 'source_unavailable'})
                continue
            # A published recognition is one statement with its conditions.
            # Trimming even a complete match can remove negation or exceptions.
            try:
                excerpt = format_recognition_content(entry)
            except ContextSelectionError:
                if len(excluded) < 20:
                    excluded.append({"type": "recognition", "id": entry["id"],
                                     "revision": entry["revision"],
                                     "reason": "recognition_source_evidence_incomplete"})
                continue
            record = self.records.read("recognitions", entry["id"])
            rank_input = RankInput(selection.score, recall_weight(self.records, 'insight', entry['id']),
                                   conditions=entry['conditions'], reference=rank_reference)
            candidate = {"score": rank_policy(rank_input), "kind": "recognition", "id": entry["id"],
                               "sort_time": (record.payload.get("updated_at") or record.payload.get("created_at")) if record else None,
                               "title": "已发布认识", "excerpt": excerpt,
                               "windows": (EvidenceWindow(0, len(entry["content"]), entry["content"]),),
                               "match_in": selection.match_in,
                               "href": f"#view=recognition&project_id={scope.project_id}",
                               "entry": entry, "snapshot": snapshot,
                               "scope": scope, "layer": "L3", "scene": view["scene"],
                               "document_ids": view["document_ids"], "document_id": None,
                               "persona": scope.project_id == "me" and project_id != "me",
                               "expansion_only": not bool(selection.score)}
            if callable(getattr(rank_policy, 'decorate', None)):
                candidate = rank_policy.decorate(candidate, rank_input)
            if time_scope:
                from .v2.insight_validity import read_validity, COLLECTION
                candidate.update(validity=read_validity(self.records, entry['id']) or {},
                    time_match_score=selection.score,
                    validity_revision=(marker.revision if (marker := self.records.read(COLLECTION, entry['id'])) else 0))
            if selection.score or entry['id'] in expansion_ids:
                candidates.append(candidate)
            if method_score:
                methods.append({**candidate, 'score': getattr(rank_policy, 'rescore', lambda score, candidate: score)(method_score, candidate), 'expansion_only': False})
        if time_scope:
            candidates = self.filter_time_candidates(candidates, time_scope, time_documents)
            methods = self.filter_time_candidates(methods, time_scope, time_documents)
        for candidate in candidates:
            priority = scene_priority(ScopeInput(scene, candidate["scene"]), get('scope'))
            if priority:
                candidate["scope_priority"] = priority
        candidates.sort(key=lambda value: (-round(value["score"], 3), value["kind"],
                                           candidate_order(value)[1], value["entry"]["id"]))
        candidates = prefer_scene_ties(candidates, score_key=lambda value: -round(value["score"], 3))
        chosen, bases = [], []
        for candidate in candidates:
            basis = _recognition_evidence_basis(candidate)
            duplicate = next((previous for previous, previous_basis in zip(chosen, bases)
                              if basis is not None and basis == previous_basis), None)
            if duplicate is not None:
                if len(excluded) < 20:
                    excluded.append({"type": "recognition", "id": candidate["id"],
                                     "revision": candidate["entry"]["revision"],
                                     "reason": "duplicate_recognition_evidence",
                                     "duplicate_of": {"id": duplicate["id"],
                                                      "revision": duplicate["entry"]["revision"]}})
                continue
            chosen.append(candidate)
            bases.append(basis)
        result = {"candidates": chosen, "excluded_sources": excluded}
        if rank_reference is not None:
            result['rank_reference'] = rank_reference
        from .v2.inspirations import collect_inspirations
        inspirations = collect_inspirations(self.records, project_id, question, scene, instruction=situation)
        if inspirations:
            result['inspiration_candidates'] = inspirations
        if time_scope:
            result.update(time_scope=time_scope, time_documents=time_documents)
        if method_query is not None:
            from .v2.context_feedback import struck_methods
            struck = struck_methods(self.records, project_id)
            methods.sort(key=lambda row: (row['id'] in struck, -row['score'],
                                         scene_priority(ScopeInput(scene, row['scene']), get('scope')), row['id']))
            result['method_candidates'] = [{**row, 'previously_struck': row['id'] in struck} for row in methods]
        return result

    def local_coverage(self, project_id: str, question: str, *, scene=None) -> dict:
        """检查本机原证据，不调用模型、缓存或索引修复。"""
        if not self.retrieval_index.read_only:
            raise ValueError('local_coverage_requires_readonly_index')

        def basis(rows):
            # 强度会随读取时间衰减；比较事实和资格，不比较瞬时分数与新采样的参考时钟。
            result = {}
            for row in rows:
                value = {key: item for key, item in row.items() if key not in {'score', 'time_scope'}}
                if row.get('time_scope'):
                    value['time_scope'] = {key: item for key, item in row['time_scope'].items() if key != 'reference'}
                result[(row['kind'], row['id'], row['layer'])] = value
            return result

        def unresolved(collected):
            return (self.retrieval_index.unavailable_for(project_id)
                    or any(row['reason'] in {'source_unavailable', 'recognition_source_evidence_incomplete'}
                           for row in collected['excluded_sources']))

        try:
            # 清单包含缺失的来源记录；索引为空不能证明项目没有证据。
            with self.retrieval_index.readonly_inventory():
                collected = self.collect_candidates(project_id, question, scene=scene, local_only=True)
                if unresolved(collected):
                    raise RecognitionConflict('local_coverage_unavailable')
            from .v2.links import InsightLinks
            from .v2.multi_query import fuse_candidates
            links = InsightLinks(self.records, self.service)
            with override(**collected['policy_versions']), stage('ladder'):
                planned = plan_ladder(collected['candidates'], question,
                    neighbors=lambda identity: links.neighbors(project_id, identity),
                    methods=fuse_candidates([collected.get('method_candidates', [])]), situation=question)
            plan = {**planned, 'project_id': project_id, 'scope': WorkScope('local-user', project_id)}

            def validate_current():
                with self.retrieval_index.readonly_inventory():
                    self._validate_ask_sources(plan, local_only=True)
                    with override(**collected['policy_versions']):
                        current = self.collect_candidates(project_id, question, scene=scene, local_only=True)
                    if (unresolved(current)
                            or basis(current['candidates']) != basis(collected['candidates'])
                            or basis(current.get('method_candidates', [])) != basis(collected.get('method_candidates', []))
                            or current['excluded_sources'] != collected['excluded_sources']):
                        raise RecognitionConflict('local_coverage_changed')

            validate_current()
            return {**planned, 'status': 'known', 'coverage': planned['trace'][-1]['coverage'],
                    'stopped': planned['trace'][-1]['stopped'], 'policy_versions': collected['policy_versions'],
                    'validate_current': validate_current}
        except (RecognitionError, ValueError, TypeError, KeyError, OSError):
            return {'status': 'unknown', 'coverage': None, 'stopped': None, 'chosen': [], 'trace': []}

    def ask_budget(self):
        target = self.ask_target()
        reader = getattr(self.models, "generation_budget_limits", None)
        limits = reader(expected_revision=target["revision"], max_tokens=512 if target["execution_location"] == "local" else 7000) if callable(reader) else None
        valid = (isinstance(limits, dict) and type(limits.get("window")) is int and type(limits.get("reserve")) is int
                 and limits["window"] > 0 and 0 <= limits["reserve"])
        budget = max(0, min(6000, (limits["window"] - limits["reserve"]) // 2)) if valid else 4000
        overhead = structured_prompt_overhead() if any(callable(getattr(self.models, name, None))
            for name in ("complete_structured", "complete_stream")) else 0
        return target, budget, overhead

    def prepare_ask(self, project_id: str, question: str, *, scene=None, retrieval_question=None, history="", collected=None, coverage_questions=None, situation=None, part_context=None, evidence_limit=None, profile_limit=None) -> dict:
        selections = (collected or {}).get('policy_versions') or versions_for_turn('project.answer')
        with override(**selections):
            plan = self._prepare_ask(project_id, question, scene=scene, retrieval_question=retrieval_question,
                history=history, collected=collected, coverage_questions=coverage_questions, situation=situation,
                part_context=part_context, evidence_limit=evidence_limit, profile_limit=profile_limit)
        return {**plan, 'policy_versions': selections}

    def prepare_drilldown(self, project_id: str, question: str, *, scene=None, retrieval_question=None,
                          history="", collected=None, coverage_questions=None, situation=None, part_context=None) -> dict:
        """Freeze the ordinary plan once, pausing selection after its upper layers."""
        selections = (collected or {}).get('policy_versions') or versions_for_turn('project.answer')
        with override(**selections):
            plan = self._prepare_ask(project_id, question, scene=scene, retrieval_question=retrieval_question,
                history=history, collected=collected, coverage_questions=coverage_questions,
                situation=situation, part_context=part_context, _pause_after='L2')
        return {**plan, 'policy_versions': selections}

    def gap_materials(self, plan, models, *, local_only):
        """Rebuild the selected upper text through its original domain owners."""
        from .v2.privacy import freeze_turn_materials
        from .v2.turn_requests import validate_frozen_inputs
        from .model_config import ModelConfigurationError
        self.validate_ask_plan(plan)
        descriptors = []
        for candidate in plan['chosen']:
            entry = candidate['entry']
            kind = candidate['kind']
            if (candidate['layer'], kind) not in {('L2', 'document'), ('L3', 'recognition')}:
                raise RecognitionConflict('gap_requires_upper_materials')
            descriptors.append({'type': kind, 'id': entry['id'], 'revision': entry['revision'],
                'project_id': candidate.get('scope', plan['scope']).project_id})
        if not descriptors:
            raise ModelConfigurationError('gap_materials_unavailable')
        allowed, privacy = freeze_turn_materials(self.records, models, plan['project_id'], descriptors,
            authority=SourceEgressService(self.records), local_only=local_only)
        if len(allowed) != len(descriptors) or (not local_only and not privacy['allow_remote']):
            raise ModelConfigurationError('gap_egress_unavailable')
        texts, selection = [], []
        for candidate, descriptor in zip(plan['chosen'], descriptors):
            entry = candidate['entry']
            if candidate['layer'] == 'L3':
                current = self.service.get_recognition(scope=candidate.get('scope', plan['scope']),
                    recognition_id=descriptor['id'])
                if current is None or not current.authorized:
                    raise RecognitionConflict('gap_recognition_unavailable')
                projection = current.retrieval_projection()
                if any(entry.get(key) != value for key, value in projection.items()):
                    raise RecognitionConflict('gap_recognition_changed')
                text = format_recognition_content(projection)
                expected = (EvidenceWindow(0, len(projection['content']), projection['content']),)
                if candidate['windows'] != expected:
                    raise RecognitionConflict('gap_window_changed')
            else:
                markdown = self.documents.markdown(descriptor['id'], revision=descriptor['revision'])
                if not isinstance(markdown, str) or candidate.get('coordinate_space') != 'document_markdown_v1':
                    raise RecognitionConflict('gap_document_unavailable')
                _, start, end = summary_of(markdown)
                windows = candidate['windows']
                if not windows or any(type(w.start) is not int or type(w.end) is not int
                        or not start <= w.start < w.end <= end or markdown[w.start:w.end] != w.text for w in windows):
                    raise RecognitionConflict('gap_window_changed')
                text = '\n…\n'.join(markdown[w.start:w.end] for w in windows)
            if candidate['excerpt'] != text:
                raise RecognitionConflict('gap_excerpt_changed')
            texts.append(text)
            selection.append({**descriptor, 'layer': candidate['layer'],
                'coordinate_space': candidate.get('coordinate_space', 'recognition_content_v1'),
                'windows': [{'start': w.start, 'end': w.end} for w in candidate['windows']]})
        self.validate_ask_plan(plan)
        validate_frozen_inputs(self.records, models,
            {'scope': {'project_id': plan['project_id']}, 'privacy': privacy})
        with override(**plan['policy_versions']):
            messages = get('retrieve')(None, plan['_drilldown']['retrieval_question'], texts, operation='gap_messages')
        return {'messages': messages, 'privacy': privacy, 'selection': selection}

    def resume_drilldown(self, plan, *, collected=None, queries=()):
        """Continue the same selection and budget; upper evidence is never reranked."""
        pending = plan['_drilldown']
        with override(**plan['policy_versions']):
            self.validate_ask_plan(plan)
            if collected is not None:
                queries = get('retrieve')(None, pending['retrieval_question'],
                    {'queries': list(queries)}, operation='gap_queries')
                rows = collected['candidates']
                if any(row['layer'] not in {'L1', 'L0'} for row in rows):
                    raise RecognitionError('drilldown_requires_lower_candidates')
                from .v2.recall_dedup import cached_candidate_vectors
                vectors = cached_candidate_vectors(self, rows)
                pending['selection'].replace_lower(rows,
                    coverage_questions=[pending['retrieval_question'], *queries], cached_vectors=vectors)
            with stage('ladder'):
                planned = pending['selection'].advance()
            result = {key: value for key, value in plan.items() if key != '_drilldown'}
            result.update(planned)
            if collected is not None:
                result['excluded_sources'] = collected['excluded_sources']
            self.validate_ask_plan(result)
            return result

    def _prepare_ask(self, project_id: str, question: str, *, scene=None, retrieval_question=None, history="", collected=None, coverage_questions=None, situation=None, part_context=None, _pause_after=None, evidence_limit=None, profile_limit=None) -> dict:
        if evidence_limit is not None and (type(evidence_limit) is not int or evidence_limit < 0):
            raise ValueError('invalid_query_evidence_limit')
        if profile_limit is not None and (type(profile_limit) is not int or profile_limit < 0):
            raise ValueError('invalid_query_profile_limit')
        retrieval_question = retrieval_question or question
        if collected is None:
            collected = self.collect_candidates(project_id, retrieval_question, scene=scene, situation=situation or question)
        elif 'method_candidates' in collected and (situation or retrieval_question != question):
            original = self.collect_candidates(project_id, retrieval_question, scene=scene, situation=situation or question,
                rank_reference=collected.get('rank_reference'))
            collected = {**collected, 'method_candidates': original.get('method_candidates', []),
                         'inspiration_candidates': original.get('inspiration_candidates', [])}
            if original.get('time_scope') and not collected.get('time_scope'):
                from .v2.multi_query import fuse_candidates
                collected = {**collected, **{key: original[key] for key in ('time_scope', 'time_documents')},
                    'candidates': fuse_candidates([collected['candidates'], original['candidates']])}
        from .v2.overviews import navigation_candidates
        navigation, overview_guard = navigation_candidates(self, project_id, question, scene)
        if navigation:
            replaced = {(candidate["kind"], candidate["id"], candidate["layer"]) for candidate in navigation}
            collected = {**collected, "candidates": [candidate for candidate in collected["candidates"]
                if (candidate["kind"], candidate["id"], candidate["layer"]) not in replaced] + navigation}
        if collected.get('time_scope'):
            collected = {**collected, 'candidates': self.filter_time_candidates(collected['candidates'],
                collected['time_scope'], collected['time_documents'])}
        from .v2.links import InsightLinks
        links = InsightLinks(self.records, self.service)
        target, budget, overhead = self.ask_budget()
        from .v2.profile import confirmed_profile
        profile = confirmed_profile(self.records, self.service, rank_reference=collected.get('rank_reference'))
        ordinary_limit = evidence_limit
        if evidence_limit is not None or profile_limit is not None:
            from .v2.profile import bounded_profile
            selected_limit = len(profile.get('items', []))
            if profile_limit is not None:
                selected_limit = min(selected_limit, profile_limit)
            if evidence_limit is not None:
                selected_limit = min(selected_limit, evidence_limit)
            profile = bounded_profile(profile, selected_limit)
            if evidence_limit is not None:
                ordinary_limit = evidence_limit - len(profile['items'])
        if part_context:
            from .v2.part_context import validate_context
            validate_context(self.records, self, part_context)
            overhead += _estimate_input_tokens([{'role':'user', 'content':part_context['text']}])
            if overhead > budget:
                raise RecognitionError('part dependency exceeds question budget')
        overhead += _estimate_input_tokens([{"role":"system", "content":profile["text"]}]) if profile["text"] else 0
        if profile.get('instruction'):
            # 单独按原估算器预留提示，最终发送仍合入原指令并据实归属。
            overhead += _estimate_input_tokens([{'role':'system', 'content':profile['instruction']}])
        from .v2.recall_dedup import cached_candidate_vectors
        with stage("vector"):
            vectors = cached_candidate_vectors(self, collected["candidates"])
        with stage("ladder"):
            from .v2.multi_query import fuse_candidates
            options = dict(token_budget=budget, prompt_overhead=overhead,
                history=history, answer_question=question, coverage_questions=coverage_questions,
                neighbors=lambda identity: links.neighbors(project_id, identity), cached_vectors=vectors,
                methods=fuse_candidates([collected.get('method_candidates', [])]), situation=situation or question,
                inspirations=collected.get('inspiration_candidates', ()), evidence_limit=ordinary_limit)
            if _pause_after is None:
                planned = plan_ladder(collected['candidates'], retrieval_question, **options)
            else:
                from .v2.ladder import start_ladder
                selection = start_ladder(collected['candidates'], retrieval_question, **options,
                    lower_order=get('retrieve')(None, operation='lower_order'))
                planned = selection.advance(stop_after=_pause_after)
        from .v2.budget import trim_candidate
        # Preserve the existing body-free rejection metadata when the token
        # window refuses an atomic recognition; ladder trace still counts it.
        excluded = list(collected["excluded_sources"])
        for candidate in collected["candidates"]:
            if (len(excluded) < 20 and candidate["kind"] == "recognition"
                    and trim_candidate(candidate, retrieval_question) is None):
                excluded.append({"type":"recognition", "id":candidate["id"],
                                 "revision":candidate["entry"]["revision"],
                                 "reason":"recognition_evidence_budget_insufficient"})
        plan = {"project_id": project_id, "question": question, "scope": WorkScope("local-user", project_id),
                **({'rank_reference': collected['rank_reference']} if collected.get('rank_reference') else {}),
                **({key: collected[key] for key in ('time_scope', 'time_documents')} if collected.get('time_scope') else {}),
                "target": target, **planned, "profile": profile, **({'part_context':part_context} if part_context else {}), "overview_guard": overview_guard, "history": history, "prompt_overhead": overhead, "excluded_sources": excluded, "state": "pending",
                "expires_monotonic": time.monotonic() + 300,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        if _pause_after is not None:
            plan['drilldown_needed'] = get('retrieve')(None, plan, operation='gap_needed')
            plan['_drilldown'] = {'selection': selection, 'collected': collected, 'scene': scene,
                                  'retrieval_question': retrieval_question}
        return plan

    def public_ask_preview(self, preview_id: str, plan: dict) -> dict:
        chosen = plan["chosen"]
        if not chosen and not plan.get("profile", {}).get("text") and not plan.get('part_context'):
            return {"no_match": True, "answer": "当前项目中没有匹配且可用于回答的资料。",
                    "sources": [], "model_used": False, "excluded_sources": plan["excluded_sources"]}
        sources = []
        for number, candidate in enumerate(chosen, 1):
            entry = candidate["entry"]
            windows = [{"start": window.start, "end": window.end} for window in candidate["windows"]]
            sources.append({"number": number, "type": candidate["kind"], "id": candidate["id"],
                            "title": candidate["title"], "excerpt": candidate["excerpt"],
                            "href": candidate["href"], "revision": entry["revision"],
                            "windows": windows, "start": windows[0]["start"] if windows else 0,
                            "end": windows[0]["end"] if windows else 0,
                            "match_in": candidate["match_in"],
                            **({"conditions": entry["conditions"]} if candidate["kind"] == "recognition" else {}),
                            "coordinate_space": candidate.get("coordinate_space", "workspace_query_content_v1")})
        return {"preview_id": preview_id, "expires_at": plan["expires_at"],
                "project_id": plan["project_id"], "question": plan["question"],
                "execution_location": plan["target"]["execution_location"],
                "model_target": plan["target"], "sources": sources,
                "excluded_sources": plan["excluded_sources"]}

    def store_ask_preview(self, plan: dict) -> str:
        preview_id = "ask-" + uuid4().hex
        with self.ask_preview_lock:
            now = time.monotonic()
            for old_id, old in list(self.ask_previews.items()):
                if old["expires_monotonic"] <= now:
                    del self.ask_previews[old_id]
            while len(self.ask_previews) >= 128:
                self.ask_previews.popitem(last=False)
            self.ask_previews[preview_id] = plan
        return preview_id

    def original_snapshot(self, scope, entry, authority):
        from .original_sources import resolve, original, document_roots
        with self.records.begin() as reader:
            if entry["kind"] == "source":
                kind,identity = resolve(reader,scope,entry["id"],kind="source")
                roots = ((kind,identity,original(reader,scope,kind,identity).revision),)
            else:
                document = reader.read("documents",entry["id"])
                if document is None:
                    raise RecognitionConflict("document is unavailable")
                from backend.recognition.document_filings import filing_experience, DocumentFilingError
                try:
                    copied = filing_experience(reader, scope, entry['id'])
                except DocumentFilingError as error:
                    raise RecognitionConflict(str(error)) from error
                roots = ((('experience', copied.object_id, copied.revision),) if copied is not None
                    else document_roots(reader,scope,document.payload.get("source_refs",[]),optional=True))
        return authority.snapshot(scope,[{"type":kind,"id":identity,"revision":revision}
            for kind,identity,revision in roots]) if roots else None

    def validate_ask_plan(self, plan: dict) -> None:
        if plan.get('part_context'):
            from .v2.part_context import validate_context
            validate_context(self.records, self, plan['part_context'])
        if (plan["chosen"] and plan["target"]["execution_location"] == "remote"
                and not egress_allowed(self.records, self.models, plan["project_id"], "generation")):
            code = ("private_project_remote_blocked" if is_private_project(self.records, plan["project_id"])
                    else "remote_disabled")
            raise ModelConfigurationError(code)
        if self.ask_target() != plan["target"]:
            raise ModelConfigurationError("ask_model_target_changed")
        if plan.get("profile") is not None:
            from .v2.profile import validate_profile
            validate_profile(self.records, self.service, plan["profile"])
        if plan.get("overview_guard") is not None:
            plan["overview_guard"]()
        if plan.get("bookshelf_guard") is not None:
            plan["bookshelf_guard"]()
        if plan.get("history_guard") is not None:
            plan["history_guard"]()
        self._validate_ask_sources(plan)

    def _validate_ask_sources(self, plan, *, local_only=False):
        project_id = plan["project_id"]
        selected = [candidate["entry"] for candidate in plan["chosen"]
                    if candidate["kind"] not in {"recognition", "search"} and not candidate.get('inspiration')]
        current_entries = {(entry["kind"], entry["id"]): entry for entry in self.query_entries(project_id, selected=selected)}
        authority = SourceEgressService(self.records)
        for candidate in plan["chosen"]:
            if candidate['kind'] == 'search':
                if not callable(plan.get('search_guard')):
                    raise RecognitionConflict('search_proof_missing')
                continue
            frozen = candidate["entry"]
            own_scope = candidate.get("scope", plan["scope"])
            if candidate.get('inspiration'):
                from .v2.inspirations import validate_inspiration
                validate_inspiration(self.records, project_id, candidate,
                                     remote=not local_only and plan['target']['execution_location'] == 'remote')
                continue
            if not local_only and is_private_project(self.records, own_scope.project_id):
                raise RecognitionError("source excluded from recall during question")
            if (not local_only and plan["target"]["execution_location"] == "remote"
                    and not egress_allowed(self.records, self.models, own_scope.project_id, "generation")):
                raise ModelConfigurationError("remote_disabled")
            if candidate["kind"] != "recognition":
                current_snapshot = self.original_snapshot(own_scope,frozen,authority)
                if current_snapshot != candidate["snapshot"]:
                    raise RecognitionConflict("original privacy changed during question")
                if current_snapshot is not None:
                    authority.validate_snapshot(own_scope,current_snapshot)
                    if not local_only:
                        authority.require(current_snapshot,"generation")
            if candidate["kind"] == "recognition":
                if candidate.get('time_scope'):
                    from .v2.insight_validity import read_validity, COLLECTION
                    marker = self.records.read(COLLECTION, frozen['id'])
                    if ((marker.revision if marker else 0) != candidate['validity_revision']
                            or read_validity(self.records, frozen['id']) != candidate['validity']):
                        raise RecognitionConflict('insight validity changed during question')
                if is_recall_excluded(self.records, own_scope, frozen["id"]) and not (candidate.get("bookshelf") and plan.get("bookshelf_guard")):
                    raise RecognitionError("source excluded from recall during question")
                authority.validate_snapshot(own_scope, candidate["snapshot"])
                if not local_only:
                    authority.require(candidate["snapshot"], "generation")
                recognition = self.service.get_recognition(scope=own_scope, recognition_id=frozen["id"])
                current = recognition.retrieval_projection() if recognition is not None and recognition.authorized else None
            else:
                recall_documents = [frozen["id"]] if candidate["kind"] == "document" else candidate.get("document_ids", [])
                if _documents_forgotten(self.records, recall_documents):
                    raise RecognitionError("source excluded from recall during question")
                current = current_entries.get((frozen["kind"], frozen["id"]))
            if (current is None or current.get("revision") != frozen["revision"]
                    or current.get("content") != frozen["content"]
                    or current.get("title") != frozen.get("title")
                    or current.get("item_id") != frozen.get("item_id")
                    or current.get("item_revision") != frozen.get("item_revision")):
                raise RecognitionError("source changed during question")
        # 普通材料读取后复验搜索证明；空材料与直接调用也保留这次末端检查。
        if plan.get('search_guard') is not None:
            plan['search_guard']()

    def mark_ask_receipt(self, preview_id: str, status: str, *, usage: dict | None = None) -> None:
        with self.records.begin() as tx:
            row = tx.read("workspace_ask_receipts", preview_id)
            if row is None:
                return
            payload = {**row.payload, "status": status, "updated_at": _now()}
            if usage is not None:
                payload["usage"] = usage
            tx.put("workspace_ask_receipts", preview_id, payload, expected_revision=row.revision)
            tx.commit()

    async def execute_ask(self, preview_id: str, project_id: str, question: str, consent: bool, *, on_delta=None, continuation=None, on_retry=None):
        if ACTIVE_ANSWER.get() is None:
            from uuid import uuid5, NAMESPACE_URL
            with self.ask_preview_lock:
                preview = self.ask_previews.get(preview_id)
                if preview is None or preview["expires_monotonic"] <= time.monotonic():
                    raise HTTPException(409, "ask_preview_expired")
                if preview["project_id"] != project_id or preview["question"] != question:
                    raise HTTPException(404, "ask_preview_not_found")
                if preview["state"] == "completed":
                    return preview["result"]
                if preview["state"] != "pending":
                    raise HTTPException(409, "ask_preview_used")
            return await self.answer_turns.run(turn_id="turn-" + uuid5(NAMESPACE_URL, "workspace-ask:" + preview_id).hex, project=project_id,
                question=question, policy_versions=preview.get('policy_versions'),
                operation=lambda: self.execute_ask(preview_id, project_id, question, consent, on_delta=on_delta, on_retry=on_retry))
        with self.ask_preview_lock:
            plan = self.ask_previews.get(preview_id)
            if plan is None or plan["expires_monotonic"] <= time.monotonic():
                raise HTTPException(409, "ask_preview_expired")
            if plan["project_id"] != project_id or plan["question"] != question:
                raise HTTPException(404, "ask_preview_not_found")
            if plan["state"] == "completed":
                return plan["result"]
            if plan["state"] != "pending":
                raise HTTPException(409, "ask_preview_used")
            try:
                self.validate_ask_plan(plan)
            except RecognitionError:
                raise HTTPException(409, "source_changed_retry") from None
            except ModelConfigurationError as failure:
                code = str(failure)
                raise HTTPException(409, code if code in {"remote_disabled", "private_project_remote_blocked"}
                                    else "ask_model_target_changed") from None
            with self.records.begin() as tx:
                tx.put("workspace_ask_receipts", preview_id, {
                    "id": preview_id, "project_id": project_id, "status": "started",
                    "created_at": _now(), "updated_at": _now(),
                    "consented_at": _now() if plan["target"]["execution_location"] == "remote" else None,
                    "consent_basis": {"scope": "global_setting", "settings_revision": {
                        "generation": plan["target"]["revision"], "mode": plan["target"]["mode_revision"]}},
                    "target": plan["target"],
                    "trace": plan.get("trace", []),
                    "sources": [{"kind": c["kind"], "id": c["entry"]["id"],
                                 "revision": c["entry"]["revision"],
                                 "item_revision": c["entry"].get("item_revision"),
                                 "windows": [{"start": w.start, "end": w.end} for w in c["windows"]],
                                 "coordinate_space": c.get("coordinate_space", "workspace_query_content_v1")}
                                for c in plan["chosen"]] + [
                                    {'kind':'recognition', 'id':item['id'], 'revision':item['revision'],
                                     'item_revision':None, 'windows':[], 'coordinate_space':'profile_block_v1'}
                                    for item in plan.get('profile', {}).get('items', [])],
                    "attempt": 1,
                }, expected_revision=0)
                tx.commit()
            plan["state"] = "running"

        chosen = plan["chosen"]
        answer_sources = [{"number": index, "type": c["kind"], "id": c["id"],
                           "title": c["title"], "excerpt": c["excerpt"], "href": c["href"],
                           "windows": [{"start": w.start, "end": w.end} for w in c["windows"]],
                           "start": c["windows"][0].start if c["windows"] else 0,
                           "end": c["windows"][0].end if c["windows"] else 0,
                           "match_in": c["match_in"],
                           **({"conditions": c["entry"]["conditions"]} if c["kind"] == "recognition" else {}),
                           "coordinate_space": c.get("coordinate_space", "workspace_query_content_v1")}
                          for index, c in enumerate(chosen, 1)]
        with stage("prompt_build"):
            source_texts = format_source_texts(chosen)
            messages = [{"role": "system", "content": get('compose')(ask_instruction, chosen)},
                        {"role": "user", "content": get('compose')(_ask_user_text, source_texts, question, plan.get('history', ''))}]
            from .v2.profile import profile_messages
            messages = profile_messages(plan.get("profile", {}), messages)
            if plan.get('part_context'):
                messages.insert(1 if plan.get('profile', {}).get('text') else 0,
                    {'role':'user', 'content':plan['part_context']['text']})
        invocation_key = continuation['invocation_key'] if continuation else 'answer'
        if continuation:
            messages = get('retry')({'kind': 'continue_messages', 'messages': continuation['messages'],
                'partial': continuation['partial']})
        try:
            dispatched_at = time.perf_counter()

            def generate():
                _record_elapsed("gateway_send", dispatched_at)
                started = time.perf_counter()
                first_seen = False
                # Legacy adapters emit their entire answer only after completion;
                # that callback cannot establish a streamed time-to-first-token.
                streaming = on_delta is not None and callable(getattr(self.models, "complete_stream", None))

                def measured_delta(text):
                    nonlocal first_seen
                    if streaming and text and not first_seen:
                        first_seen = True
                        _record_elapsed("first_token", started)
                    if on_delta is not None:
                        on_delta(text)

                try:
                    return generate_answer(self.models, messages, response_model=AskOutput,
                        max_tokens=512 if plan["target"]["execution_location"] == "local" else 7000,
                        validate_current=lambda: self.validate_ask_plan(plan),
                        on_delta=measured_delta if on_delta is not None else None, invocation_key=invocation_key,
                        **({'on_retry': on_retry} if on_retry is not None else {}))
                finally:
                    _record_elapsed("generation", started)

            output, meta = await run_in_threadpool(generate)
            self.validate_ask_plan(plan)
            answer, citations = output.answer, output.citations
            if continuation:
                answer = continuation['partial'] + answer
                if isinstance(citations, list):
                    citations = [*(int(square or fullwidth) for square, fullwidth in
                                   re.findall(r'\[(\d+)\]|【(\d+)】', continuation['partial'])),
                                 *citations]
                from .v2.followup import sum_usage
                meta = {**meta, 'usage': sum_usage(continuation['usage'], meta.get('usage', {}))}
            if (not isinstance(answer, str) or not answer.strip() or not isinstance(citations, list)
                    or any(type(number) is not int or number < 1 or number > len(answer_sources) for number in citations)):
                raise ValueError("invalid_answer")
            result = {"answer": answer.strip(), "sources": [answer_sources[number - 1]
                      for number in dict.fromkeys(citations)], "model_used": True,
                      "model_usage": meta.get("usage", {}), "excluded_sources": plan["excluded_sources"],
                      "context": _ask_context(chosen, source_texts, messages, meta.get("context_budget"),
                          plan.get("history", ""), plan.get("history_count", 0), plan.get("profile"), plan.get('part_context'))}
            if any(candidate.get('supplemented') for candidate in chosen) and ACTIVE_ANSWER.get() is not None:
                result['context']['feedback'] = {'turn_id': ACTIVE_ANSWER.get()[0]['turn_id'],
                                                 'project_id': project_id}
            self.mark_ask_receipt(preview_id, "completed", usage=result["model_usage"])
            with self.ask_preview_lock:
                plan["result"] = result
                plan["state"] = "completed"
                plan["chosen"] = []  # Idempotent replay needs only the bounded public result.
            return result
        except ModelInterrupted as interrupted:
            self.validate_ask_plan(plan)
            observation = answer_observation(invocation_key)
            from .v2.followup import sum_usage
            usage = sum_usage(*observation['observations'])
            if continuation:
                usage = sum_usage(continuation['usage'], usage)
            if not observation['complete']:
                usage['observed_only'] = True
            self.mark_ask_receipt(preview_id, 'failed', usage=usage)
            with self.ask_preview_lock:
                plan['state'] = 'failed'
            return {'answer': None, 'partial': (continuation['partial'] if continuation else '') + interrupted.partial,
                'interruption': interrupted.interruption, 'sources': [],
                'model_used': True, 'model_usage': usage, 'excluded_sources': plan['excluded_sources'],
                'context': _ask_context(chosen, source_texts, messages, None,
                    plan.get('history', ''), plan.get('history_count', 0), plan.get('profile'))}
        except RecognitionError:
            error = HTTPException(409, "source_changed_retry")
        except ModelConfigurationError as failure:
            code = str(failure)
            if plan["target"]["execution_location"] == "remote" and code in {
                "model_configuration_changed_before_request", "model_egress_remote_not_consented"
            }:
                if not egress_allowed(self.records, self.models, plan["project_id"], "generation"):
                    code = "private_project_remote_blocked" if is_private_project(self.records, plan["project_id"]) else "remote_disabled"
                elif code == "model_configuration_changed_before_request" or self.ask_target() != plan["target"]:
                    code = "ask_model_target_changed"
            error = HTTPException(409, code) if code in {"ask_model_target_changed", "remote_disabled", "private_project_remote_blocked"} else HTTPException(502, "answer_generation_failed")
        except Exception:
            error = HTTPException(502, "answer_generation_failed")
        try:
            self.mark_ask_receipt(preview_id, "failed")
        finally:
            with self.ask_preview_lock:
                plan["state"] = "failed"
        raise error

    async def ask_preview(self, body: dict):
        project_id = _project(body.get("project_id", "default"))
        question = _text(body.get("question"), "question")
        if len(question) > 1000:
            raise HTTPException(400, "question_too_long")
        plan = self.prepare_ask(project_id, question)
        if not plan["chosen"] and not plan.get("profile", {}).get("text") and not plan.get('part_context'):
            return self.public_ask_preview("", plan)
        preview_id = self.store_ask_preview(plan)
        return self.public_ask_preview(preview_id, plan)

    async def ask(self, body: dict):
        project_id = _project(body.get("project_id", "default"))
        question = _text(body.get("question"), "question")
        if len(question) > 1000:
            raise HTTPException(400, "question_too_long")
        preview_id = body.get("preview_id")
        if preview_id is not None:
            if not isinstance(preview_id, str) or not re.fullmatch(r"ask-[0-9a-f]{32}", preview_id):
                raise HTTPException(400, "invalid_ask_preview_id")
            return await self.execute_ask(preview_id, project_id, question,
                                          True)
        if (self.ask_target()["execution_location"] == "remote"
                and is_private_project(self.records, project_id)):
            raise HTTPException(409, "private_project_remote_blocked")
        plan = self.prepare_ask(project_id, question)
        if not plan["chosen"] and not plan.get("profile", {}).get("text") and not plan.get('part_context'):
            return self.public_ask_preview("", plan)
        preview_id = self.store_ask_preview(plan)
        return await self.execute_ask(preview_id, project_id, question, True)
