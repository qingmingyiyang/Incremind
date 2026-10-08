"""Offline retrieval and synthetic extraction checks; never loads runtime or sends network requests."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime
import json
import sqlite3
import subprocess
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

EVALUATION_TIME = "2026-10-03T00:00:00+00:00"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.recall_preferences import set_preference
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.memory_app.v2.followup import bound_history, condense
from backend.memory_app.v2.policies import override, parse_overrides
from backend.memory_app.v2.projects import assign_scene
from backend.recognition import RecognitionService, WorkScope
from backend.shared.llm.litellm_gateway import _estimate_input_tokens
from core.document_engine import SQLiteDocumentRepository
from core.document_engine.ports import DocumentDraft
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


class NoModels:
    def __init__(self):
        self.attempts = 0

    def complete(self, *args, **kwargs):
        self.attempts += 1
        raise AssertionError("Retrieval evaluation must not call a model")

    def public(self):
        return {}


def score_selection(item, chosen, names, coverage):
    """Score retrieval objects and evidence, never generated-answer quality."""
    selected = {names.get(c["id"], c["id"]) for c in chosen}
    expected = set(item["expected_ids"])
    forbidden = set(item.get("forbidden_ids", []))
    hit = bool(selected & expected)
    if item.get("require_all") or item["category"] == "multi_source":
        hit = bool(expected) and expected <= selected
    if item['category'] == 'freshness':
        hit = hit and bool(chosen) and names.get(chosen[0]['id'], chosen[0]['id']) == item['expected_first_id']
    if forbidden & selected:
        hit = False
    for identity, answer in item.get("expected_evidence", {}).items():
        hit = hit and any(names.get(c["id"], c["id"]) == identity
                         and answer in c["excerpt"] for c in chosen)
    if item["category"] == "long_document":
        hit = hit and any(names.get(c["id"], c["id"]) in expected
                         and item["answer_text"] in c["excerpt"] for c in chosen)
    abstention = item["category"] == "abstention"
    if abstention:
        hit = item.get("should_abstain") is True and (not chosen or coverage < .6)
    return {"selected_ids": sorted(selected), "hit": hit,
            "coverage": coverage, "recall_count": len(chosen),
            "citation_correct_count": len((selected & expected) - forbidden),
            "citation_count": len(selected),
            "should_abstain": item.get("should_abstain", False),
            "false_recall": bool(abstention and chosen and coverage >= .6)}


def seed(root, fixture, *, forgetting_mode="manual"):
    """Construct published synthetic objects through existing domain boundaries."""
    # Source experience timestamps are rendered in actual retrieval excerpts.
    # Freeze their fixture clock as well, without rewriting returned evidence.
    with patch("backend.recognition.service._now", return_value=EVALUATION_TIME):
        return _seed(root, fixture, forgetting_mode=forgetting_mode)


def _seed(root, fixture, *, forgetting_mode):
    records = SQLiteStructuredRecordStore(root / "records.sqlite3")
    documents = SQLiteDocumentRepository(records)
    service = RecognitionService(records)
    sources = JsonObjectStore(root / ".rebuild-data")
    identities = {}
    for item in fixture["documents"]:
        documents.now = item["created_at"]
        sources.write("sources", item["source_id"], {
            "id": item["source_id"], "title": item["title"], "project_id": item["project_id"],
            "metadata": {"content": item["original"]}, "created_at": item["created_at"],
        }, expected_revision=0)
        doc = documents.create(DocumentDraft(title=item["title"], document_type="legacy-material",
            markdown=f'# {item["title"]}\n\n## 摘要\n{item["summary"]}\n\n## 正文\n{item["body"]}',
            source_refs=({"source_id": item["source_id"], "locator": "text:0"},),
            project_id=item["project_id"]))
        identities[item["id"]] = doc["id"]
        # Synthetic publication marker: no production application state is read.
        with records.begin() as tx:
            review_id = "review-" + item["source_id"]
            tx.put("workspace_review_intents", review_id, {
                "id": review_id, "source_id": item["source_id"],
                "document_id": doc["id"], "project_id": item["project_id"], "state": "confirmed",
                "document_revision": doc["revision"], "expected_document_id": None,
                "expected_document_revision": None, "confirmed_markdown": documents.markdown(doc["id"]),
            }, expected_revision=0)
            tx.commit()
        if item.get("scene") is not None:
            assign_scene(records, "document", doc["id"], item["project_id"], item["scene"])
    for item in fixture["insights"]:
        scope = WorkScope("local-user", item["project_id"])
        if item.get("document_id"):
            experience, _ = ensure_document_experience(documents, service, scope.project_id,
                                                       identities[item["document_id"]])
        else:
            experience = service.stage_experience(scope=scope, content=item["text"],
                                                  experience_id="experience-" + item["id"])
        candidate = service.propose(scope=scope, content=item["text"],
            conditions=item.get('conditions', ()),
            source_experience_ids=[experience], candidate_id="candidate-" + item["id"])
        # Freeze only the fixture's event clock; publish and its history are real.
        with patch("backend.recognition.service._now", return_value=item.get("created_at", EVALUATION_TIME)):
            recognition = service.publish(scope=scope, candidate_id=candidate.id,
                expected_revision=1, reviewer="local-user", recognition_id=item["id"])
        if item.get("scene") is not None:
            assign_scene(records, "recognition", recognition.id, item["project_id"], item["scene"])
        if item.get("forgotten"):
            set_preference(records, scope, recognition.id, recognition_revision=recognition.revision,
                           preference_revision=0, state="forgotten")
        if item.get("forgotten") and forgetting_mode == "auto-forgotten":
            with records.begin() as tx:
                row = tx.read("recognition_recall_preferences", recognition.id)
                tx.put("recognition_recall_preferences", recognition.id, {**row.payload, "by": "auto"}, expected_revision=row.revision)
                tx.commit()
        identities[item["id"]] = recognition.id
    # Domain seeding bypasses the HTTP confirmation hook: replay its derived, model-free links.
    from backend.memory_app.v2.links import InsightLinks
    links = InsightLinks(records, service)
    for item in fixture["insights"]:
        links.discover(item["project_id"], item["id"])
    for item in fixture["insights"]:
        if item.get("confirmed_supersedes"):
            with patch("backend.memory_app.v2.links.now", return_value=item["created_at"]), \
                 patch("backend.memory_app.relations._now", return_value=item["created_at"]):
                proposal = links.propose(item["project_id"], item["id"], item["supersedes"],
                                         "supersedes", "Synthetic user-confirmed update")
                links.review(item["project_id"], proposal["id"], proposal["revision"], True)
    from backend.memory_app.v2.consolidation import Consolidation
    Consolidation(records, service, documents, now=lambda: datetime.fromisoformat(EVALUATION_TIME)).run()
    query = WorkspaceQuery(records, documents, sources, NoModels(), service)
    if fixture.get('inspirations'):
        _seed_inspirations(query, fixture['inspirations'], identities)
    return query, identities


def _seed_inspirations(query, inspirations, identities):
    """合成原话通过真实工作台捕获，候选正文不充当检索证据。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from backend.memory_app.v2.workbench import install_workbench_routes
    app = FastAPI()
    install_workbench_routes(app, records=query.records, models=query.models,
        documents=query.documents, service=query.service, workspace=SimpleNamespace(query=query))
    with TestClient(app) as client:
        for item in inspirations:
            response = client.post('/api/v2/workbench/turns', json={
                'project_id': 'eval-inspiration', 'intent': 'inspiration', 'text': item['text']})
            if response.status_code != 200:
                raise AssertionError('synthetic inspiration capture failed')
            candidate = response.json()['turn']['receipt']['inspiration']['insight']['id']
            row = query.records.read('recognition_candidates', candidate)
            identities[item['id']] = row.payload['source_experience_ids'][0]


def _evaluate_gap_cases(cases):
    """用选定的已登记策略运行固定合成对照。"""
    from backend.memory_app.v2.policies import get, version
    try:
        selected = version('gap')
    except ValueError as error:
        if str(error) != 'unknown_policy_interface':
            raise
        selected = None
    policy = get('gap', version=selected) if selected is not None else None
    rows = []
    for case in cases:
        operation, given = case['operation'], case['input']
        if operation not in {'insufficient', 'related', 'selection'}:
            raise ValueError('unknown_gap_evaluation_operation')
        actual = None
        if policy is not None:
            if operation == 'insufficient':
                actual = policy.insufficient(given['answer'])
            elif operation == 'related':
                actual = policy.related(given['first'], given['second'])
            else:
                actual = [item['id'] for item in policy(given['items'],
                    now=datetime.fromisoformat(given['now']),
                    dismissed=given.get('dismissed', ()), enabled=given.get('enabled', True))]
        rows.append({'id': case['id'], 'category': 'gap_policy', 'operation': operation,
            'policy_version': selected, 'expected': case['expected'], 'actual': actual,
            'hit': (type(actual) is type(case['expected']) and actual == case['expected'])
                   if policy is not None else None})
    return rows


def _gap_policy_metrics(rows):
    selected = rows[0]['policy_version']
    def measure(group):
        count = len(group)
        hits = sum(row['hit'] for row in group) if selected is not None else None
        return {'total': count, 'evaluated': count if selected is not None else 0,
                'hits': hits, 'accuracy': round(hits / count, 6) if hits is not None else None}
    return {'state': 'enabled' if selected is not None else 'disabled',
        'policy_version': selected, 'metric_kind': 'pure_policy_protocol_controls',
        **measure(rows), 'by_operation': {operation: measure([
            row for row in rows if row['operation'] == operation])
            for operation in sorted({row['operation'] for row in rows})}}


def _vector_protocol_control(operation, given):
    """只隔离第三方模型，真实执行前缀、输入协议和队列容量。"""
    from threading import Event
    from backend.memory_app.local_vectors import SentenceEncoder, VectorWorker, LocalVectorError, validate_request
    from backend.memory_app.v2.embedding_settings import vector_policy
    policy = vector_policy()
    if operation == 'request':
        try:
            texts, kind = validate_request(given, policy=policy)
        except LocalVectorError as error:
            return {'error': str(error)}
        return {'accepted': True, 'items': len(texts), 'input_type': kind}
    if operation == 'encode':
        observed = {}
        class ModelBoundary:
            tokenizer = type('TokenizerBoundary', (), {'encode': staticmethod(lambda text: [0])})()
            def encode(self, texts, **options):
                observed.update(inputs=list(texts), batch_size=options['batch_size'],
                    normalize_embeddings=options['normalize_embeddings'])
                return [[0.0] * options['truncate_dim'] for _ in texts]
        encoder = object.__new__(SentenceEncoder)
        encoder.model = ModelBoundary()
        vectors, _ = encoder.encode(given['texts'], given['input_type'], policy=policy)
        observed['dims'] = len(vectors[0])
        return observed
    if operation == 'queue':
        entered, release = Event(), Event()
        class ModelBoundary:
            def encode(self, texts, kind, *, policy):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('vector_evaluation_release_missing')
                return [[0.0] * 256 for _ in texts], len(texts)
        worker = VectorWorker(ROOT / 'work', policy=policy, loader=lambda directory: ModelBoundary())
        accepted, failure = 0, None
        try:
            for index in range(given['items']):
                try:
                    worker.submit(['合成容量输入'])
                except LocalVectorError as error:
                    failure = str(error)
                    break
                accepted += 1
                if index == 0 and not entered.wait(5):
                    raise RuntimeError('vector_evaluation_worker_not_started')
        finally:
            release.set()
            worker.close()
        return {'accepted': accepted, 'error': failure,
            'closed': worker.closed and not worker.thread.is_alive() and worker.pending == 0}
    raise ValueError('unknown_vector_evaluation_operation')


def _evaluate_vector_cases(cases):
    from backend.memory_app.v2.policies import get, version
    try:
        selected = version('vector')
    except ValueError as error:
        if str(error) != 'unknown_policy_interface':
            raise
        selected = None
    # 默认激活或显式覆盖都会消费登记实现；未激活接口没有编码或队列工作。
    if selected is not None:
        get('vector')()
    rows = []
    for case in cases:
        actual = (_vector_protocol_control(case['operation'], case['input'])
            if selected is not None else None)
        rows.append({'id': case['id'], 'category': 'vector_policy', 'operation': case['operation'],
            'policy_version': selected, 'expected': case['expected'], 'actual': actual,
            'hit': (type(actual) is type(case['expected']) and actual == case['expected'])
                if selected is not None else None})
    return rows


def evaluate(fixture_path=None, *, forgetting_mode="manual"):
    if forgetting_mode not in {"manual", "auto-forgotten"}:
        raise ValueError("invalid forgetting mode")
    fixture_path = fixture_path or ROOT / "tests/fixtures/memory_eval/corpus.json"
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    if 'base_corpus' in fixture:
        if fixture['base_corpus'] != 'corpus.json':
            raise ValueError('base_corpus must reference the adjacent corpus.json')
        base = json.loads(Path(fixture_path).with_name('corpus.json').read_text(encoding='utf-8'))
        fixture = {**base, **fixture}
        if fixture.get('inspiration_questions'):
            fixture['questions'] = [*base['questions'], *fixture['inspiration_questions']]
    freshness = fixture.get('freshness', {})
    fixture = {**fixture, 'insights': [*fixture['insights'], *freshness.get('insights', [])],
               'questions': [*fixture['questions'], *freshness.get('questions', [])]}
    with TemporaryDirectory(prefix="chriptmas-memory-eval-") as directory, patch(
        'backend.memory_app.v2.insight_validity.now', return_value=EVALUATION_TIME
    ), closing(
        sqlite3.connect(Path(directory) / "records.sqlite3")
    ) as keeper:
        # Keep the synthetic database's WAL alive between real domain reads.
        # No transaction or cached records are retained; closing runs before
        # TemporaryDirectory cleanup and includes the final checkpoint cost.
        keeper.execute("PRAGMA journal_mode=WAL").fetchone()
        SQLiteStructuredRecordStore(Path(directory) / "records.sqlite3").read("evaluation", "absent")
        keeper.execute("SELECT name FROM sqlite_schema").fetchall()
        query, identities = seed(Path(directory), fixture, forgetting_mode=forgetting_mode)
        if fixture.get('elsewhere'):
            with query.records.begin() as tx:
                for project in fixture['elsewhere']['projects']:
                    tx.put('v2_projects', project['id'], {'name': project['name'],
                        'scenes': project['scenes'], 'builtin': None}, expected_revision=0)
                tx.commit()
        names = {actual: logical for logical, actual in identities.items()}
        rows, calibration = [], []
        for item in [*fixture["questions"], *fixture.get('elsewhere', {}).get('questions', [])]:
            # Exercise production history budgeting; this unconfigured adapter explicitly
            # skips model rewriting, so the offline scores never simulate language quality.
            _, budget, overhead = query.ask_budget()
            history = bound_history(item.get("history", []), item["question"], budget=budget, prompt_overhead=overhead)
            rewritten = asyncio.run(condense(query, item["project_id"], item["question"], history, turn_id=item["id"]))
            from backend.memory_app.v2.multi_query import expand_plan
            retrieval_question = rewritten["question"] or item["question"]
            scene = item.get("scene")
            collected = query.collect_candidates(item["project_id"], retrieval_question, scene=scene)
            plan = query.prepare_ask(item["project_id"], item["question"], collected=collected,
                retrieval_question=retrieval_question, history=history["text"], scene=scene)
            plan, _, _ = asyncio.run(expand_plan(query, item["project_id"], retrieval_question, plan, collected, turn_id=item["id"], scene=scene))
            from backend.memory_app.v2.bookshelf import consult_bookshelf
            consult_bookshelf(query, item["project_id"], retrieval_question, plan, scene=scene)
            tokens = (_estimate_input_tokens([{"role": "user", "content":
                "\n\n".join(c["excerpt"] for c in plan["chosen"])}]) if plan["chosen"] else 0)
            rows.append({"id": item["id"], "category": item["category"],
                "expected_ids": item["expected_ids"],
                "forbidden_ids": item.get("forbidden_ids", []),
                "expected_evidence": item.get("expected_evidence", {}),
                **score_selection(item, plan["chosen"], names, plan["trace"][-1]["coverage"]),
                "selected_evidence": [{"id": names.get(c["id"], c["id"]),
                                       "layer": c["layer"], "excerpt": c["excerpt"],
                                       **({'stale': True} if c.get('stale') is True else {})} for c in plan["chosen"]],
                "stop_layer": plan["trace"][-1]["layer"],
                "sufficient": plan["trace"][-1]["stopped"], "estimated_tokens": tokens})
            if item['category'] == 'elsewhere':
                from backend.memory_app.v2.elsewhere import suggest_elsewhere
                hint = suggest_elsewhere(query, item['project_id'], item['question'],
                    coverage=plan['trace'][-1]['coverage'], has_hits=bool(plan['chosen']),
                    tagged=item.get('tagged', False))
                expected = item['expected_elsewhere']
                rows[-1].update(elsewhere=hint, expected_elsewhere=expected,
                    partition=item['partition'], hit=hint == expected,
                    false_suggestion=expected is None and hint is not None)
                if item['partition'] == 'calibration':
                    calibration.append({'input': {'project_id': item['project_id'], 'question': item['question'],
                        'coverage': plan['trace'][-1]['coverage'], 'has_hits': bool(plan['chosen']),
                        'tagged': item.get('tagged', False)}, 'expected': expected})
        assert query.models.attempts == 0, "Offline evaluation attempted model access"
        if fixture.get('comparative_extraction'):
            if __package__:
                from .memory_extract_eval import evaluate_cases
            else:
                from memory_extract_eval import evaluate_cases
            rows.extend(evaluate_cases(Path(directory) / 'comparative', fixture['comparative_extraction'], seed))
        if fixture.get('comment_extraction'):
            if __package__:
                from .memory_extract_eval import evaluate_comment_cases
            else:
                from memory_extract_eval import evaluate_comment_cases
            rows.extend(evaluate_comment_cases(Path(directory) / 'comments', fixture['comment_extraction'], seed))
        if fixture.get('gap_cases'):
            rows.extend(_evaluate_gap_cases(fixture['gap_cases']))
        if fixture.get('vector_cases'):
            rows.extend(_evaluate_vector_cases(fixture['vector_cases']))
    categories = {}
    for category in sorted({row["category"] for row in rows}):
        group = [row for row in rows if row["category"] == category]
        if category in {'gap_policy', 'vector_policy'}:
            categories[category] = _gap_policy_metrics(group)
            continue
        hits = sum(row["hit"] for row in group)
        if category in {'comparative_extraction', 'comment_supplement'}:
            categories[category] = {'total': len(group), 'hits': hits,
                'hit_rate': round(hits / len(group), 6),
                'synthetic_model_attempts': sum(row['synthetic_model_attempts'] for row in group),
                'remote_model_attempts': 0,
                'average_prompt_tokens': round(sum(row['estimated_prompt_tokens'] for row in group) / len(group), 3),
                'average_completion_tokens': round(sum(row['estimated_completion_tokens'] for row in group) / len(group), 3)}
            if category == 'comment_supplement':
                categories[category]['synthetic_capture_model_attempts'] = sum(row['synthetic_capture_model_attempts'] for row in group)
                categories[category]['capture_http_requests'] = sum(row['capture_http_requests'] for row in group)
            continue
        categories[category] = {"total": len(group), "hits": hits,
            "hit_rate": round(hits / len(group), 6),
            "citation_correct_count": sum(row["citation_correct_count"] for row in group),
            "citation_count": sum(row["citation_count"] for row in group),
            "citation_correct_rate": round(sum(row["citation_correct_count"] for row in group) /
                sum(row["citation_count"] for row in group), 6) if sum(row["citation_count"] for row in group) else None,
            "average_recall_count": round(sum(row["recall_count"] for row in group) / len(group), 3),
            "stop_layers": dict(sorted(Counter(row["stop_layer"] for row in group).items())),
            "average_tokens": round(sum(row["estimated_tokens"] for row in group) / len(group), 3)}
        if category == "abstention":
            categories[category]["false_recalls"] = sum(row["false_recall"] for row in group)
            categories[category]["false_recall_rate"] = round(categories[category]["false_recalls"] / len(group), 6)
        if category == 'elsewhere':
            from backend.memory_app.v2.policies import get
            from backend.memory_app.v2.links import similarity
            calibrated = get('elsewhere', version='@1')(operation='calibrate', calibration=calibration,
                score=similarity, candidates=[{'project_id': project['id'], 'scene': scene,
                    'texts': (project['name'], scene or '')} for project in fixture['elsewhere']['projects']
                    for scene in (None, *project['scenes'])])
            negative = [row for row in group if row['expected_elsewhere'] is None]
            categories[category].update(hint_accuracy=round(hits / len(group), 6),
                calibration=calibrated,
                false_suggestion_rate=round(sum(row['false_suggestion'] for row in negative) / len(negative), 6),
                held_out_wrong_place_total=sum(row['partition'] == 'held_out' and row['expected_elsewhere'] is not None for row in group),
                partitions={key: {'total': len(part), 'correct': sum(row['hit'] for row in part)}
                    for key in ('calibration', 'held_out')
                    for part in [[row for row in group if row['partition'] == key]]})
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    return {"schema_version": 2, "repository_revision": revision,
        "forgetting_mode": forgetting_mode, "token_measure": "gateway UTF-8 estimate of selected evidence",
        "evaluation_time": EVALUATION_TIME, "model_attempts": query.models.attempts,
        'synthetic_model_attempts': sum(row.get('synthetic_model_attempts', 0) for row in rows),
        **({'synthetic_capture_model_attempts': sum(row.get('synthetic_capture_model_attempts', 0) for row in rows)}
            if fixture.get('comment_extraction') else {}),
        'remote_model_attempts': 0,
        **({'inspiration_capture_http_requests': len(fixture['inspirations'])}
            if fixture.get('inspirations') else {}),
        "metric_definitions": {"hit_rate": "Legacy categories: any expected object; multi_source: all expected objects; every expected_evidence must appear in its object's selected excerpt; forbidden objects always invalidate; long_document: expected object's selected excerpt contains answer_text; abstention: empty selection or coverage < 0.6 and should_abstain",
            "citation_correct_rate": "Micro precision of unique selected retrieval object IDs, not generated-answer citation quality; null when no objects selected",
            "average_recall_count": "Mean selected evidence fragments, including multiple layers of one object",
            "false_recall_rate": "Abstention questions with nonempty evidence and coverage >= 0.6 divided by abstention questions",
            'comparative_extraction': 'Exact annotated differences, relation/destination/conditions and frozen neighbor binding through real services with synthetic completions; not model semantic judgment quality',
            **({'freshness': 'Current annotated evidence must lead the actual selected context and retain its exact answer facts; expired evidence may remain recalled and marked stale. Retrieval order only, not generated-answer quality.'}
                if freshness else {}),
            **({'gap_policy': 'Exact independently annotated insufficiency, lexical overlap and expiry/dismiss/off protocol controls through the registered policy; not service grouping; not model semantic quality'}
                if fixture.get('gap_cases') else {}),
            **({'vector_policy': 'Independently annotated production input, prefix, dimension, batch and queue protocol controls with a deterministic third-party model boundary; not model quality or Ask retrieval quality'}
                if fixture.get('vector_cases') else {}),
            **({'inspiration': 'Production source-window selection from real synthetic workbench captures; original experience identity and expected exact original wording, not generated-answer quality'}
                if fixture.get('inspirations') else {}),
            **({'comment_supplement': 'Exact independently annotated pending candidates and codepoint quote proofs after real capture/admission, frozen request and owner matching; synthetic completions compare protocol capabilities, not model semantic judgment quality'}
                if fixture.get('comment_extraction') else {}),
            **({'elsewhere': 'Exact annotated project/scene navigation after the real retrieval ladder; false suggestions divided by negative cases. Calibration and held-out partitions are distinct, lexical only, zero model calls.'}
                if fixture.get('elsewhere') else {})},
        "fixture_counts": {"documents": len(fixture["documents"]), "sources": len(fixture["documents"]),
                           "insights": len(fixture["insights"]), "questions": len(rows)},
        "categories": categories, "questions": rows}


def _selection_keys(chosen):
    from backend.memory_app.v2.workbench import _ask_receipt
    sources = [{'number':index,'title':'','excerpt':'','coordinate_space':'','windows':[]}
               for index in range(1, len(chosen)+1)]
    projected = _ask_receipt({'trace':[]}, chosen, {'answer':'','sources':sources}, None)
    return [(entry['layer'], entry['id']) for entry in projected['citations']]


def _source_binding(candidate, identity):
    entry = candidate.get('entry', {})
    space = candidate.get('coordinate_space')
    if (space == 'source_content_v1' and entry.get('kind') == 'source'
            and entry.get('source_id') == identity):
        return 'source'
    if (space == 'workspace_source_text_v1' and entry.get('kind') == 'document'
            and entry.get('item_id') == identity):
        return 'original'
    return None


def _unused_source_bindings(opened, clock, cutoff):
    from backend.memory_app.v2.budget import _source_texts
    from backend.memory_app.v2.signals import _fact_time
    bindings = {}
    for group in opened.groups:
        turn = opened.records.read('v2_turns', group['turn_id'])
        if turn is None or turn.payload.get('project_id') != group.get('project_id'):
            continue
        value = turn.payload
        at = _fact_time(value.get('created_at'))
        if value.get('by') == 'admin' or at is None or at > clock or cutoff is not None and at <= cutoff:
            continue
        answer = group.get('answer') or {}
        chosen = answer.get('chosen')
        context = answer.get('receipt',{}).get('ask',{}).get('context') or {}
        targets = {(entry.get('layer'),entry.get('id')) for entry in context.get('entries',[])
                   if isinstance(entry,dict) and entry.get('layer') == 'source' and isinstance(entry.get('id'),str)}
        frozen = opened.answer_input(group['turn_id'])
        messages = frozen.get('messages',[]) if isinstance(frozen,dict) else []
        actual = any(call.get('turn_id') == group['turn_id'] and call.get('model_call_purpose') == 'primary'
                     and call.get('status') == 'completed' for call in group.get('calls',[]))
        proven = {}
        receipt = answer.get('receipt',{}).get('ask',{})
        egress_id = receipt.get('egress_receipt_id')
        egress = opened.records.read('workspace_ask_receipts',egress_id) if isinstance(egress_id,str) else None
        sources = egress.payload.get('sources') if egress is not None else None
        ordered_context = [entry for entry in context.get('entries',[]) if isinstance(entry,dict) and not entry.get('persona')]
        linked = (egress is not None and egress.payload.get('id') == egress_id
                  and egress.payload.get('project_id') == group['project_id']
                  and egress.payload.get('status') == 'completed' and isinstance(sources,list))
        if actual and linked and isinstance(chosen,list) and len(ordered_context) == len(chosen) and len(sources) >= len(chosen):
            try:
                keys = _selection_keys(chosen)
                pairs = [(entry.get('layer'),entry.get('id')) for entry in ordered_context]
                prefixes = _source_texts([{'id':identity,'title':entry['title'],'excerpt':''}
                                         for entry, (_,identity) in zip(ordered_context,keys)])
                # Ordered framing is supporting evidence. The primary completed call,
                # exact parent immutable input/result and linked completed egress are
                # the original owner chain proving transmission and source binding.
                def ordered(message):
                    text = message.get('content','')
                    position = 0
                    for prefix in prefixes:
                        found = text.find(prefix,position)
                        if found < 0:
                            return False
                        position = found + len(prefix)
                    return bool(prefixes)
                if pairs == keys and any(ordered(message) for message in messages if isinstance(message,dict)):
                    for candidate, identity, source in zip(chosen,keys,sources):
                        if identity[0] != 'source':
                            continue
                        entry = candidate['entry']
                        owner = None
                        revision = source.get('revision')
                        if (candidate.get('project_id') == group['project_id'] and source.get('id') == entry.get('id')
                                and type(revision) is int and revision > 0 and isinstance(source.get('windows'),list)
                                and source['windows']):
                            if (entry.get('source_id') == identity[1] and entry.get('item_id') is None
                                    and source.get('kind') == 'source' and source.get('coordinate_space') == 'source_content_v1'):
                                owner = 'source'
                            elif (entry.get('item_id') == identity[1]
                                    and source.get('kind') == 'document' and source.get('coordinate_space') == 'workspace_source_text_v1'
                                    and type(source.get('item_revision')) is int and source['item_revision'] > 0):
                                owner = 'original'
                        proven.setdefault(identity,set()).add(owner)
            except (KeyError,TypeError,ValueError,AttributeError):
                proven = {}
        for identity in targets | proven.keys():
            bindings.setdefault((group['project_id'],identity),set()).update(proven.get(identity,{None}))
    return bindings


def replay(root, *, policies, now=None, k=5):
    """Compare original local retrieval owners on a quiescent copied history."""
    from datetime import timedelta, timezone
    from tools.signal_report import open_signal_copy
    from backend.memory_app.v2.signals import SignalService, _fact_time
    if len(policies) != 2 or type(k) is not int or k < 1:
        raise ValueError('replay_two_policies_required')
    selections = [parse_overrides(values) for values in policies]
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError('replay_clock_invalid')
    with open_signal_copy(root) as opened:
        owner = SignalService(opened.records)
        settings = owner.settings()
        if not settings['enabled']:
            return {}
        cutoff = _fact_time(settings.get('cleared_at'))
        earliest = clock - timedelta(days=90)
        statistics = owner.report(kernel_groups=opened.groups, read_model_input=opened.answer_input)
        unused = statistics.get('unused', {}).get('objects', [])
        future_projects = {row.payload.get('project_id') for row in opened.records.list('v2_turns')
                           if isinstance(row.payload.get('receipt',{}).get('ask'),dict)
                           and row.payload.get('by') != 'admin'
                           and (at := _fact_time(row.payload.get('created_at'))) is not None
                           and at > clock and (cutoff is None or at > cutoff)}
        rows = []
        query = opened.query()
        source_bindings = _unused_source_bindings(opened, clock, cutoff)
        from backend.memory_app.v2.multi_query import expand_plan
        from backend.memory_app.v2.bookshelf import consult_bookshelf
        for turn in opened.records.list('v2_turns'):
            value = turn.payload
            created = _fact_time(value.get('created_at'))
            if (value.get('intent') != 'ask' or value.get('by') == 'admin' or created is None
                    or not earliest <= created <= clock or cutoff is not None and created <= cutoff
                    or not isinstance(value.get('user_text'), str) or not isinstance(value.get('project_id'), str)):
                continue
            project, question = value['project_id'], value['user_text']
            citations = value.get('receipt', {}).get('ask', {}).get('citations')
            valid_layers = {'insight', 'summary', 'note', 'source', 'persona'}
            cited = {(entry['layer'], entry['id']) for entry in citations
                     if isinstance(entry, dict) and entry.get('layer') in valid_layers
                     and isinstance(entry.get('id'), str)} if isinstance(citations, list) else None
            citation_spaces = {(entry.get('layer'),entry.get('id')):entry.get('locator',{}).get('coordinate_space')
                               for entry in citations or [] if isinstance(entry,dict)}
            proof = opened.answer_input(turn.object_id) is not None
            malformed_citations = isinstance(citations, list) and len(cited) != len(citations)
            struck = set()
            for feedback in opened.records.list('v2_context_feedback'):
                fact = feedback.payload
                at = _fact_time(fact.get('at'))
                if (fact.get('project_id') == project and fact.get('by') != 'admin' and at is not None
                        and fact.get('action') == 'strike' and fact.get('object_kind') == 'recognition'
                        and at <= clock and (cutoff is None or at > cutoff)
                        and isinstance(fact.get('object_id'), str)):
                    struck.add(('insight', fact['object_id']))
            unused_ids = {(entry['layer'], entry['object_id']) for entry in unused if entry['project_id'] == project
                          and entry['sent'] >= 5 and entry['cited'] == 0}
            if project in future_projects:
                unused_ids = set()
            variants = []
            for selected in selections:
                with override(**selected):
                    scene = value.get('scene')
                    history = bound_history([], question, budget=query.ask_budget()[1], prompt_overhead=query.ask_budget()[2])
                    rewritten = asyncio.run(condense(query, project, question, history, turn_id=turn.object_id))
                    retrieval_question = rewritten['question'] or question
                    collected = query.collect_candidates(project, retrieval_question, scene=scene)
                    plan = query.prepare_ask(project, question, collected=collected, retrieval_question=retrieval_question,
                                             history=history['text'], scene=scene)
                    plan, _, _ = asyncio.run(expand_plan(query, project, retrieval_question, plan, collected, turn_id=turn.object_id, scene=scene))
                    consult_bookshelf(query, project, retrieval_question, plan, scene=scene)
                    ranks = {identity:index for index, identity in enumerate(_selection_keys(plan['chosen']), 1)}
                    unavailable_objects = query.retrieval_index.unavailable
                    unavailable = len(unavailable_objects)
                    from backend.memory_app.v2.privacy import is_private_project
                    private = is_private_project(query.records, project)
                    unknown = private or unavailable > 0 and not ranks
                    def missing(identity):
                        layer, identity_id = identity
                        if layer == 'source':
                            space = citation_spaces.get(identity)
                            if space == 'source_content_v1':
                                return ('source',identity_id) in unavailable_objects
                            if space == 'workspace_source_text_v1':
                                return ('original',identity_id) in unavailable_objects
                            return True
                        kind = 'document' if layer in {'note', 'summary'} else 'recognition'
                        return (kind, identity_id) in unavailable_objects
                    citation_unknown = (len(cited or ()) if private or not proof else
                                        sum(missing(identity) for identity in cited or ()))
                    if malformed_citations or cited is None:
                        citation_unknown += 1
                    current_sources = {}
                    for candidate, identity in zip(plan['chosen'], _selection_keys(plan['chosen'])):
                        if identity[0] == 'source':
                            current_sources.setdefault(identity,set()).add(_source_binding(candidate,identity[1]))
                    def unused_missing(identity):
                        if identity[0] != 'source':
                            return missing(identity)
                        owners = source_bindings.get((project,identity), {None})
                        if len(owners) != 1 or None in owners:
                            return True
                        current = current_sources.get(identity)
                        if current is not None and current != owners:
                            return True
                        return (next(iter(owners)),identity[1]) in unavailable_objects
                    def positions(identities, *, unused=False):
                        return [{'layer':layer,'object_id':identity,'rank':ranks.get((layer,identity)),
                                 'coverage':'unknown' if private or (unused_missing((layer,identity)) if unused else missing((layer,identity))) else
                                            'selected' if (layer,identity) in ranks else 'not_selected'}
                                for layer,identity in sorted(identities)]
                    variants.append({'status':'unavailable' if unknown else 'partial' if unavailable else 'complete',
                        **({'reason':'private_no_models'} if private else {}),
                        'rank_space':'selected_context',
                        'selected':None if unknown else len(ranks), 'missing_projections':unavailable,
                        'cited_total':len(cited) if cited is not None else None,
                        'citation_unknown':citation_unknown,
                        'cited_top_k':None if unknown or citation_unknown else sum(ranks.get(identity, k+1) <= k for identity in cited),
                        'struck_ranks':positions(struck), 'unused_ranks':positions(unused_ids, unused=True)})
            before, after = variants
            def changes(field):
                previous = {(entry['layer'],entry['object_id']):entry for entry in before[field]}
                following = {(entry['layer'],entry['object_id']):entry for entry in after[field]}
                result = []
                for identity in sorted(previous.keys() | following.keys()):
                    old, new = previous.get(identity), following.get(identity)
                    known = old is not None and new is not None and old['coverage'] != 'unknown' and new['coverage'] != 'unknown'
                    both = known and old['rank'] is not None and new['rank'] is not None
                    change = ('retained' if both else 'removed_from_selection' if known and old['rank'] is not None
                              else 'entered_selection' if known and new['rank'] is not None else 'not_selected' if known else 'unknown')
                    result.append({'layer':identity[0],'object_id':identity[1],
                        'before_rank':old['rank'] if old else None,'after_rank':new['rank'] if new else None,
                        'rank_delta':new['rank']-old['rank'] if both else None,
                        'selection_change':change,'coverage':'known' if known else 'unknown'})
                return result
            rows.append({'turn_id':turn.object_id,'project_id':project,'before':before,'after':after,
                'struck_changes':changes('struck_ranks'),'unused_changes':changes('unused_ranks'),
                'unused_coverage':'unknown_future_history' if project in future_projects else 'current_present_facts',
                'selected_delta':after['selected']-before['selected'] if before['selected'] is not None and after['selected'] is not None else None,
                'cited_top_k_delta':after['cited_top_k']-before['cited_top_k'] if before['cited_top_k'] is not None and after['cited_top_k'] is not None else None})
        if owner.settings() != settings:
            return {}
        return {'policies':selections,'k':k,'turns':rows,'model_attempts':query.models.attempts,
                'baseline':'local_no_models','missing_projections':len(query.retrieval_index.unavailable)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--forgetting-mode", choices=("manual", "auto-forgotten"), default="manual")
    parser.add_argument("--policy", action="append", default=[], metavar="INTERFACE=VERSION")
    parser.add_argument('--cases', type=Path, help='Explicit synthetic corpus subset')
    parser.add_argument('--replay', type=Path, help='Quiescent data-root copy; two --policy variant selections')
    args = parser.parse_args(argv)
    if not args.label or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in args.label):
        parser.error("label must contain letters, numbers, hyphens or underscores")
    output = args.output or ROOT / "work/qa/eval" / f'{datetime.now():%Y-%m-%d}-{args.label}.json'
    try:
        if args.replay is not None:
            from tools.signal_report import _copy_root, _output_file
            source = _copy_root(args.replay)
            output = _output_file(output, source)
            result = replay(source, policies=[value.split(',') for value in args.policy])
            output = _output_file(output, source)
        else:
            with override(**parse_overrides(args.policy)):
                result = evaluate(args.cases, forgetting_mode=args.forgetting_mode)
    except ValueError as error:
        parser.error(str(error) if args.replay is None or str(error) in {'report_output_forbidden', 'report_output_exists', 'replay_two_policies_required'} else 'signal_copy_invalid')
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.replay is not None:
        with output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = ({"output":str(output),"turns":len(result.get("turns",[]))} if args.replay is not None
               else {"output":str(output),"categories":result["categories"]})
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
