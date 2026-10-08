"""Daily, source-bound consolidation. Every semantic output remains reviewable."""

import json
import logging
import re
import time
from threading import RLock
from .policies import get, override
from .policies.pipelines import versions_for_turn
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from ..document_recognition import ensure_document_experience, CANDIDATE_SOURCE_CONSTRAINTS
from ..generation_sources import generation_source_guard
from ..relations import RelationProposalService
from ..source_egress import SourceEgressService
from .memory_turn import MemoryTurn
from ..workspace_contracts import _json, _project
from .layers import summary_of
from .insights import source_documents
from .links import InsightLinks, similarity
from .privacy import egress_allowed, is_private_project
from .usage import timestamp, utc_now
from .transaction_records import TransactionRecords

_LOGGER = logging.getLogger(__name__)
DUPLICATE_THRESHOLD = 0.9


class PatternOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=40)
    conditions: list[str] = Field(max_length=12)


class LearningOutput(PatternOutput):
    event_ids: list[str] = Field(default_factory=list)
    kind: str = 'pattern'


@dataclass
class Memory:
    object_id: str
    revision: int
    payload: dict
    kind: str
    experiences: tuple
    documents: tuple
    refs: tuple


def compatible(a, b):
    """High overlap alone cannot establish duplicate numeric or negative claims."""
    numbers = lambda s: re.findall(r"\d+(?:\.\d+)?", s)
    polarity = lambda s: set(re.findall(r"不能|禁止|无需|不得|不|\bnot\b|\bnever\b|\bno\b", s.lower()))
    return numbers(a) == numbers(b) and polarity(a) == polarity(b)


def evidence_guard(reader, scope, snapshot):
    SourceEgressService(TransactionRecords(reader)).validate_snapshot(scope, snapshot)


class Consolidation:
    def __init__(self, records, service, documents, models=None, *, now=utc_now, recent_days=7, completion_clock=None):
        self.records, self.service, self.documents, self.models = records, service, documents, models
        self.now, self.recent_days = now, recent_days
        self.completion_clock = completion_clock
        self.links = InsightLinks(records, service, models, maintenance_guard=self._owns_run)
        self._execution_lock = RLock()

    def status(self, project_id):
        from .learning_events import COLLECTION
        state = self.records.read(COLLECTION, project_id)
        date = self.now().date().isoformat()
        run = self.records.read('v2_consolidation_runs', date)
        running = run is not None and run.payload.get('status') == 'running'
        live = running and (self.now() - timestamp(run.payload.get('started_at'), self.now())).total_seconds() < 600
        job = next((row for row in self.records.list('v2_consolidation_jobs')
                    if row.payload.get('date') == date and row.payload.get('project_id') == project_id
                    and row.payload.get('status') == 'running' and live), None)
        spent = run is not None and run.payload.get('status') in {'completed', 'partial'}
        return {'score': state.payload['score'] if state else 0, 'limit': spent or bool(live and job is None),
                'running': job is not None, 'job_id': job.object_id if job else None}

    def request(self, project_id, submit):
        date = self.now().date().isoformat()
        with self.records.begin() as tx:
            prior = tx.read('v2_consolidation_runs', date)
            if prior is not None:
                live = prior.payload.get('status') == 'running' and (
                    self.now() - timestamp(prior.payload.get('started_at'), self.now())).total_seconds() < 600
                if live:
                    own = next((row for row in tx.list('v2_consolidation_jobs') if row.payload.get('date') == date
                                and row.payload.get('project_id') == project_id and row.payload.get('status') == 'running'), None)
                    if own:
                        return {'job_id': own.object_id}
                if live or prior.payload.get('status') in {'completed', 'partial'}:
                    raise HTTPException(409, 'consolidate_limit')
            now = self.now().isoformat()
            self._fence_jobs(tx, date)
            reservation = tx.put('v2_consolidation_runs', date, {'status': 'running', 'started_at': now},
                                 expected_revision=prior.revision if prior else 0)
            identity = 'consolidation-' + uuid4().hex
            tx.put('v2_consolidation_jobs', identity, {'date': date, 'project_id': project_id,
                'status': 'running', 'updated_at': now}, expected_revision=0)
            tx.commit()
        versions = versions_for_turn('memory.consolidate')
        def execute():
            with self._execution_lock, override(**versions):
                return get('consolidate')(self._run, project_id=project_id, reserved=(date, reservation, identity))
        submit(execute)
        return {'job_id': identity}

    def _fence_jobs(self, tx, date):
        for job in tx.list('v2_consolidation_jobs'):
            if job.payload.get('date') == date and job.payload.get('status') == 'running':
                tx.put('v2_consolidation_jobs', job.object_id,
                       {**job.payload, 'status': 'failed', 'updated_at': self.now().isoformat()}, expected_revision=job.revision)

    def _collect(self, project_id=None, *, outcome_documents=()):
        from .outcome_corrections import _root
        now = self.now()
        rows, projects = [], set()

        def edited_root(identity, project, document=None):
            document = document if document is not None else self.documents.read(identity)
            revision = document.get('revision') if document else None
            if type(revision) is not int or revision < 2:
                return False
            bound = _root(self.records, WorkScope('local-user', project), identity)
            return bound is not None and revision > bound.revisions['document_revision']

        def recent(created, usage=None):
            at = usage.payload.get("updated_at") if usage else created
            return (now - timestamp(at, now)).total_seconds() <= self.recent_days * 86400

        for doc in self.documents.list():
            project, identity = doc.get("project_id"), doc["id"]
            pref = self.records.read("v2_document_recall", identity)
            if (
                not project
                or identity in outcome_documents
                or (project_id is not None and project != project_id)
                or doc.get("status") == "archived"
                or is_private_project(self.records, project)
                or (pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user")
            ):
                continue
            if not recent(doc.get("created_at"), self.records.read("v2_usage_document", identity)):
                continue
            if edited_root(identity, project, doc):
                continue  # User edits remain feedback; they are not a new product birth.
            eid, revision = ensure_document_experience(self.documents, self.service, project, identity)
            experience = self.records.read("recognition_experiences", eid)
            text = self.documents.markdown(identity, revision=revision)
            summary = summary_of(text)[0]
            rows.append(
                Memory(
                    identity,
                    revision,
                    {"project_id": project, "content": summary or text},
                    "document",
                    (eid,),
                    (identity,),
                    ({"type": "experience", "id": eid, "revision": experience.revision},),
                )
            )
            projects.add(project)
        selected = set()
        for collection, kind in (("recognition_candidates", "candidate"), ("recognitions", "recognition")):
            for row in self.records.list(collection):
                p = row.payload
                project = p.get("project_id")
                if (
                    p.get("scope") != {"user_id": "local-user", "project_id": project}
                    or not project
                    or (project_id is not None and project != project_id)
                    or is_private_project(self.records, project)
                    or p.get("state") != ("pending" if kind == "candidate" else "active")
                ):
                    continue
                pref = (
                    self.records.read("recognition_recall_preferences", row.object_id)
                    if kind == "recognition"
                    else None
                )
                if pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user":
                    continue
                if kind == "recognition" and not self.links.eligible(project, row.object_id):
                    continue
                if recent(p.get("created_at"), self.records.read("v2_usage_insight", row.object_id)):
                    selected.add(row.object_id)
                    if kind == "recognition":
                        selected.update(n["other_id"] for n in self.links.neighbors(project, row.object_id))
                if kind == "candidate" and self.records.read("v2_candidate_merges", row.object_id):
                    continue
                experiences = tuple(p.get("source_experience_ids", []))
                docs = source_documents(
                    self.records, WorkScope("local-user", project), experiences, p.get("source_recognition_ids", [])
                )
                refs = []
                for eid in experiences:
                    experience = self.records.read("recognition_experiences", eid)
                    if experience:
                        refs.append({"type": "experience", "id": eid, "revision": experience.revision})
                        docs.update(
                            r["id"]
                            for r in experience.payload.get("provenance", {}).get("source_refs", [])
                            if r.get("type") == "document"
                        )
                if any(edited_root(identity, project) for identity in docs):
                    continue  # An indirect candidate/recognition cannot restage an edited Root.
                if kind == "recognition":
                    refs = [{"type": "recognition", "id": row.object_id, "revision": row.revision}]
                else:
                    refs.extend(
                        {"type": "recognition", "id": rid, "revision": revision}
                        for rid, revision in p.get("source_recognition_revisions", {}).items()
                    )
                memory = Memory(
                    row.object_id, row.revision, dict(p), kind, experiences, tuple(sorted(docs)), tuple(refs)
                )
                if not self._manually_forgotten_sources(memory, self.records):
                    rows.append(memory)
        # Connected objects and their documents participate even if older than the window.
        recent_documents = {r.object_id for r in rows if r.kind == "document"}
        rows = [
            r for r in rows if r.kind == "document" or r.object_id in selected or set(r.documents) & recent_documents
        ]
        return rows

    def _manually_forgotten_sources(self, row, reader):
        identities = set(row.payload.get("source_recognition_ids", []))
        pending = list(identities)
        while pending:
            identity = pending.pop()
            source = reader.read("recognitions", identity)
            pref = reader.read("recognition_recall_preferences", identity)
            if pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user":
                return True
            if source:
                for item in source.payload.get("source_recognition_ids", []):
                    if item not in identities:
                        identities.add(item)
                        pending.append(item)
            if len(identities) > 256:
                raise RecognitionConflict("consolidation_source_graph_too_large")
        for identity in row.documents:
            pref = reader.read("v2_document_recall", identity)
            doc = reader.read("documents", identity)
            if (
                (pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user")
                or not doc
                or doc.payload.get("status") == "archived"
            ):
                return True
        return False

    def _owns_run(self, reader):
        if hasattr(self, "_reservation"):
            date, record = self._reservation
            if reader.read("v2_consolidation_runs", date) != record:
                raise RecognitionConflict("consolidation_run_taken_over")

    def _renew(self):
        date, previous = self._reservation
        with self.records.begin() as tx:
            self._owns_run(tx)
            record = tx.put(
                "v2_consolidation_runs",
                date,
                {**previous.payload, "started_at": self.now().isoformat()},
                expected_revision=previous.revision,
            )
            tx.commit()
        self._reservation = date, record

    def _validate(self, rows):
        self._owns_run(self.records)
        for row in rows:
            project = row.payload["project_id"]
            if self._manually_forgotten_sources(row, self.records):
                raise RecognitionConflict("consolidation_manually_forgotten_source")
            if is_private_project(self.records, project):
                raise RecognitionConflict("consolidation_private_project")
            if row.kind == "document":
                doc = self.documents.read(row.object_id)
                if not doc or doc["revision"] != row.revision or doc.get("status") == "archived":
                    raise RecognitionConflict("consolidation_document_changed")
                pref = self.records.read("v2_document_recall", row.object_id)
            else:
                current = self.records.read(
                    "recognitions" if row.kind == "recognition" else "recognition_candidates", row.object_id
                )
                if not current or current.revision != row.revision or current.payload != row.payload:
                    raise RecognitionConflict("consolidation_input_changed")
                pref = self.records.read("recognition_recall_preferences", row.object_id)
            if pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user":
                raise RecognitionConflict("consolidation_manually_forgotten")

    def _groups(self, rows):
        remaining, groups = {r.object_id: r for r in rows}, []
        while remaining:
            anchor = remaining.pop(sorted(remaining)[0])
            group = [anchor]
            queue = [anchor]
            while queue:
                current = queue.pop()
                neighbors, threshold = self.links._rank(
                    current.payload["project_id"],
                    current,
                    list(remaining.values()),
                    source_refs=lambda row: list(row.refs),
                    validate_current=lambda: self._validate(rows),
                    limit=max(1, len(remaining)),
                )
                for row, score in neighbors:
                    if score >= threshold and compatible(current.payload["content"], row.payload["content"]):
                        remaining.pop(row.object_id, None)
                        group.append(row)
                        queue.append(row)
            groups.append(group)
        return groups

    def _merges(self, project, group):
        rows = [r for r in group if r.kind != "document" and not self.records.read("v2_insight_patterns", r.object_id)]
        created, used = 0, set()
        authority = RestructureProposalService(self.service)
        for index, a in enumerate(rows):
            for b in rows[index + 1 :]:
                if a.object_id in used or b.object_id in used:
                    continue
                texts = a.payload["content"], b.payload["content"]
                if similarity(*texts) < DUPLICATE_THRESHOLD or not compatible(*texts):
                    continue
                signature = sorted([[a.object_id, a.revision], [b.object_id, b.revision]])
                if any(
                    r.payload.get("step_metadata", {}).get("consolidation_inputs") == signature
                    for r in self.records.list("recognition_restructure_proposals")
                ):
                    continue
                snapshot = authority.capture(
                    scope=WorkScope("local-user", project),
                    recognition_ids=[a.object_id, b.object_id],
                    expected_revisions=dict(signature),
                    pending_output=True,
                )
                conditions = sorted(set(a.payload.get("conditions", [])) | set(b.payload.get("conditions", [])))
                with self.records.begin() as tx:
                    self._owns_run(tx)
                    authority.save_in_uow(
                        tx,
                        scope=WorkScope("local-user", project),
                        proposal_id="merge-" + uuid4().hex,
                        snapshot=snapshot,
                        operation="merge",
                        outputs=[
                            {
                                "content": texts[0],
                                "conditions": conditions,
                                "source_experience_ids": [r["id"] for r in snapshot["experiences"]],
                                "source_recognition_ids": [
                                    r["id"]
                                    for r in snapshot["recognitions"]
                                    if r["id"] not in snapshot["target_recognition_ids"]
                                ],
                            }
                        ],
                        reason="近期材料反复表达同一认识",
                        step_metadata={"source": "daily", "consolidation_inputs": signature},
                    )
                    tx.commit()
                used.update((a.object_id, b.object_id))
                created += 1
        return created

    def _pattern(self, project, group, *, events=(), use_corrections=False, outcome_events=(), review_events=()):
        from . import consolidation_events, signal_review_feedback
        legacy_events = events
        events = (*events, *outcome_events, *review_events)
        retained = {root['id']: root['revision'] for event in outcome_events for root in event['_roots']}
        docs = sorted({identity for r in group for identity in r.documents} |
                      {identity for event in legacy_events for identity in event['_documents']} | set(retained))
        if (len(docs) < 2 and not events) or self.models is None:
            return 0
        config = self.models.public().get("generation", {})
        remote = urlsplit(str(config.get("base_url", ""))).hostname not in {"localhost", "127.0.0.1", "::1"}
        if (
            not config.get("configured")
            or config.get("enabled") is False
            or (remote and not egress_allowed(self.records, self.models, project, "generation"))
        ):
            return 0
        source_docs = [self.documents.read(i) for i in docs]

        def manually_forgotten(identity):
            pref = self.records.read("v2_document_recall", identity)
            return pref and pref.payload.get("state") == "forgotten" and pref.payload.get("by", "user") == "user"

        if any(
            not d or d.get("project_id") != project or d.get("status") == "archived" or manually_forgotten(d["id"])
            for d in source_docs
        ):
            return 0
        signature = [[d["id"], retained.get(d['id'], d["revision"])] for d in source_docs]
        event_ids = sorted(event['event_id'] for event in events)
        def matches(row):
            return (row.payload.get('project_id') == project and row.payload.get('documents') == signature
                    and (not use_corrections or row.payload.get('event_ids', []) == event_ids))
        if any(
            matches(row)
            for row in self.records.list("v2_consolidation_inputs")
        ):
            return 0
        document_experiences = {i: ensure_document_experience(self.documents, self.service, project, i,
            **({'retained_revision': retained[i]} if i in retained else {}))[0] for i in docs}
        experiences = list(document_experiences.values())
        if legacy_events:
            experiences = sorted(set(experiences) | {ref['id'] for event in legacy_events for ref in event['_refs']
                                                    if ref['type'] == 'experience'})
        experiences = sorted(set(experiences) | {identity for event in review_events for identity in event['_source_experience_ids']})
        scope = WorkScope("local-user", project)
        refs = [
            {"type": "experience", "id": eid, "revision": self.records.read("recognition_experiences", eid).revision}
            for eid in experiences
        ]
        if use_corrections:
            refs = list({(ref['type'], ref['id'], ref['revision']): {key: ref[key] for key in ('type', 'id', 'revision')}
                for ref in [*refs, *(ref for event in (*legacy_events, *review_events) for ref in event['_refs'])]}.values())
        review_recognitions = sorted({ref['id'] for ref in refs if ref['type'] == 'recognition'}) if review_events else []
        egress_snapshot = SourceEgressService(self.records).snapshot(scope, refs) if refs else None
        if use_corrections:
            original_count = consolidation_events.original_count(self.records, egress_snapshot) if egress_snapshot else 0
            if not events and not get('consolidate')(None, operation='accept', kind='pattern', source_count=original_count):
                return 0
        # Only verified, genuinely parentless feedback can have no source packet.
        if not refs and not outcome_events:
            raise RecognitionConflict('consolidation_sources_unavailable')
        guard = generation_source_guard(SourceEgressService(self.records), self.models, scope, refs) if refs else None

        def validate():
            if guard:
                guard()
            elif self.models.public()['generation'] != config:
                raise RecognitionConflict('consolidation_configuration_changed')
            self._validate(group)
            if use_corrections:
                consolidation_events.validate(self.records, legacy_events)
            if outcome_events:
                consolidation_events.validate_consumer_outcomes(self.records, outcome_events,
                    project=project, now=self.now().isoformat())
            if review_events:
                signal_review_feedback.validate_review_corrections(self.records, review_events,
                    project=project, now=self.now().isoformat())
            if any(manually_forgotten(identity) for identity in docs):
                raise RecognitionConflict("consolidation_manually_forgotten")
            if remote and not egress_allowed(self.records, self.models, project, "generation"):
                raise RecognitionConflict("consolidation_remote_disabled")

        messages = [
            {
                "role": "system",
                "content": "从至少两份材料归纳共同规律，保留条件，不自动发布。"
                + CANDIDATE_SOURCE_CONSTRAINTS
                + '只返回JSON {"text":"不超过40字的认识","conditions":[]}。',
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"documents": [{"id": i, "text": self.documents.markdown(i)} for i in docs]}, ensure_ascii=False
                ),
            },
        ]
        key = [signature, [[event['event_id'], event['_row'].revision, event['_owner'].revision] for event in legacy_events]] if use_corrections else signature
        if outcome_events:
            key = [key, consolidation_events.outcome_key(outcome_events)]
        if review_events:
            key = [key, signal_review_feedback.review_key(review_events)]
        turn = MemoryTurn(self.records, self.models, kind="memory.consolidate", project=project,
            key=key, materials=[{**ref, "project_id": project} for ref in refs], validate=validate,
            **({'freeze_request': consolidation_events.freeze_adapter(legacy_events,
                **({'verified_events': outcome_events, 'review_events': review_events,
                    'project': project, 'now': self.now().isoformat()}
                   if outcome_events or review_events else {}))} if use_corrections else {}))
        if use_corrections:
            messages = get('consolidate')(None, operation='messages', text=turn.request['input']['text'],
                                           constraints=CANDIDATE_SOURCE_CONSTRAINTS)
        output, metadata = turn.generate(messages, response_model=LearningOutput if use_corrections else PatternOutput, max_tokens=1000)
        validate()
        if use_corrections and events:
            selected_events = get('consolidate')(None, operation='selected', events=events, event_ids=output.event_ids)
            if outcome_events:
                selected_refs = [ref for event in selected_events if event in (*legacy_events, *review_events) for ref in event['_refs']]
                selected_refs += [{'type': 'experience', 'id': document_experiences[root['id']],
                    'revision': self.records.read('recognition_experiences', document_experiences[root['id']]).revision}
                    for event in selected_events if event in outcome_events for root in event['_roots']]
                selected_refs = list({(ref['type'], ref['id'], ref['revision']):
                    {key: ref[key] for key in ('type', 'id', 'revision')} for ref in selected_refs}.values())
                selected_snapshot = SourceEgressService(self.records).snapshot(scope, selected_refs) if selected_refs else None
                original_count = consolidation_events.original_count(self.records, selected_snapshot) if selected_snapshot else 0
            else:
                original_count = consolidation_events.selected_source_count(self.records, project, selected_events)
        if use_corrections and not get('consolidate')(None, operation='accept', events=events, event_ids=output.event_ids,
            expected_ids=event_ids, kind=output.kind, source_count=original_count):
            raise RecognitionError('consolidation_learning_evidence_invalid')
        text = output.text.strip()
        if not text:
            raise RecognitionError("empty_pattern")
        targets = [
            r
            for r in self.records.list("recognitions")
            if self.links.eligible(project, r.object_id)
            and similarity(text, r.payload["content"]) >= DUPLICATE_THRESHOLD
            and compatible(text, r.payload["content"])
        ]
        if (outcome_events or review_events) and (not experiences or is_private_project(self.records, project)):
            # The existing relation owner needs source evidence in a public project.
            targets = []
        if review_events:
            # The original support owner accepts experiences and its target only.
            # Preserve other genuine recognition parents through a pending candidate.
            targets = [target for target in targets if set(review_recognitions) <= {target.object_id}]
        def commit():
            with self.records.begin() as tx:
                self._owns_run(tx)
                if egress_snapshot:
                    SourceEgressService(TransactionRecords(tx)).validate_snapshot(scope, egress_snapshot)
                if use_corrections:
                    consolidation_events.validate(TransactionRecords(tx), legacy_events)
                if outcome_events:
                    consolidation_events.validate_consumer_outcomes(TransactionRecords(tx), outcome_events,
                        project=project, now=self.now().isoformat())
                if review_events:
                    signal_review_feedback.validate_review_corrections(TransactionRecords(tx), review_events,
                        project=project, now=self.now().isoformat())
                for row in group:
                    if self._manually_forgotten_sources(row, tx):
                        raise RecognitionConflict("consolidation_manually_forgotten_source")
                    if (
                        row.kind != "document"
                        and tx.read(
                            "recognitions" if row.kind == "recognition" else "recognition_candidates", row.object_id
                        ).revision
                        != row.revision
                    ):
                        raise RecognitionConflict("consolidation_input_changed")
                if any(
                    matches(row)
                    for row in tx.list("v2_consolidation_inputs")
                ):
                    return 0
                # The transaction reader preserves source CAS checks in the domain write below.
                if targets:
                    target = sorted(targets, key=lambda r: r.object_id)[0]
                    if tx.read("recognitions", target.object_id) != target:
                        raise RecognitionConflict("consolidation_target_changed")
                    RelationProposalService(
                        TransactionRecords(tx), allow_persona=True, evidence_guard=evidence_guard
                    ).propose_evidence(
                        scope,
                        target.object_id,
                        experiences,
                        [{"id": d["id"], "revision": retained.get(d['id'], d["revision"])} for d in source_docs],
                        text,
                        conditions=output.conditions,
                        snapshots=[
                            {"project_id": project, "snapshot": egress_snapshot},
                            {
                                "project_id": target.payload["project_id"],
                                "snapshot": SourceEgressService(TransactionRecords(tx)).snapshot(
                                    WorkScope("local-user", target.payload["project_id"]),
                                    [{"type": "recognition", "id": target.object_id, "revision": target.revision}],
                                ),
                            },
                        ],
                    )
                else:
                    candidate = self.service.__class__(TransactionRecords(tx)).propose(
                        scope=scope,
                        content=text,
                        conditions=output.conditions,
                        source_experience_ids=experiences,
                        **({'source_recognition_ids': review_recognitions} if review_events else {}),
                        candidate_id="pattern-" + turn.turn_id,
                        generation={
                            "id": metadata["generation_id"],
                            "step_version": "candidate-from-experiences-v1",
                            "model": metadata.get("model"),
                            "configuration_revision": metadata.get("configuration_revision"),
                            "completed_at": metadata["completed_at"],
                        },
                    )
                    tx.put(
                        "v2_insight_patterns",
                        candidate.id,
                        {
                            "kind": output.kind if use_corrections else "pattern",
                            "project_id": project,
                            "document_ids": docs,
                            "source_candidate_ids": [r.object_id for r in group if r.kind == "candidate"],
                            **({'event_ids': output.event_ids} if use_corrections else {}),
                        },
                        expected_revision=0,
                    )
                tx.put(
                    "v2_consolidation_inputs",
                    "input-" + uuid4().hex,
                    {"project_id": project, "documents": signature, **({'event_ids': event_ids} if use_corrections else {})},
                    expected_revision=0,
                )
                tx.commit()
            return 1
        def existing():
            return 1 if any(matches(r)
                for r in self.records.list("v2_consolidation_inputs")) else None
        return turn.propose(key="pattern", write=commit, existing=existing)

    def run(self, project_id=None):
        with self._execution_lock, override(**versions_for_turn('memory.consolidate')):
            return get('consolidate')(self._run, **({'project_id': project_id} if project_id is not None else {}))

    def _run(self, project_id=None, *, use_corrections=False, reserved=None):
        started, date = time.monotonic(), self.now().date().isoformat()
        prior = self.records.read("v2_consolidation_runs", date)

        def reusable(row):
            if row is None or row.payload.get("status") == "failed":
                return True
            return (
                row.payload.get("status") == "running"
                and (self.now() - timestamp(row.payload.get("started_at"), self.now())).total_seconds() >= 600
            )

        if reserved is None and not reusable(prior):
            return {"processed": 0, "new_suggestions": 0, "replayed": True}
        with self.records.begin() as tx:
            current = tx.read("v2_consolidation_runs", date)
            if reserved is not None:
                if reserved[0] != date or current != reserved[1]:
                    raise RecognitionConflict('consolidation_run_taken_over')
                reservation = current
            elif current != prior or not reusable(current):
                return {"processed": 0, "new_suggestions": 0, "replayed": True}
            else:
                if use_corrections:
                    self._fence_jobs(tx, date)
                reservation = tx.put(
                    "v2_consolidation_runs",
                    date,
                    {"status": "running", "started_at": self.now().isoformat()},
                    expected_revision=current.revision if current else 0,
                )
            tx.commit()
        self._reservation = date, reservation
        try:
            qualified, reviewed = {}, {}
            if use_corrections:
                from .consolidation_events import consumer_outcomes, OUTCOMES
                config = self.models.public().get('generation', {}) if self.models else {}
                local = bool(config.get('configured') and config.get('enabled') is not False and
                    urlsplit(str(config.get('base_url', ''))).hostname in {'localhost', '127.0.0.1', '::1'})
                projects = {row.payload.get('project_id') for row in self.records.list(OUTCOMES)
                    if row.payload.get('project_id') and (project_id is None or row.payload['project_id'] == project_id)}
                qualified = {project: consumer_outcomes(self.records, project,
                    now=self.now().isoformat(), local_only=local) for project in projects}
                from .learning_events import _signal_decision_project
                from .signal_review_feedback import review_corrections
                review_projects = {_signal_decision_project(self.records, row.payload)
                    for row in self.records.list('v2_signal_decisions')}
                reviewed = {project: review_corrections(self.records, project,
                    now=self.now().isoformat(), local_only=local) for project in review_projects
                    if project and (project_id is None or project == project_id)}
            outcome_documents = {root['id'] for events in qualified.values() for event in events for root in event['_roots']}
            rows = self._collect(project_id, outcome_documents=outcome_documents)
            new, failed = 0, 0
            event_projects = {row.payload.get('project_id') for row in self.records.list('v2_correction_events')
                              if row.payload.get('project_id') and (project_id is None or row.payload['project_id'] == project_id)} | {
                                  project for project, events in (*qualified.items(), *reviewed.items()) if events} if use_corrections else set()
            for project in sorted({r.payload["project_id"] for r in rows} | event_projects):
                own = [r for r in rows if r.payload["project_id"] == project]
                for group in self._groups(own):
                    self._renew()
                    self._validate(group)
                    new += self._merges(project, group)
                    try:
                        new += self._pattern(project, group, use_corrections=use_corrections)
                    except Exception as error:
                        failed += 1
                        _LOGGER.warning("consolidation_pattern_failed exception_type=%s", type(error).__name__)
                if use_corrections:
                    from .consolidation_events import corrections
                    events = corrections(self.records, project)
                    outcome_events = qualified.get(project, ())
                    review_events = reviewed.get(project, ())
                    if events or outcome_events or review_events:
                        self._renew()
                        relevant = [row for row in own if set(row.documents) &
                                    {identity for event in events for identity in event['_documents']}]
                        try:
                            new += self._pattern(project, relevant, events=events, use_corrections=True,
                                outcome_events=outcome_events, review_events=review_events)
                        except Exception as error:
                            failed += 1
                            _LOGGER.warning('consolidation_correction_failed exception_type=%s', type(error).__name__)
                for row in own:
                    self._owns_run(self.records)
                    if row.kind == "recognition":
                        self.links.discover(project, row.object_id)
            from .overviews import ScopeOverviews
            overviews = ScopeOverviews(self.records, self.documents, self.models,
                                      now=self.now, validate_owner=self._owns_run)
            scopes = set()
            for document in (self.documents.list() if callable(getattr(self.models, "public", None))
                    and self.models.public().get('generation', {}).get('enabled') is not False else ()):
                project = document.get("project_id")
                if not project:
                    continue
                if project_id is not None and project != project_id:
                    continue
                scopes.add((project, None))
                assignment = self.records.read("v2_scene_assignments_document", document["id"])
                if assignment is None:
                    item = next((row for row in self.records.list("workspace_items")
                                 if row.payload.get("document_id") == document["id"] and row.payload.get("status") == "confirmed"), None)
                    assignment = self.records.read("v2_scene_assignments_item", item.object_id) if item else None
                if assignment and assignment.payload.get("project_id") == project:
                    scopes.add((project, assignment.payload["scene"]))
            for project, scene in sorted(scopes, key=lambda value: (value[0], value[1] or "")):
                self._renew()
                try:
                    overviews.update(project, scene)
                except Exception as error:
                    failed += 1
                    _LOGGER.warning("consolidation_overview_failed exception_type=%s", type(error).__name__)
        except Exception as error:
            with self.records.begin() as tx:
                current = tx.read("v2_consolidation_runs", date)
                if current == self._reservation[1]:
                    tx.put(
                        "v2_consolidation_runs",
                        date,
                        {"status": "failed", "exception_type": type(error).__name__},
                        expected_revision=current.revision,
                    )
                    if reserved is not None:
                        job = tx.read('v2_consolidation_jobs', reserved[2])
                        tx.put('v2_consolidation_jobs', job.object_id,
                               {**job.payload, 'status': 'failed', 'updated_at': self.now().isoformat()},
                               expected_revision=job.revision)
                    tx.commit()
            raise
        payload = {
            "processed": len(rows),
            "new_suggestions": new,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "failed_groups": failed,
            "status": "partial" if failed else "completed",
        }
        with self.records.begin() as tx:
            if tx.read("v2_consolidation_runs", date) != self._reservation[1]:
                raise RecognitionConflict("consolidation_run_changed")
            tx.put("v2_consolidation_runs", date, payload, expected_revision=self._reservation[1].revision)
            if reserved is not None:
                job = tx.read('v2_consolidation_jobs', reserved[2])
                tx.put('v2_consolidation_jobs', job.object_id,
                       {**job.payload, 'status': payload['status'], 'updated_at': self.now().isoformat()},
                       expected_revision=job.revision)
            if new:
                tx.put(
                    "v2_activity",
                    "activity-" + uuid4().hex,
                    {"kind": "consolidate", "by": "auto", "created_at": self.now().isoformat(), **payload},
                    expected_revision=0,
                )
            if use_corrections and not failed:
                from .learning_events import complete_in_transaction
                clock_sample = self.completion_clock() if self.completion_clock is not None else None
                for project in sorted({row.payload['project_id'] for row in rows} | event_projects | ({project_id} if project_id else set())):
                    complete_in_transaction(tx, project, clock_sample=clock_sample)
            tx.commit()
        return payload


def install_consolidation_routes(application, *, records, consolidation=None):
    router = APIRouter(prefix="/api/v2/library")
    service = RelationProposalService(records, allow_persona=True, evidence_guard=evidence_guard)

    @router.get('/consolidate')
    def status(project_id: str):
        return consolidation.status(_project(project_id))

    @router.post('/consolidate')
    async def consolidate(request: Request, background: BackgroundTasks):
        body = await _json(request)
        if set(body) != {'project_id'}:
            raise HTTPException(400, 'invalid_consolidation_request')
        return consolidation.request(_project(body['project_id']), background.add_task)

    @router.get("/insights/{identity}/evidence-support")
    async def supports(identity: str, project_id: str):
        return {"items": service.list_evidence(WorkScope("local-user", _project(project_id)), identity)}

    @router.post("/evidence-support/{identity}/{action}")
    async def review(identity: str, action: str, request: Request):
        body = await _json(request)
        if (
            action not in {"accept", "dismiss"}
            or set(body) != {"project_id", "expected_revision"}
            or type(body["expected_revision"]) is not int
        ):
            raise HTTPException(400, "invalid_support_review")
        try:
            return service.review_evidence(
                WorkScope("local-user", _project(body["project_id"])),
                identity,
                body["expected_revision"],
                action == "accept",
            )
        except RecognitionError as error:
            raise HTTPException(409, "support_changed") from error

    application.include_router(router)
