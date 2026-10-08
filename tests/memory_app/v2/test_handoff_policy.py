"""Pure handoff output preserves qualified evidence and fits its complete bytes."""
from copy import deepcopy
from importlib import import_module
import json

import pytest

from backend.memory_app.v2 import policies
from backend.shared.llm.message_metadata import _estimate_input_tokens


def count_tokens(text):
    return _estimate_input_tokens([{"role": "user", "content": text}]) - _estimate_input_tokens(
        [{"role": "user", "content": ""}])


def entry(identity, *, layer="L3", excerpt="正文。条件之外不适用。", conditions=("在原条件内",)):
    return {"object_id": identity, "layer": layer, "title": "标题🙂", "excerpt": excerpt,
        "sources": [{"type": "original_item", "id": "source-" + identity, "revision": 2,
                     "project_id": "project-a"}], "revision": 3, "conditions": list(conditions)}


def handoff(rows, **options):
    return import_module("backend.memory_app.v2.policies.handoff").v1(
        rows, count_tokens=count_tokens, **options)


def test_default_budget_numbered_layers_and_every_metadata_field():
    rows = [entry("r1"), entry("d1", layer="L2"), entry("d2", layer="L1"), entry("s1", layer="L0")]
    result = handoff(rows)
    assert result["version"] == "handoff@1"
    assert result["budget"] == 3000
    assert result["entries"] == [{"id": f"M{n}", **row} for n, row in enumerate(rows, 1)]
    assert result["profile"] == []
    assert json.loads(result["text"]) == {"entries": result["entries"], "profile": []}
    assert result["tokens"] == count_tokens(result["text"]) <= 3000


def test_profile_is_separate_with_independent_identifiers_and_atomic_conditions():
    row, person = entry("r1"), entry("me1", conditions=("只在写作时", "禁止推断事实"))
    result = handoff([row], profile=[person])
    assert result["entries"] == [{"id": "M1", **row}]
    assert result["profile"] == [{"id": "P1", **person}]
    assert result["tokens"] == count_tokens(result["text"]) <= result["budget"]


def test_identical_input_is_byte_deterministic_and_both_directions_are_detached():
    rows, people = [entry("r1", excerpt="中文🙂\r\n引文\r不加末尾换行")], [entry("me1")]
    before = deepcopy((rows, people))
    first, second = handoff(rows, profile=people), handoff(rows, profile=people)
    assert first == second
    assert first["text"].encode("utf-8") == second["text"].encode("utf-8")
    assert (rows, people) == before
    first["entries"][0]["sources"][0]["id"] = "changed-output"
    first["profile"][0]["conditions"].append("changed-output")
    assert (rows, people) == before
    rows[0]["sources"][0]["id"] = "changed-input"
    people[0]["conditions"].append("changed-input")
    assert second["entries"][0]["sources"] == before[0][0]["sources"]
    assert second["profile"][0]["conditions"] == before[1][0]["conditions"]


def test_exact_full_serialized_budget_and_one_less_never_truncate_conditions():
    row = entry("r1", conditions=("条件" * 100, "完整否定和例外"))
    complete = handoff([row])
    assert handoff([row], budget=complete["tokens"])["entries"] == [{"id": "M1", **row}]
    omitted = handoff([row], budget=complete["tokens"] - 1)
    assert omitted["entries"] == []
    assert omitted["text"] == ""
    assert omitted["tokens"] == 0


def test_oversized_row_is_omitted_whole_and_next_fitting_row_has_no_number_gap():
    huge, small = entry("huge", excerpt="大" * 13000), entry("small")
    result = handoff([huge, small], budget=250)
    assert result["entries"] == [{"id": "M1", **small}]
    assert result["tokens"] == count_tokens(result["text"]) <= 250


def test_profile_and_metadata_share_budget_and_profile_is_admitted_first():
    person, row = entry("me1"), entry("r1")
    profile_only = handoff([], profile=[person])
    result = handoff([row], profile=[person], budget=profile_only["tokens"])
    assert result["profile"] == [{"id": "P1", **person}]
    assert result["entries"] == []
    assert result["tokens"] == count_tokens(result["text"]) <= result["budget"]


def test_maximum_budget_is_capped_and_large_content_remains_within_it():
    result = handoff([entry("large", excerpt="中" * 13000), entry("small")], budget=50000)
    assert result["budget"] == 12000
    assert result["entries"] == [{"id": "M1", **entry("small")}]
    assert result["tokens"] == count_tokens(result["text"]) <= 12000


def test_empty_and_tiny_budget_need_no_envelope_or_registry_changes():
    active, registered = dict(policies.ACTIVE), deepcopy(policies._REGISTRY)
    assert handoff([], budget=1) == {"version": "handoff@1", "budget": 1,
        "entries": [], "profile": [], "tokens": 0, "text": ""}
    assert handoff([entry("r1")], budget=1)["entries"] == []
    assert policies.ACTIVE == active
    assert policies._REGISTRY == registered
    assert policies.ACTIVE["handoff"] == "@1"


@pytest.mark.parametrize("budget", [0, -1, True, 3.5, "3000", None])
def test_invalid_budgets_are_rejected(budget):
    with pytest.raises(ValueError, match="invalid_handoff_budget"):
        handoff([entry("r1")], budget=budget)


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"revision": 0}, {"layer": "unknown"}, {"layer": []}, {"layer": {}}, {"object_id": ""},
    {"conditions": "incomplete"}, {"conditions": ["valid", 2]}, {"sources": []},
    {"excerpt": 3}, {"extra": "must not silently discard"},
])
def test_invalid_entry_metadata_cannot_be_silently_rewritten(changes):
    with pytest.raises(ValueError, match="invalid_handoff_entry"):
        handoff([{**entry("r1"), **changes}])


def test_source_revisions_and_unsupported_json_are_rejected_before_output():
    for revision in (True, 0, "2"):
        row = entry("r1")
        row["sources"][0]["revision"] = revision
        with pytest.raises(ValueError, match="invalid_handoff_entry"):
            handoff([row])
    row = entry("r1")
    row["sources"][0]["weight"] = float("nan")
    with pytest.raises(ValueError, match="invalid_handoff_entry"):
        handoff([row])


def test_bad_caller_counter_cannot_claim_a_budget_or_return_partial_output():
    module = import_module("backend.memory_app.v2.policies.handoff")
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError, match="invalid_handoff_token_count"):
            module.v1([entry("r1")], count_tokens=lambda text: invalid)
