"""Routing uses the real kernel, SQLite, model configuration and gateway."""
import json
from pathlib import Path
import time
from threading import Event, Thread

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.privacy import set_private_project
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


TEXT = "记下本季度的预算已经确定为十万元。根据这段预算信息回答我们有多少资金可以使用？"


def part(intent, span, depends_on=(), instruction=None):
    return {"intent": intent, "span": span, "instruction": instruction, "depends_on": list(depends_on)}


def good_output():
    left, right = TEXT.split("。", 1)
    return {"parts": [part("remember", left + "。"), part("ask", right, [0])]}


@pytest.fixture
def env(tmp_path):
    # Load the optional gateway dependency before the four-second route budget.
    import litellm
    records = SQLiteStructuredRecordStore(tmp_path / "recognition.sqlite3")
    calls = []
    output = {"value": good_output(), "hook": None}
    def wire(**kwargs):
        calls.append(kwargs)
        if output["hook"]:
            output["hook"](kwargs)
        value = output["value"]
        if isinstance(value, Exception):
            raise value
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 6, "total_tokens": 20}}
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=wire)
    models.update("generation", {"base_url": "https://example.test", "model": "synthetic-model",
        "api_key": "synthetic-only", "allow_remote": True, "expected_revision": 0})
    from backend.memory_app.v2.route import RouteService
    return RouteService(records, models), records, models, calls, output


def test_valid_model_route_frozen_replay_and_durable_receipt(env):
    service, records, models, calls, output = env
    first = service.route(TEXT, project_id="alpha", request_key="same")
    assert first.mode == "model"
    assert [p.model_dump() for p in first.parts] == good_output()["parts"]
    frozen = json.dumps(service.store.get_request(first.turn_id), ensure_ascii=False, sort_keys=True).encode()
    from backend.memory_app.v2.route import RouteService
    second = RouteService(records, models).route(TEXT, project_id="alpha", request_key="same")
    assert first == second
    assert frozen == json.dumps(service.store.get_request(second.turn_id), ensure_ascii=False, sort_keys=True).encode()
    assert len(calls) == 1
    request = service.store.get_request(first.turn_id)
    inputs = json.loads(request["input"]["text"])
    assert inputs == {"original_text": TEXT, "project_id": "alpha", "examples": [], "has_files": False}
    assert request["execution_policy"]["purpose"] == "aux"
    assert request["capability_policy"]["allowed"] == []
    assert request["execution_policy"]["budget"]["planner_timeout_ms"] == 4000
    assert calls[0]["max_tokens"] == 400
    assert 0 < calls[0]["timeout"] <= 4
    assert "response_format" in calls[0]
    receipt_event = next(e for e in service.store.events_after(first.turn_id) if e["type"] == "model.completed")
    receipt = service.store.get(receipt_event["data"]["receipt_ref"])
    assert receipt["model_call_purpose"] == "aux"
    assert receipt["usage"]["total_tokens"] == 20
    assert first.egress_receipt_id == receipt["receipt_id"]
    assert first.usage == receipt["usage"]


@pytest.mark.parametrize("change", ["rewritten", "overlap", "omission", "zero", "four", "two_do", "intent", "reverse", "cycle", "self", "bool_dep", "range", "three_layers", "remember_instruction", "long_instruction"])
def test_invalid_model_output_falls_back_once(env, change):
    service, records, models, calls, output = env
    value = good_output()
    parts = value["parts"]
    if change == "rewritten": parts[0]["span"] = "被改写的预算"
    elif change == "overlap": parts[1]["span"] = TEXT
    elif change == "omission": parts.pop()
    elif change == "zero": value["parts"] = []
    elif change == "four": value["parts"] = parts * 2
    elif change == "two_do":
        parts[0]["intent"] = "do"
        parts[1]["intent"] = "do"
        parts[1]["depends_on"] = []
    elif change == "intent": parts[0]["intent"] = "unknown"
    elif change == "reverse": parts[0]["depends_on"] = [1]
    elif change == "cycle": parts[0]["depends_on"] = [1]
    elif change == "self": parts[0]["depends_on"] = [0]
    elif change == "bool_dep": parts[1]["depends_on"] = [False]
    elif change == "range": parts[1]["depends_on"] = [9]
    elif change == "three_layers":
        value["parts"] = [part("remember", TEXT[:4]), part("ask", TEXT[4:8], [0]), part("do", TEXT[8:], [1])]
    elif change == "remember_instruction": parts[0]["instruction"] = "重新组织原文"
    else: parts[1]["instruction"] = "问" * 201
    output["value"] = value
    result = service.route_model(TEXT, project_id="alpha", request_key=change)
    assert result.mode == "rules"
    assert len(result.parts) == 1
    assert result.parts[0].span == TEXT
    assert len(calls) == 1
    again = service.route_model(TEXT, project_id="alpha", request_key=change)
    assert again.mode == "rules"
    assert len(calls) == 1


@pytest.mark.parametrize("reason", ["disabled", "private", "private_tag", "unknown_tag", "ambiguous_tag"])
def test_privacy_blocks_all_model_calls(env, reason):
    service, records, models, calls, output = env
    text = TEXT
    if reason == "disabled":
        models.update("generation", {"allow_remote": False, "expected_revision": 1})
    elif reason == "private": set_private_project(records, "alpha", True, 0)
    else:
        text = "#秘密 " + TEXT
        if reason != "unknown_tag":
            with records.begin() as tx:
                tx.put("v2_projects", "secret", {"name": "秘密"}, expected_revision=0)
                if reason == "ambiguous_tag": tx.put("v2_projects", "second", {"name": "秘密"}, expected_revision=0)
                tx.commit()
            set_private_project(records, "secret", True, 0)
    assert service.route_model(text, project_id="alpha", request_key=reason).mode == "rules"
    assert calls == []


@pytest.mark.parametrize("text,files", [("", True), ("https://example.test/doc#details", False), ("灵感：让书架自己发光", False), ("怎么安排？", False), ("帮我写一句祝福。", False)])
def test_fast_routes_never_call_models(env, text, files):
    service, records, models, calls, output = env
    result = service.route(text, project_id="alpha", request_key="fast", has_files=files)
    assert result.mode == "rules"
    assert calls == []


def test_mixed_short_input_and_file_with_text_use_model(env):
    service, records, models, calls, output = env
    text = "记下预算十万。还剩多少？"
    output["value"] = {"parts": [part("remember", "记下预算十万。"), part("ask", "还剩多少？", [0])]}
    assert service.route(text, project_id="alpha", request_key="mixed", has_files=True).mode == "model"
    assert len(calls) == 1


def test_timeout_transport_and_late_output_do_not_become_model_plans(env):
    service, records, models, calls, output = env
    output["value"] = TimeoutError("synthetic timeout")
    result = service.route_model(TEXT, project_id="alpha", request_key="timeout")
    assert result.mode == "rules"
    assert len(calls) == 1
    output["value"] = good_output()
    output["hook"] = lambda kw: time.sleep(4.02)
    result = service.route_model(TEXT, project_id="alpha", request_key="late")
    assert result.mode == "rules"
    assert service.store.get_immutable_payload(result.turn_id, "workbench-route-output-v1") is None


def test_revocation_during_wire_fails_closed(env):
    service, records, models, calls, output = env
    output["hook"] = lambda kw: set_private_project(records, "alpha", True, 0)
    assert service.route_model(TEXT, project_id="alpha", request_key="revoke").mode == "rules"
    assert len(calls) == 1


def test_key_cannot_be_reused_for_changed_input(env):
    service, records, models, calls, output = env
    service.route_model(TEXT, project_id="alpha", request_key="key")
    with pytest.raises(ValueError, match="identity"):
        service.route_model(TEXT + "修改", project_id="alpha", request_key="key")
    assert len(calls) == 1


def test_scope_tags_and_repeated_spans_validate_original_positions():
    from backend.memory_app.v2.route import validate_route_output
    text = "第一行\n#项目 第二行"
    parts = [part("remember", "第一行"), part("ask", "第二行")]
    assert len(validate_route_output(text, {"parts": parts}).parts) == 2
    assert len(validate_route_output("记下，记下", {"parts": [part("remember", "记下"), part("ask", "记下")]}).parts) == 2
    with pytest.raises(ValueError):
        validate_route_output("https://example.test/#secret", {"parts": [part("remember", "https://example.test/")]})


def test_long_repeated_omitted_content_is_rejected_in_bounded_time():
    from backend.memory_app.v2.route import validate_route_output
    started = time.monotonic()
    with pytest.raises(ValueError):
        validate_route_output("甲" * 60000, {"parts": [part("remember", "甲"), part("ask", "甲"), part("do", "甲")]})
    assert time.monotonic() - started < 1


def test_long_anchor_keeps_valid_repeated_span_indices():
    from backend.memory_app.v2.route import validate_route_output
    value = {"parts": [part("ask", "和", [1]), part("remember", "甲" + "和" * 4999)]}
    result = validate_route_output("甲" + "和" * 5000, value)
    assert result.model_dump() == value


def test_two_instances_same_key_dispatch_one_wire(env):
    service, records, models, calls, output = env
    from backend.memory_app.v2.route import RouteService
    entered, release = Event(), Event()
    def block_wire(kw):
        entered.set()
        assert release.wait(3)
    output["hook"] = block_wire
    results = []
    worker = Thread(target=lambda: results.append(service.route_model(TEXT, project_id="alpha", request_key="concurrent")))
    worker.start()
    assert entered.wait(4)
    try:
        second = RouteService(records, models).route_model(TEXT, project_id="alpha", request_key="concurrent")
        assert second.mode == "rules"
        assert second.reason == "prior_result_unavailable"
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0].mode == "model"
    assert service.route_model(TEXT, project_id="alpha", request_key="concurrent").mode == "model"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["private", "disabled", "configuration"])
def test_completed_result_is_not_reused_after_authority_changes(env, change):
    service, records, models, calls, output = env
    assert service.route_model(TEXT, project_id="alpha", request_key="old").mode == "model"
    if change == "private":
        set_private_project(records, "alpha", True, 0)
    else:
        models.update("generation", {**({"allow_remote": False} if change == "disabled" else {"model": "other-model"}),
            "expected_revision": 1})
    assert service.route_model(TEXT, project_id="alpha", request_key="old").mode == "rules"
    assert len(calls) == 1


def test_manual_selection_does_not_route(env):
    service, records, models, calls, output = env
    result = service.route(TEXT, project_id="alpha", request_key="manual", intent="remember")
    assert result.mode == "manual"
    assert len(result.parts) == 1 and result.parts[0].span == TEXT
    assert calls == []


def test_fan_in_dependencies_use_real_gateway(env):
    service, records, models, calls, output = env
    root = Path(__file__).resolve().parents[3]
    fixture = json.loads((root / "tests/fixtures/workbench_route/cases.json").read_text(encoding="utf-8"))
    case = next(case for case in fixture["cases"] if case["id"] == "rad03")
    output["value"] = {"parts": case["parts"]}
    result = service.route_model(case["text"], project_id="alpha", request_key="fan-in")
    assert result.mode == "model", tuple(service.store.events_after(result.turn_id))
    assert [p.model_dump() for p in result.parts] == case["parts"]


def test_perfect_model_pipeline_and_production_policy_reports(env, tmp_path):
    service, records, models, calls, output = env
    import importlib.util
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("route_eval", root / "tools" / "route_eval.py")
    evaluator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluator)
    fixture = json.loads((root / "tests/fixtures/workbench_route/cases.json").read_text(encoding="utf-8"))
    from backend.memory_app.v2.intent import parse_scope_tag
    tags = {parse_scope_tag(case["text"])[0] for case in fixture["cases"]} - {None}
    with records.begin() as tx:
        for index, tag in enumerate(sorted(tags)):
            tx.put("v2_projects", f"project-{index}", {"name": tag}, expected_revision=0)
        tx.commit()
    predictions = {"version": 1, "cases": []}
    policy = {"version": 1, "cases": []}
    policy_modes = {}
    for case in fixture["cases"]:
        output["value"] = {"parts": case["parts"]}
        planned = service.route_model(case["text"], project_id="alpha", request_key="model-" + case["id"], has_files=case["has_files"])
        if planned.mode != "model":
            evidence = root / "work/qa/T13.1"
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "corpus-failure.json").write_text(json.dumps({"case": case["id"],
                "plan": planned.model_dump(), "events": list(service.store.events_after(planned.turn_id)) if planned.turn_id else []},
                ensure_ascii=False, indent=2), encoding="utf-8")
        assert planned.mode == "model", (case["id"], planned)
        predictions["cases"].append({"id": case["id"], "parts": [p.model_dump() for p in planned.parts]})
        planned = service.route(case["text"], project_id="alpha", request_key="policy-" + case["id"], has_files=case["has_files"])
        policy_modes[case["id"]] = planned.mode
        policy["cases"].append({"id": case["id"], "parts": [p.model_dump() for p in planned.parts]})
    report = evaluator.evaluate(fixture, predictions)
    policy_report = evaluator.evaluate(fixture, policy)
    assert all(metric["accuracy"] == 1 for metric in report["metrics"].values())
    assert policy_report["case_count"] == len(fixture["cases"])
    assert set(policy_modes) == {case["id"] for case in fixture["cases"]}
    assert set(policy_modes.values()) <= {"rules", "model"}
    policy_report["route_modes"] = policy_modes
    evidence = root / "work/qa/T13.1"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "perfect-model.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (evidence / "production-policy.json").write_text(json.dumps(policy_report, ensure_ascii=False, indent=2), encoding="utf-8")


def test_submission_identity_uses_direct_record_key(env):
    service, records, models, calls, output = env
    from backend.memory_app.turn_routing import _revision
    result = service.route_model(TEXT, project_id="alpha", request_key="indexed")
    assert result.mode == "model"
    key = _revision({"project_id": "alpha", "request_key": "indexed"})
    stored = records.read("v2_route_turn_keys", key)
    assert stored is not None
    assert stored.payload["request"]["turn_id"] == result.turn_id
    assert service.route_model(TEXT, project_id="alpha", request_key="indexed") == result
    assert len(calls) == 1


def test_same_submission_key_is_scoped_to_project(env):
    service, records, models, calls, output = env
    first = service.route_model(TEXT, project_id="alpha", request_key="shared")
    second = service.route_model(TEXT, project_id="beta", request_key="shared")
    assert first.mode == second.mode == "model"
    assert first.turn_id != second.turn_id
    assert service.store.get_request(first.turn_id)["scope"]["project_id"] == "alpha"
    assert service.store.get_request(second.turn_id)["scope"]["project_id"] == "beta"
    assert len(calls) == 2
    assert service.route_model(TEXT, project_id="alpha", request_key="shared") == first
    assert service.route_model(TEXT, project_id="beta", request_key="shared") == second
    assert len(calls) == 2


def test_disabled_frozen_authority_is_not_upgraded_before_dispatch(env):
    service, records, models, calls, output = env
    from datetime import datetime, timezone
    from backend.memory_app.turn_routing import CONFIGURATION_FIELDS, _revision
    from backend.memory_app.v2.turn_requests import freeze_product_turn
    inputs = {"original_text": TEXT, "project_id": "alpha", "examples": [], "has_files": False}
    models.update("generation", {"allow_remote": False, "expected_revision": 1})
    request = freeze_product_turn("workbench.route", records=records, models=models,
        project_id="alpha", load_text=lambda item: "", text=json.dumps(inputs, ensure_ascii=False, sort_keys=True),
        turn_id="route-frozen-disabled", session_id="aux-frozen-disabled", operation_id="op-frozen-disabled",
        idempotency_key="frozen-disabled", created_at=datetime.now(timezone.utc).isoformat(), capabilities=[])
    assert request["privacy"]["allow_remote"] is False
    models.update("generation", {"allow_remote": True, "expected_revision": 2})
    public = models.public()["generation"]
    identity = {"project_id": "alpha", "request_key": "frozen-disabled"}
    with records.begin() as tx:
        tx.put("v2_route_turn_keys", _revision(identity), {"identity": identity, "inputs": inputs,
            "request": request, "configuration": {field: public.get(field) for field in CONFIGURATION_FIELDS}},
            expected_revision=0)
        tx.commit()
    result = service.route_model(TEXT, project_id="alpha", request_key="frozen-disabled")
    assert result.mode == "rules"
    assert calls == []


def test_mismatched_index_identity_cannot_replay_another_submission(env):
    service, records, models, calls, output = env
    from backend.memory_app.turn_routing import _revision
    assert service.route_model(TEXT, project_id="alpha", request_key="identity").mode == "model"
    key = _revision({"project_id": "alpha", "request_key": "identity"})
    with records.begin() as tx:
        old = tx.read("v2_route_turn_keys", key)
        tx.put("v2_route_turn_keys", key, {**old.payload,
            "identity": {"project_id": "alpha", "request_key": "different"}}, expected_revision=old.revision)
        tx.commit()
    with pytest.raises(ValueError, match="identity"):
        service.route_model(TEXT, project_id="alpha", request_key="identity")
    assert len(calls) == 1
