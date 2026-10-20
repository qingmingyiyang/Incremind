"""Receipt projection and durable turn persistence for the workbench."""

from __future__ import annotations

from fastapi import HTTPException

from ..server_jobs import QuotaError
from ..workspace_contracts import _empty_ask_context
from .task_do import TASK_EXECUTIONS


ITEM_STATES = "v2_workbench_item_states"


def persist_workbench_turn(tx, *, turn_id, project, thread_id, cleaned, now, created_at,
                           intent, receipt, item_id, item, instance, run_id, title_prefix,
                           replace_turn, research_state, parent_turn_id=None, route_meta=None, track_item=True):
    """共用原创建写入，完成回执与提交仍由调用方的同一事务负责。"""
    thread = tx.read("v2_threads", thread_id)
    if thread is not None and thread.payload.get("project_id") != project:
        raise HTTPException(404, "workbench_not_found")
    if thread is None:
        tx.put("v2_threads", thread_id, {"project_id": project, "title": cleaned[:40] or item["title"],
            **({'parent_turn_id':parent_turn_id} if parent_turn_id else {}),
            "created_at": now, "updated_at": now}, expected_revision=0)
    else:
        tx.put("v2_threads", thread_id, {**thread.payload, "updated_at": now}, expected_revision=thread.revision)
    saved = tx.put("v2_turns", turn_id, {"project_id": project, "thread_id": thread_id,
        "intent": intent, "user_text": cleaned, "created_at": created_at, "updated_at": now,
        "receipt": receipt, "item_id": item_id, "instance": instance, "run_id": run_id,
        **({'parent_turn_id':parent_turn_id} if parent_turn_id else {}),
        **({'route':route_meta} if route_meta else {}),
        **({"favorite_title_prefix": title_prefix} if title_prefix else {})},
        expected_revision=tx.read("v2_turns", turn_id).revision if replace_turn else 0)
    if intent == "remember" and track_item:
        tx.put(ITEM_STATES, turn_id, {"project_id": project, "item_id": item_id,
            "turn_id": turn_id, "thread_id": thread_id, "state": "processing", "error": None,
            "created_at": now, "updated_at": now}, expected_revision=0)
    if research_state is not None:
        tx.put(TASK_EXECUTIONS, turn_id, research_state, expected_revision=0)
    return saved


_ERRORS = {"processing_failed", "insight_generation_failed", "insight_invalid_output",
    "insight_private_project", "confirmation_failed", "related_retrieval_failed", "interrupted",
    "remote_disabled", "private_project_remote_blocked", "scene_assignment_failed",
    "storage_quota_exceeded", "job_quota_exceeded"}
_ASK_LAYERS = {"L3": "insight", "L2": "summary", "L1": "note", "L0": "source", "inspiration": "inspiration"}


def _ask_receipt(plan, chosen, result, egress_receipt_id, egress=None):
    layers = dict.fromkeys(('insight', 'summary', 'note', 'source', 'persona'), 0)
    if any(candidate['layer'] == 'inspiration' for candidate in chosen):
        layers['inspiration'] = 0
    layers['persona'] = plan.get('profile', {}).get('count', 0)
    for candidate in chosen:
        layers[_ASK_LAYERS[candidate["layer"]]] += 1
        layers["persona"] += int(bool(candidate.get("persona")))
    citations = []
    for source in result["sources"]:
        candidate = chosen[source["number"] - 1]
        entry = candidate["entry"]
        layer = _ASK_LAYERS[candidate["layer"]]
        identity = (entry["id"] if layer in {"insight", "inspiration"} else entry["document_id"] if layer in {"summary", "note"}
                    else entry.get("item_id") or entry["source_id"])
        citations.append({"n": source["number"], "layer": layer, "persona": bool(candidate.get("persona")),
            "id": identity, "title": source["title"], "quote": source["excerpt"],
            "locator": {"coordinate_space": source["coordinate_space"], "windows": source["windows"]},
            **({'url':candidate['url']} if candidate.get('url') else {}),
            **({"bookshelf": True} if candidate.get("bookshelf") else {}),
            **({"stale": True} if layer == "insight" and candidate.get("stale") is True else {}),
            **({"historical": True} if layer == "insight" and candidate.get("temporal")
               and candidate.get("validity", {}).get("valid_until") else {})})
    context = result.get("context") if not result.get("no_match") else _empty_ask_context()
    if isinstance(context, dict):
        egress = egress or {}
        basis = egress.get('consent_basis', {})
        context = {**context, 'egress': {'model': egress.get('target', {}).get('model'),
            'consent_scope': basis.get('scope'), 'settings_revision': basis.get('settings_revision'),
            'excluded_private': None}}
    if isinstance(context, dict) and plan.get("bookshelf", {}).get("hits"):
        context["bookshelf"] = plan["bookshelf"]
    return {"answer": result["answer"], "citations": citations, "layers": layers,
        "trace": [{**row, "layer": _ASK_LAYERS[row["layer"]]} for row in plan["trace"]],
        "egress_receipt_id": egress_receipt_id, "no_match": result.get("no_match", False),
        "context": context,
        "model_usage": result.get("model_usage", {}), "excluded_sources": result.get("excluded_sources", []),
        **({key: result[key] for key in ('partial', 'interruption')} if 'interruption' in result else {})}


def _safe_error(error, fallback):
    if isinstance(error, QuotaError):
        code = str(error)
        return code if code in {"storage_quota_exceeded", "job_quota_exceeded"} else fallback
    code = getattr(error, "detail", None)
    return code if isinstance(code, str) and code in _ERRORS else fallback

