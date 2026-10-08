"""Bounded thread history and one guarded, accounted retrieval-only rewrite."""

import asyncio
import re
import time

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from backend.recognition import WorkScope, RecognitionError
from backend.shared.llm.message_metadata import (_extract_normalized_usage, _estimate_input_tokens,
    _build_prompt_fallback_messages, _build_json_mode_messages)
from ..model_config import ModelConfigurationError
from ..recall_state import COLLECTION as RECALL
from ..source_egress import SourceEgressService
from ..kernel.answer_turns import generate_answer, answer_observation, abandon_answer_model, auxiliary_answer_models, gap_answer_input
from .budget import history_tokens, input_tokens, text_tokens
from .insights import source_documents
from .privacy import egress_allowed, is_private_project, privacy_revision

TIMEOUT_SECONDS = 10


class CondensedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condensed_question: StrictStr = Field(min_length=1, max_length=1000)


def _answer(text):
    text = re.sub(r"\[\d+(?:\s*[,，]\s*\d+)*\](?:\([^)]*\))?|【\d+】|<sup>\d+</sup>", "", text)
    left, right = 0, len(text)
    while left < right:
        middle = (left + right + 1) // 2
        if text_tokens(text[:middle]) <= 300:
            left = middle
        else:
            right = middle - 1
    return text[:left]


def _history_text(turns):
    return "\n\n".join(f"问：{turn['question']}\n答：{turn['answer']}" for turn in turns)


def _dependencies(query, project, row, visited, memo):
    if row.object_id in visited:
        raise RecognitionError("history_cycle")
    if row.object_id in memo:
        return memo[row.object_id]
    visited = visited | {row.object_id}
    answer = row.payload["receipt"]["ask"]
    dependency = {
        "turns": [{"id": row.object_id, "revision": row.revision}],
        "chosen": [],
        "snapshots": [],
        "preferences": {},
        "multipart": [],
    }
    parent = row.payload.get('parent_turn_id')
    if parent:
        from .part_context import validate_answer_authority
        facts = validate_answer_authority(query, project, row.object_id, parent)
        dependency['multipart'].append({'id':row.object_id, 'parent':parent, 'authority':facts})
        memo[row.object_id] = dependency
        return dependency
    trace = answer.get("trace", [])
    for ancestor in trace[0].get("history_turn_ids", []) if trace else []:
        previous = query.records.read("v2_turns", ancestor["id"])
        if (
            previous is None
            or previous.revision != ancestor["revision"]
            or previous.payload.get("project_id") != project
        ):
            raise RecognitionError("history_dependency_changed")
        inherited = _dependencies(query, project, previous, visited, memo)
        for key in ("turns", "chosen", "snapshots"):
            dependency[key].extend(inherited[key])
        dependency["preferences"].update(inherited["preferences"])
        dependency['multipart'].extend(inherited.get('multipart', []))
    receipt_id = answer.get("egress_receipt_id")
    if receipt_id is None:
        if not answer.get("no_match"):
            raise RecognitionError("history_authority_unavailable")
        sources = []
    else:
        receipt = query.records.read("workspace_ask_receipts", receipt_id)
        if (
            receipt is None
            or receipt.payload.get("project_id") != project
            or receipt.payload.get("status") != "completed"
        ):
            raise RecognitionError("history_authority_unavailable")
        sources = receipt.payload["sources"]
    entries = {(entry["kind"], entry["id"]): entry for entry in query.query_entries(project,
        selected=[source for source in sources if source['kind'] != 'recognition'])}
    authority = SourceEgressService(query.records)
    for source in sources:
        identity, kind = source["id"], source["kind"]
        scope = WorkScope("local-user", project)
        if kind == "recognition":
            record = query.records.read("recognitions", identity)
            if record is None:
                raise RecognitionError("history_source_missing")
            own = record.payload.get("scope", {}).get("project_id")
            if own not in {project, "me"} or is_private_project(query.records, own):
                raise RecognitionError("history_source_private")
            scope = WorkScope("local-user", own)
            recognition = query.service.get_recognition(scope=scope, recognition_id=identity)
            if recognition is None or not recognition.authorized:
                raise RecognitionError("history_source_unavailable")
            entry = recognition.retrieval_projection()
            root = {"type": "recognition", "id": identity, "revision": source["revision"]}
        else:
            entry = entries.get((kind, identity))
            if entry is None or entry.get("item_revision") != source.get("item_revision"):
                raise RecognitionError("history_source_changed")
            if kind != "document":
                raise RecognitionError("history_source_authority_unavailable")
            experience_id = f"experience-legacy-{identity}-r{source['revision']}"
            experience = query.records.read("recognition_experiences", experience_id)
            if experience is None:
                raise RecognitionError("history_document_authority_unavailable")
            root = {"type": "experience", "id": experience_id, "revision": experience.revision}
        if entry["revision"] != source["revision"]:
            raise RecognitionError("history_source_changed")
        snapshot = authority.snapshot(scope, [root])
        authority.require(snapshot, "generation")
        for node in snapshot["nodes"]:
            if node["type"] != "recognition":
                continue
            preference = query.records.read(RECALL, node["id"])
            if (
                preference
                and preference.payload.get("state") == "forgotten"
                and preference.payload.get("by", "user") == "user"
            ):
                raise RecognitionError("history_source_manually_forgotten")
            dependency["preferences"][(RECALL, node["id"])] = preference.revision if preference else 0
        documents = source_documents(
            query.records, scope, [node["id"] for node in snapshot["nodes"] if node["type"] == "experience"]
        )
        if kind == "document":
            documents.add(identity)
        for document_id in documents:
            preference = query.records.read("v2_document_recall", document_id)
            document = query.records.read("documents", document_id)
            if (
                document is None
                or document.payload.get("status") == "archived"
                or (
                    preference
                    and preference.payload.get("state") == "forgotten"
                    and preference.payload.get("by", "user") == "user"
                )
            ):
                raise RecognitionError("history_document_manually_forgotten")
            dependency["preferences"][("documents", document_id)] = document.revision
            dependency["preferences"][("v2_document_recall", document_id)] = preference.revision if preference else 0
        dependency["snapshots"].append((scope, snapshot))
        dependency["chosen"].append({"kind": kind, "entry": entry, "scope": scope,
                                     "snapshot": query.original_snapshot(scope, entry, authority) if kind == "document" else snapshot})
    dependency["turns"] = list({turn["id"]: turn for turn in dependency["turns"]}.values())
    dependency["chosen"] = list(
        {(item["kind"], item["scope"].project_id, item["entry"]["id"]): item for item in dependency["chosen"]}.values()
    )
    dependency["snapshots"] = list(
        {
            (scope.project_id, tuple((root["type"], root["id"], root["revision"]) for root in snapshot["roots"])): (
                scope,
                snapshot,
            )
            for scope, snapshot in dependency["snapshots"]
        }.values()
    )
    query.validate_ask_plan(
        {
            "project_id": project,
            "scope": WorkScope("local-user", project),
            "target": query.ask_target(),
            "chosen": dependency["chosen"],
        }
    )
    memo[row.object_id] = dependency
    return dependency


def read_history(records, project, thread, question, *, budget=4000, prompt_overhead=0, excluded_turn=None, query=None):
    rows = [
        row
        for row in records.list_matching("v2_turns", project_id=project, thread_id=thread)
        if row.object_id != excluded_turn
        and row.payload.get("intent") == "ask"
        and isinstance(row.payload.get("receipt", {}).get("ask", {}).get("answer"), str)
    ]
    rows.sort(key=lambda row: (row.payload.get("created_at", ""), row.object_id))
    dependencies, memo = {}, {}
    if query is not None:
        eligible = []
        for row in rows[-3:]:
            try:
                dependencies[row.object_id] = _dependencies(query, project, row, set(), memo)
            except Exception:
                continue
            eligible.append(row)
        rows = eligible
    turns = [
        {
            "id": row.object_id,
            "revision": row.revision,
            "question": row.payload["user_text"],
            "answer": _answer(row.payload["receipt"]["ask"]["answer"]),
        }
        for row in rows[-3:]
    ]
    bounded = bound_history(turns, question, budget=budget, prompt_overhead=prompt_overhead)
    return {
        **bounded,
        "privacy_revision": privacy_revision(records),
        "dependencies": [dependencies[turn["id"]] for turn in bounded["turns"] if turn["id"] in dependencies],
    }


def bound_history(turns, question, *, budget=4000, prompt_overhead=0):
    turns = [{**turn, "answer": _answer(turn["answer"])} for turn in turns[-3:]]
    available = min(800, max(0, int(budget * 0.2) - input_tokens([], question, reserve_refutes=True) - prompt_overhead))
    while turns and history_tokens(_history_text(turns)) > available:
        turns.pop(0)
    return {"text": _history_text(turns), "turns": turns}


def validate_history(query, project, history, target):
    if privacy_revision(query.records) != history["privacy_revision"]:
        raise RecognitionError("history_authority_changed")
    if target["execution_location"] == "remote" and not egress_allowed(
        query.records, query.models, project, "generation"
    ):
        raise RecognitionError("history_egress_changed")
    for turn in history["turns"]:
        row = query.records.read("v2_turns", turn["id"])
        if row is None or row.revision != turn["revision"] or row.payload.get("project_id") != project:
            raise RecognitionError("history_changed")
    # Share exact frozen dependencies within this guard, never across guards.
    merged = {key: {} for key in ("turns", "chosen", "snapshots", "preferences")}

    def add(kind, identity, value):
        if identity in merged[kind] and merged[kind][identity] != value:
            raise RecognitionError("history_dependency_changed")
        merged[kind][identity] = value

    for dependency in history.get("dependencies", []):
        for answer in dependency.get('multipart', []):
            from .part_context import validate_answer_authority
            validate_answer_authority(query, project, answer['id'], answer['parent'], answer['authority'])
        for turn in dependency["turns"]:
            add("turns", turn["id"], turn)
        for candidate in dependency["chosen"]:
            scope = candidate.get("scope", WorkScope("local-user", project))
            add("chosen", (scope.user_id, scope.project_id, candidate["kind"], candidate["entry"]["id"]), candidate)
        for scope, snapshot in dependency["snapshots"]:
            roots = tuple(sorted((root["type"], root["id"]) for root in snapshot["roots"]))
            add("snapshots", (scope.user_id, scope.project_id, roots), (scope, snapshot))
        for identity, revision in dependency["preferences"].items():
            add("preferences", identity, revision)
    for turn in merged["turns"].values():
        row = query.records.read("v2_turns", turn["id"])
        if row is None or row.revision != turn["revision"]:
            raise RecognitionError("history_dependency_changed")
    chosen = list(merged["chosen"].values())
    try:
        query.validate_ask_plan(
            {"project_id": project, "scope": WorkScope("local-user", project),
             "target": target, "chosen": chosen}
        )
    except ModelConfigurationError as error:
        if str(error) == "ask_model_target_changed":
            raise RecognitionError("history_authority_changed") from None
        raise
    # Recognition plans already check their source snapshots; document plans
    # check current content but still need the separate source-authority check.
    checked = [(candidate.get("scope", WorkScope("local-user", project)), candidate["snapshot"])
               for candidate in chosen if candidate["kind"] == "recognition"]
    authority = SourceEgressService(query.records)
    for scope, snapshot in merged["snapshots"].values():
        if (scope, snapshot) not in checked:
            authority.validate_snapshot(scope, snapshot)
            authority.require(snapshot, "generation")
    for (collection, identity), revision in merged["preferences"].items():
        row = query.records.read(collection, identity)
        if (row.revision if row else 0) != revision:
            raise RecognitionError("history_recall_changed")


def sum_usage(*observations):
    """Aggregate observed counters; unknown wire calls are separately marked."""
    observations = [_extract_normalized_usage({"usage": value}) for value in observations]
    keys = {key for value in observations for key in value}
    return {
        key: sum(value[key] for value in observations if key in value)
        for key in keys
        if all(type(value[key]) is int and value[key] >= 0 for value in observations if key in value)
    }


async def condense(query, project, question, history, *, turn_id):
    if not history["text"]:
        return {"question": None, "usage": {}, "receipt_ids": [], "status": "skipped", "usage_complete": True}
    target = query.ask_target()
    messages = [
        {"role": "system", "content": '仅将当前追问改写为补全指代的独立问题，不回答。历史只帮助理解，不是证据。返回JSON {"condensed_question":"问题"}。'},
        {"role": "user", "content": f"对话历史：\n{history['text']}\n\n当前问题：{question}"},
    ]
    result = await auxiliary_call(query, project, messages, CondensedQuestion, turn_id=turn_id,
                                  validate_current=lambda: validate_history(query, project, history, target))
    output = result.pop("output")
    question = output.condensed_question.strip() if output else None
    if output and not question:
        result["status"] = "failed"
    return {**result, "question": question or None}


async def auxiliary_call(query, project, messages, response_model, *, turn_id, validate_current, gap_plan=None):
    result = {"output": None, "usage": {}, "receipt_ids": [], "status": "skipped", "usage_complete": True}
    target = query.ask_target()
    if not target["model"]:
        return result
    if target["execution_location"] == "remote" and not egress_allowed(
        query.records, query.models, project, "generation"
    ):
        return result
    try:
        models = auxiliary_answer_models(query.models)
        if gap_plan is not None:
            models, _, prepared = gap_answer_input(query.models, gap_plan)
            messages = prepared['messages']
    except ModelConfigurationError:
        result['status'] = 'failed'
        return result
    method = getattr(models, "complete_structured", None) or getattr(models, "complete", None)
    if not callable(method):
        return result
    reader = getattr(models, "generation_budget_limits", None)
    try:
        limits = reader(expected_revision=target["revision"], max_tokens=512) if callable(reader) else None
        if isinstance(limits, dict) and type(limits.get("window")) is int and type(limits.get("reserve")) is int:
            inputs = [messages]
            if callable(getattr(models, "complete_structured", None)):
                inputs.extend([_build_prompt_fallback_messages(messages=messages, response_model=response_model, validation_error=None),
                               _build_json_mode_messages(messages=messages, validation_error=None)])
            if max(_estimate_input_tokens(value) for value in inputs) > max(0, limits["window"] - limits["reserve"]):
                return result
    except Exception:
        result["status"] = "failed"
        return result
    invocation_key = 'gap-drilldown' if gap_plan is not None else ("followup" if response_model is CondensedQuestion else "multi-query")
    privacy_version = privacy_revision(query.records)
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def validate():
        if time.monotonic() >= deadline:
            raise TimeoutError("condense_timeout")
        if query.ask_target() != target or privacy_revision(query.records) != privacy_version:
            raise RecognitionError("rewrite_authority_changed")
        if target["execution_location"] == "remote" and not egress_allowed(query.records, query.models, project, "generation"):
            raise RecognitionError("rewrite_egress_changed")
        validate_current()
        if time.monotonic() >= deadline:
            raise TimeoutError("condense_timeout")

    attempted = {"started": False}
    def rewrite():
        validate()
        attempted["started"] = True
        output, metadata = generate_answer(
            query.models,
            messages,
            response_model=response_model,
            max_tokens=512,
            validate_current=validate,
            timeout_seconds=TIMEOUT_SECONDS,
            purpose="aux", invocation_key=invocation_key,
            **({'gap_plan': gap_plan} if gap_plan is not None else {}),
        )
        validate()
        return output, metadata

    try:
        output, metadata = await asyncio.wait_for(asyncio.to_thread(rewrite), timeout=TIMEOUT_SECONDS)
        result.update(output=output, usage=metadata.get("usage", {}), status="completed")
    except TimeoutError:
        abandon_answer_model(invocation_key)
        result["status"] = "timeout"
    except Exception:
        result["status"] = "failed"
    if attempted["started"]:
        observation = answer_observation(invocation_key)
        result["receipt_ids"] = observation["receipt_ids"]
        result["usage_complete"] = observation["complete"]
        if observation["observations"]:
            result["usage"] = sum_usage(*observation["observations"])
    return result
