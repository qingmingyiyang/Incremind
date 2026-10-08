"""Dynamic forgotten-memory spines, bounded consultation and CAS relearning."""

import logging
from types import SimpleNamespace
from uuid import uuid4

from backend.recognition import RecognitionError, WorkScope
from core.search_and_recall.evidence_windows import EvidenceWindow, select_evidence_windows
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..context_adapter import format_recognition_content
from ..source_egress import SourceEgressService
from .budget import evidence_tokens, input_tokens, text_tokens
from .insights import insight_view, source_documents
from .layers import summary_of
from .links import InsightLinks
from .privacy import is_private_project, privacy_revision, egress_allowed
from .projects import scene_of
from .policies import get
from .policies.types import RankInput, ScopeInput
from .policies.scope import prefer_scene_ties, scene_priority
from .usage import utc_now

# Fixed forgotten questions: intended spine .133333, other spines .0625.
KEYWORD_THRESHOLD = 0.10
_LOGGER = logging.getLogger(__name__)


def _preferences(query, scope, snapshot, documents):
    values = {}
    for node in snapshot.get("nodes", []):
        if node["type"] == "recognition":
            values[("recognition_recall_preferences", node["id"])] = query.records.read(
                "recognition_recall_preferences", node["id"]
            )
    for identity in documents:
        row = query.records.read("v2_document_recall", identity)
        if row and row.payload.get("state") == "forgotten":
            raise RecognitionError("bookshelf_document_forgotten")
        values[("v2_document_recall", identity)] = row
        values[("documents", identity)] = query.records.read("documents", identity)
    for (collection, _), row in values.items():
        if (
            collection == "recognition_recall_preferences"
            and row
            and row.payload.get("state") == "forgotten"
            and row.payload.get("by", "user") != "auto"
        ):
            raise RecognitionError("bookshelf_manual_forget")
        if collection == "documents" and (
            row is None or row.payload.get("status") == "archived" or row.payload.get("project_id") != scope.project_id
        ):
            raise RecognitionError("bookshelf_document_unavailable")
    return values


def _spines(query, project, scene):
    authority, records = SourceEgressService(query.records), query.records
    spines = []
    for own_project in dict.fromkeys((project, "me")):
        scope = WorkScope("local-user", own_project)
        if is_private_project(records, own_project):
            continue
        for row in records.list_matching("recognitions", state="active"):
            if row.payload.get("scope") != {"user_id": "local-user", "project_id": own_project}:
                continue
            preference = records.read("recognition_recall_preferences", row.object_id)
            if (
                not preference
                or preference.payload.get("project_id") != own_project
                or preference.payload.get("user_id") != "local-user"
                or preference.payload.get("state") != "forgotten"
                or preference.payload.get("by", "user") != "auto"
            ):
                continue
            try:
                insight = query.service.get_recognition(scope=scope, recognition_id=row.object_id)
                if insight is None or not insight.authorized:
                    continue
                entry = insight.retrieval_projection()
                view = insight_view(records, scope, row.object_id, service=query.service)
                if view is None or not get('scope')(ScopeInput(scene, view["scene"])):
                    continue
                snapshot = authority.snapshot(
                    scope, [{"type": "recognition", "id": row.object_id, "revision": row.revision}]
                )
                authority.require(snapshot, "generation")
                docs = source_documents(
                    records, scope, [n["id"] for n in snapshot["nodes"] if n["type"] == "experience"]
                )
                preferences = _preferences(query, scope, snapshot, docs)
                spines.append(
                    {
                        "kind": "recognition",
                        "id": row.object_id,
                        "layer": "insight",
                        "scope": scope,
                        "title": entry["content"],
                        "text": entry["content"],
                        "date": row.payload.get("updated_at") or row.payload.get("created_at"),
                        "scene": view["scene"],
                        "entry": entry,
                        "snapshot": snapshot,
                        "snapshots": [snapshot],
                        "documents": sorted(docs),
                        "preferences": preferences,
                        "recall": preference,
                        "href": f"#view=recognition&project_id={own_project}",
                    }
                )
            except (RecognitionError, ValueError):
                continue
        if own_project != project:
            continue
        cooled = [{'kind':'document', 'id':row.object_id} for row in records.list('v2_document_recall')
                  if row.payload.get('state') == 'cooled'
                  and row.payload.get('project_id', own_project) == own_project]
        for entry in query.query_entries(own_project, selected=cooled):
            if entry["kind"] != "document":
                continue
            row = records.read("documents", entry["id"])
            recall = records.read("v2_document_recall", entry["id"])
            if (
                row is None
                or not recall
                or recall.payload.get("state") != "cooled"
                or recall.payload.get("project_id", own_project) != own_project
            ):
                continue
            assignment = scene_of(records, "document", entry["id"])
            assigned_scene = assignment.get("scene") if assignment else None
            if not get('scope')(ScopeInput(scene, assigned_scene)):
                continue
            summary, _, _ = summary_of(query.documents.markdown(entry["id"]))
            first_sentence = summary.split("。", 1)[0].split("\n", 1)[0]
            snapshots = []
            try:
                for experience in records.list("recognition_experiences"):
                    if experience.payload.get("scope") != {"user_id": "local-user", "project_id": own_project}:
                        continue
                    refs = experience.payload.get("provenance", {}).get("source_refs", [])
                    if any(ref.get("type") == "document" and ref.get("id") == entry["id"] for ref in refs):
                        snapshot = authority.snapshot(
                            scope, [{"type": "experience", "id": experience.object_id, "revision": experience.revision}]
                        )
                        authority.require(snapshot, "generation")
                        snapshots.append(snapshot)
                preferences = _preferences(query, scope, {}, [entry["id"]])
                spines.append(
                    {
                        "kind": "document",
                        "id": entry["id"],
                        "layer": "note",
                        "scope": scope,
                        "title": entry["title"],
                        "text": entry["title"] + "\n" + first_sentence,
                        "date": row.payload.get("updated_at") or row.payload.get("created_at"),
                        "scene": assigned_scene,
                        "entry": entry,
                        "snapshots": snapshots,
                        "documents": [entry["id"]],
                        "preferences": preferences,
                        "recall": recall,
                        "href": entry["href"],
                    }
                )
            except (RecognitionError, ValueError):
                continue
    return spines


def _validate(query, spines, version, project, scene):
    if privacy_revision(query.records) != version:
        raise RecognitionError("bookshelf_privacy_changed")
    authority = SourceEgressService(query.records)
    for spine in spines:
        scope = spine["scope"]
        if is_private_project(query.records, scope.project_id):
            raise RecognitionError("bookshelf_project_private")
        if query.ask_target()["execution_location"] == "remote" and not egress_allowed(
            query.records, query.models, scope.project_id, "generation"
        ):
            raise RecognitionError("bookshelf_egress_disabled")
        for (collection, identity), row in spine["preferences"].items():
            if query.records.read(collection, identity) != row:
                raise RecognitionError("bookshelf_recall_changed")
        for snapshot in spine["snapshots"]:
            authority.validate_snapshot(scope, snapshot)
            authority.require(snapshot, "generation")
        if spine["kind"] == "document":
            entries = query.query_entries(project, selected=[spine["entry"]])
            if spine["entry"] not in entries:
                raise RecognitionError("bookshelf_document_changed")
            assignment = scene_of(query.records, "document", spine["id"])
            if (assignment.get("scene") if assignment else None) != spine["scene"]:
                raise RecognitionError("bookshelf_scene_changed")
        else:
            view = insight_view(query.records, scope, spine["id"], service=query.service)
            if view is None or view["scene"] != spine["scene"]:
                raise RecognitionError("bookshelf_scene_changed")


def validate_bookshelf_binding(query, binding, *, turn_id=None):
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecord
    hits = [{**spine, 'scope':WorkScope(**spine['scope']),
             'preferences':{tuple(key):SQLiteStructuredRecord(**row) if row else None
                            for key, row in spine['preferences']}} for spine in binding['hits']]
    if turn_id:
        # Only the CAS transition written by this answer's actual citation may
        # update a recall preference. Source/policy snapshots remain untouched.
        from dataclasses import asdict
        for activity in query.records.list_matching('v2_activity', turn_id=turn_id):
            proof = activity.payload.get('recall_transition')
            if activity.revision != 1 or activity.payload.get('kind') != 'revive' or not proof:
                continue
            before, after = proof['before'], proof['after']
            key = (before['collection'], before['object_id'])
            if (after['collection'], after['object_id']) != key or after['revision'] != before['revision'] + 1:
                raise RecognitionError('bookshelf_revival_changed')
            if (before['payload'].get('by') != 'auto' or after['payload'].get('by') != 'auto'
                    or (before['payload'].get('state'), after['payload'].get('state'))
                    not in {('forgotten', 'cooled'), ('cooled', 'normal')}):
                raise RecognitionError('bookshelf_revival_changed')
            for spine in hits:
                frozen = spine['preferences'].get(key)
                if frozen is not None and asdict(frozen) == before:
                    spine['preferences'][key] = SQLiteStructuredRecord(**after)
    _validate(query, hits, binding['version'], binding['project'], binding['scene'])


def _document_windows(markdown, windows):
    """Keep every original span once, within the existing per-window budget."""
    result = []
    for window in sorted(windows, key=lambda w: (w.start, w.end)):
        start, end = window.start, window.end
        if result and start < result[-1].end:
            previous = result[-1]
            if end <= previous.end:
                continue
            combined = markdown[previous.start:end]
            if text_tokens(combined) <= 1200:
                result[-1] = EvidenceWindow(previous.start, end, combined)
                continue
            start = previous.end
        result.append(EvidenceWindow(start, end, markdown[start:end]))
    return tuple(result)


def _content(query, spine, question, *, rank_reference=None):
    entry, scope = spine["entry"], spine["scope"]
    if spine["kind"] == "recognition":
        excerpt = format_recognition_content(entry)
        evidence = []
        for identity in spine["documents"]:
            summary, _, _ = summary_of(query.documents.markdown(identity))
            if summary:
                evidence.append(summary)
        if not evidence:
            for node in spine["snapshot"]["nodes"]:
                if node["type"] == "experience":
                    row = query.records.read("recognition_experiences", node["id"])
                    if row and isinstance(row.payload.get("content"), str) and row.payload["content"].strip():
                        evidence.append(row.payload["content"])
        if not evidence:
            return None
        excerpt += "\n\n原始依据：\n" + "\n\n".join(evidence)
        if text_tokens(excerpt) > 1200:
            return None
        windows = (EvidenceWindow(0, len(entry["content"]), entry["content"]),)
        layer, coordinates = "L3", "recognition_content_v1"
    else:
        markdown = query.documents.markdown(entry["id"])
        summary, start, end = summary_of(markdown)
        windows = [EvidenceWindow(start, end, markdown[start:end])] if summary else []
        selection = select_evidence_windows(markdown, question, title=entry["title"], max_chars=3600)
        windows.extend(w for w in selection.windows if (w.start, w.end) != (start, end))
        windows = _document_windows(markdown, windows)
        excerpt = "\n…\n".join(w.text for w in windows)
        if not excerpt or any(text_tokens(w.text) > 1200 for w in windows):
            return None
        layer, coordinates = "L1", "document_markdown_v1"
    usage_kind = "insight" if spine["kind"] == "recognition" else "document"
    usage = query.records.read("v2_usage_" + usage_kind, spine["id"])
    candidate = {
        "id": spine["id"],
        "kind": spine["kind"],
        "score": spine["score"],
        "layer": layer,
        "title": spine["title"],
        "excerpt": excerpt,
        "entry": entry,
        "snapshot": (query.original_snapshot(scope, entry, SourceEgressService(query.records))
                     if spine["kind"] == "document" else spine.get("snapshot")),
        "scope": scope,
        "scene": spine["scene"],
        "href": spine["href"],
        "windows": windows,
        "match_in": "content",
        "coordinate_space": coordinates,
        "bookshelf": True,
        "document_id": entry.get("document_id"),
        "document_ids": spine["documents"],
        "persona": False,
        "bookshelf_restore": {
            "kind": usage_kind,
            "id": spine["id"],
            "project": scope.project_id,
            "recall_revision": spine["recall"].revision,
            "usage_count": usage.payload["count"] if usage else 1,
        },
    }
    policy = get('rank')
    if spine['kind'] == 'recognition' and callable(getattr(policy, 'decorate', None)):
        candidate = policy.decorate(candidate, RankInput(spine['score'], 1,
            conditions=entry['conditions'], reference=rank_reference))
    return candidate


def consult_bookshelf(query, project, question, plan, *, scene=None):
    version = privacy_revision(query.records)
    spines = _spines(query, project, scene)
    proxies = [
        SimpleNamespace(
            object_id="bookshelf-" + s["kind"] + "-" + s["id"],
            revision=s["entry"]["revision"],
            payload={"project_id": s["scope"].project_id, "content": s["text"]},
        )
        for s in spines
    ]
    by_id = {row.object_id: spine for row, spine in zip(proxies, spines)}
    anchor = SimpleNamespace(
        object_id="bookshelf-query", revision=0, payload={"project_id": project, "content": question}
    )

    def refs(row):
        spine = by_id.get(row.object_id)
        return [root for snapshot in spine["snapshots"] for root in snapshot["roots"]] if spine else []

    def validate_spines(values):
        if plan.get("history_guard"):
            plan["history_guard"]()
        _validate(query, values, version, project, scene)

    ranked, threshold = (
        InsightLinks(
            query.records, query.service, query.models if callable(getattr(query.models, "public", None)) else None
        )._rank(
            project,
            anchor,
            proxies,
            source_refs=refs,
            validate_current=lambda: validate_spines(spines),
            limit=len(proxies),
            keyword_threshold=KEYWORD_THRESHOLD,
        )
        if proxies
        else ([], KEYWORD_THRESHOLD)
    )
    hits = [{**by_id[row.object_id], "score": score} for row, score in ranked if score >= threshold]
    policy = get('rank')
    if callable(getattr(policy, 'adjust', None)):
        hits = [{**spine, 'score': policy.adjust(spine['score'], RankInput(spine['score'], 1,
            conditions=spine['entry']['conditions'], reference=plan.get('rank_reference')))}
            if spine['kind'] == 'recognition' else spine for spine in hits]
        hits.sort(key=lambda spine: -spine['score'])
    for spine in hits:
        priority = scene_priority(ScopeInput(scene, spine["scene"]), get('scope'))
        if priority:
            spine["scope_priority"] = priority
    hits = prefer_scene_ties(hits, score_key=lambda spine: -round(spine["score"], 3))
    validate_spines(hits)
    used, skipped = 0, 0
    if not plan["trace"][-1]["stopped"]:
        for spine in hits:
            if used == 2:
                break
            try:
                candidate = _content(query, spine, question, rank_reference=plan.get('rank_reference'))
            except (RecognitionError, ValueError):
                candidate = None
            if candidate is None:
                skipped += 1
                continue
            if plan.get('time_scope'):
                eligible = query.filter_time_candidates([candidate], plan['time_scope'], plan['time_documents'])
                if not eligible:
                    continue
                candidate = eligible[0]
            # Keep original-coordinate evidence; combine only markdown-layer slices.
            replaced = [
                c
                for c in plan["chosen"]
                if c["kind"] == candidate["kind"]
                and c["entry"]["id"] == candidate["entry"]["id"]
                and c["layer"] != "L0"
            ]
            if candidate["kind"] == "document":
                windows = {(w.start, w.end, w.text): w for w in candidate["windows"]}
                for previous in replaced:
                    if (
                        previous.get("coordinate_space") == candidate["coordinate_space"]
                        and previous["entry"]["revision"] == candidate["entry"]["revision"]
                    ):
                        windows.update({(w.start, w.end, w.text): w for w in previous["windows"]})
                candidate["windows"] = _document_windows(query.documents.markdown(candidate["entry"]["id"]), windows.values())
                candidate["excerpt"] = "\n…\n".join(w.text for w in candidate["windows"])
            retained = [c for c in plan["chosen"] if c not in replaced]
            candidate["persona"] = spine["scope"].project_id == "me" and project != "me"
            proposed = retained + [candidate]
            if (
                evidence_tokens(proposed) > int(0.8 * plan["budget"])
                or input_tokens(proposed, plan["question"], reserve_refutes=True, history=plan.get("history", ""))
                + plan.get("prompt_overhead", 0)
                > plan["budget"]
            ):
                skipped += 1
                continue
            for removed in plan["chosen"]:
                if removed not in retained:
                    row = next(
                        (r for r in plan["trace"] if r["layer"] == removed["layer"] and "expanded_from" not in r), None
                    )
                    if row:
                        row["selected"] = max(0, row["selected"] - 1)
            plan["chosen"] = proposed
            row = next(
                (r for r in plan["trace"] if r["layer"] == candidate["layer"] and "expanded_from" not in r), None
            )
            if row:
                row["considered"] += 1
                row["selected"] += 1
            used += 1
    plan["bookshelf_guard"] = lambda: validate_spines(hits)
    from dataclasses import asdict
    plan['bookshelf_guard'].frozen_binding = {'version':version, 'project':project, 'scene':scene,
        'hits':[{**spine, 'scope':{'user_id':spine['scope'].user_id, 'project_id':spine['scope'].project_id},
                 'preferences':[[list(key), asdict(row) if row else None] for key, row in spine['preferences'].items()]}
                for spine in hits]}
    plan['bookshelf_guard'].continuation = {'spines': hits, 'version': version, 'project': project, 'scene': scene}
    plan["bookshelf"] = {
        "hits": len(hits),
        "used": used,
        "spines": [
            {
                **{key: spine[key] for key in ("id", "kind", "layer", "title", "date", "scene", "href")},
                "persona": spine["scope"].project_id == "me" and project != "me",
            }
            for spine in hits
        ],
    }
    plan["trace"][0]["bookshelf"] = {"hits": len(hits), "used": used}
    plan["trace"][0]["skipped_budget"] += skipped
    return plan


def relearn_cited(records, chosen, citations, *, turn_id=None):
    cited = {citation["n"] for citation in citations}
    for number, candidate in enumerate(chosen, 1):
        info = candidate.get("bookshelf_restore")
        if number not in cited or not info:
            continue
        collection = "recognition_recall_preferences" if info["kind"] == "insight" else "v2_document_recall"
        try:
            with records.begin() as tx:
                recall = tx.read(collection, info["id"])
                usage = tx.read("v2_usage_" + info["kind"], info["id"])
                if (
                    recall is None
                    or recall.revision != info["recall_revision"]
                    or usage is None
                    or usage.payload.get("project_id") != info["project"]
                    or usage.payload.get("count") != info["usage_count"] + 1
                ):
                    continue
                if info["kind"] == "insight":
                    if recall.payload.get("state") != "forgotten" or recall.payload.get("by", "user") != "auto":
                        continue
                    score, state = 0.5, "cooled"
                else:
                    if recall.payload.get("state") != "cooled" or usage.payload.get("score", 0) < 0.5:
                        continue
                    score, state = usage.payload["score"], "normal"
                now = utc_now().isoformat()
                restored = tx.put(
                    collection,
                    info["id"],
                    {**recall.payload, "state": state, "by": "auto", "changed_at": now},
                    expected_revision=recall.revision,
                )
                tx.put(
                    "v2_usage_" + info["kind"],
                    info["id"],
                    {**usage.payload, "score": score},
                    expected_revision=usage.revision,
                )
                tx.put(
                    "v2_activity",
                    "activity-" + uuid4().hex,
                    {
                        "kind": "revive",
                        "by": "auto",
                        "project_id": info["project"],
                        "object_kind": info["kind"],
                        "object_id": info["id"],
                        "created_at": now,
                        **({'turn_id':turn_id, 'recall_transition':{
                            'before':{'collection':recall.collection, 'object_id':recall.object_id,
                                      'revision':recall.revision, 'payload':dict(recall.payload)},
                            'after':{'collection':restored.collection, 'object_id':restored.object_id,
                                     'revision':restored.revision, 'payload':dict(restored.payload)}}} if turn_id else {}),
                    },
                    expected_revision=0,
                )
                tx.commit()
        except SQLiteUnitOfWorkConflict:
            continue
        except Exception as error:
            _LOGGER.warning("bookshelf_relearn_failed exception_type=%s", type(error).__name__)
