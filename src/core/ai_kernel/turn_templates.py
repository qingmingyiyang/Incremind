"""Immutable versioned template data and validation; no runtime dependency."""
from types import MappingProxyType
from collections.abc import Mapping


TURN_KINDS = (
    "memory.organize", "memory.propose_insights", "memory.consolidate",
    "memory.link_suggest", "memory.overview", "memory.place", "memory.skill_export", "media.image_read", "project.answer", "project.task", "workbench.route", "external.context", "web.search",
)
_READS = MappingProxyType({
    "memory.organize": ("source.evidence.read",),
    "memory.propose_insights": ("source.evidence.read",),
    "memory.consolidate": ("memory.candidate.evidence.read", "memory.recall"),
    "memory.link_suggest": ("memory.recall",),
    "memory.overview": (),
    "memory.place": (),
    "memory.skill_export": (),
    "media.image_read": (),
    "project.answer": ("memory.recall", "source.evidence.read"),
    "external.context": ("external.context.execute",),
    "web.search": (),
})
# Version 1 is a persisted protocol, not a projection of live Agent profiles.
_TASK_V1_CAPABILITIES = (
    "agent.fan_in", "agent.interrupt", "agent.list", "agent.message", "agent.plan", "agent.spawn", "agent.wait",
    "analyze_source", "companion.chat.context.read", "companion.chat.message.write", "companion.vision.analyze.write",
    "companion.vision.context.read", "document.draft.propose", "image.generate", "memory.candidate.evidence.read",
    "memory.candidate.propose.write", "memory.recall", "presentation.pptx.fixed", "project_skill.draft.propose",
    "project_skill.evidence.read", "series.intake.organize.commit", "source.evidence.read",
    "workbench.input.classification.context.read", "workbench.input.classification.enhance.write", "workbench.question.answer",
)


def _template(kind, version=1):
    if kind not in TURN_KINDS:
        raise ValueError("unknown product turn kind")
    if type(version) is not int or version not in {1, 2} or (version == 2 and kind not in {"project.answer", "project.task"}):
        raise ValueError("execution template version is invalid")
    if version == 2 and kind == "project.task":
        return ("external.task.execute",), 1, 1_230_000
    if version == 2:
        return (*_READS[kind], "workbench.answer.execute"), 2, 120_000
    if kind == "workbench.route":
        # DeepSeek often needs 3-6 s and a cold first call ~7 s plus local
        # bookkeeping; 8 s still fell back to rules and split nothing.
        return (), 1, 15_000
    if kind == "web.search":
        return (), 1, 120_000
    if kind == "project.task":
        return _TASK_V1_CAPABILITIES, 64, 600_000
    if kind == "external.context":
        return _READS[kind], 1, 120_000
    return _READS[kind], 4 if kind.startswith("memory.") else 2, 120_000


def template_purpose(kind):
    """Return the purpose assigned to a registered version-one template."""
    if kind not in TURN_KINDS:
        raise ValueError("unknown product turn kind")
    return "aux" if kind.startswith("memory.") or kind in {"workbench.route", "media.image_read", "external.context", "web.search"} else "primary"


def turn_purpose(request):
    """Absent policy means historical primary, without rewriting old payloads."""
    policy = request.get("execution_policy")
    purpose = policy.get("purpose") if isinstance(policy, Mapping) else "primary"
    if purpose not in {"primary", "aux"}:
        raise ValueError("product turn purpose is invalid")
    return purpose


def is_user_turn(request):
    return turn_purpose(request) == "primary"


def validate_execution_policy(request):
    policy = request.get("execution_policy")
    if not isinstance(policy, Mapping) or set(policy) != {"template_version", "purpose", "budget"}:
        raise ValueError("execution policy fields are invalid")
    kind = request["desired_outcome"]
    allowed, steps, timeout = _template(kind, policy["template_version"])
    if turn_purpose(request) != template_purpose(kind):
        raise ValueError("turn kind purpose does not match its template")
    if not set(request["capability_policy"]["allowed"]) <= set(allowed):
        raise ValueError("turn capabilities exceed the frozen template")
    if kind == "external.context":
        exact = request.get("capability_request")
        if (request["capability_policy"] != {"allowed": ["external.context.execute"], "denied": [], "require_approval": []}
                or not isinstance(exact, Mapping) or set(exact) != {"mode", "capability_id", "arguments"}
                or exact.get("mode") != "execute_exact_v1" or exact.get("capability_id") != "external.context.execute"):
            raise ValueError("external context requires its exact read capability")
        args = exact.get("arguments")
        scope = args.get("scope") if isinstance(args, Mapping) else None
        if (not isinstance(args, Mapping) or set(args) != {"client", "tool", "query", "scope", "budget"}
                or not isinstance(args.get("client"), str) or args["client"] not in {"claude", "codex"}
                or not isinstance(args.get("tool"), str) or args["tool"] not in {"projects", "recall", "methods", "read"}
                or not isinstance(args.get("query"), str) or '\x00' in args["query"]
                or type(args.get("budget")) is not int or not 1 <= args["budget"] <= 12000
                or not isinstance(scope, Mapping) or set(scope) != {"user_id", "project_id"}
                or not isinstance(scope.get("user_id"), str) or not scope["user_id"]
                or scope.get("project_id") != request["scope"]["project_id"]):
            raise ValueError("external context exact arguments are invalid")
    if kind == "project.task" and policy["template_version"] == 2:
        # 引用格式仅收窄冻结请求；真实绑定和权限仍由宿主 authority 核验。
        exact = request.get("capability_request")
        args = exact.get("arguments") if isinstance(exact, Mapping) else None
        ref = args.get("binding_ref") if isinstance(args, Mapping) else None
        if (request["capability_policy"] != {"allowed": ["external.task.execute"], "denied": [], "require_approval": []}
                or not isinstance(exact, Mapping) or set(exact) != {"mode", "capability_id", "arguments"}
                or exact.get("mode") != "execute_exact_v1" or exact.get("capability_id") != "external.task.execute"
                or not isinstance(args, Mapping) or set(args) != {"binding_ref"}
                or ref != f"crp://session/{request['turn_id']}/external-task-run-v1"):
            raise ValueError("external task requires its exact binding capability")
    context = request["context_policy"]
    maxima = {"include_project_skill": kind == "project.task",
              "include_memory": kind.startswith("project."),
              "include_session_history": kind == "project.task"}
    if kind == "project.task" and policy["template_version"] == 2:
        maxima = dict.fromkeys(maxima, False)
    if any(type(context[name]) is not bool or (context[name] and not permitted)
           for name, permitted in maxima.items()):
        raise ValueError("turn context exceeds the frozen template")
    if type(context["max_context_bytes"]) is not int or not 1024 <= context["max_context_bytes"] <= 262144:
        raise ValueError("turn context budget is invalid")
    budget = policy["budget"]
    if not isinstance(budget, Mapping) or set(budget) != {"max_steps", "planner_timeout_ms"}:
        raise ValueError("turn budget fields are invalid")
    for name, maximum in (("max_steps", steps), ("planner_timeout_ms", timeout)):
        value = budget[name]
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("turn budget exceeds the template or is invalid")




def planner_limits(request, events, *, max_steps, timeout_ms):
    policy = request.get("execution_policy")
    if policy is None:
        return max_steps, timeout_ms
    budget = policy["budget"]
    initiated = {
        event.get("correlation", {}).get("model_request_id")
        for event in events if event.get("type") == "model.requested"
        and not event.get("correlation", {}).get("tool_call_id")
    }
    initiated.discard(None)
    return max(0, min(max_steps, budget["max_steps"]) - len(initiated)), min(timeout_ms, budget["planner_timeout_ms"])
