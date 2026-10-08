"""Offline workbench routing evaluation; no runtime data or model calls.

CLI: python tools/route_eval.py [--cases PATH] [--predictions PATH] [--output PATH]
Predictions use {"version": 1, "cases": [{"id": ..., "parts": [...]}]}.
Missing case IDs are empty predictions; unknown/duplicate IDs are input errors.
Intent accuracy is exact intent SET equality per case. Span accuracy counts
one-to-one parts with equal intent and span after stripping ONLY boundary
Unicode punctuation/whitespace. Dependency accuracy counts those matched parts
whose full dependency sets match after index remapping. Both part metrics use
sum(max(expected_count, predicted_count)) as denominator, including unmatched
and extra parts. Equivalent part permutations are accepted; ambiguous matches
maximize span matches first, then dependency matches. Instructions are not scored.

The baseline calls the actual route_intent(text, has_files), then uses the same
parse_scope_tag-cleaned body as workbench.execute_one. If removing a middle tag
produces a noncontiguous body, the baseline keeps the full original text and
marks baseline_span_fallback; it never invents a split or concatenated span.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.memory_app.v2.intent import parse_scope_tag, route_intent
from backend.memory_app.v2.policies import get, override, parse_overrides

INTENTS = {"remember", "ask", "do", "inspiration"}
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "workbench_route" / "cases.json"


def normalize_span(span):
    """Preserve all interior text, symbols, and letters exactly."""
    def boundary(char):
        return char.isspace() or unicodedata.category(char).startswith("P")
    start, end = 0, len(span)
    while start < end and boundary(span[start]):
        start += 1
    while end > start and boundary(span[end - 1]):
        end -= 1
    return span[start:end]


def _nonoverlapping_spans(text, parts):
    """Find a valid occurrence assignment, including repeated literal spans."""
    def place(index, occupied):
        if index == len(parts):
            return True
        span = parts[index]["span"]
        if not span:  # The validated file-only case has no text interval.
            return place(index + 1, occupied)
        start = text.find(span)
        while start >= 0:
            end = start + len(span)
            if all(end <= left or start >= right for left, right in occupied):
                if place(index + 1, occupied + [(start, end)]):
                    return True
            start = text.find(span, start + 1)
        return False
    return place(0, [])


def _rows(document, *, gold):
    if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("document version must be integer 1")
    rows = document.get("cases")
    if not isinstance(rows, list) or (gold and not rows):
        raise ValueError("cases must be a list, nonempty for the evaluation fixture")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("each case needs a nonempty string id")
        key = row["id"]
        if key in indexed:
            raise ValueError(f"duplicate case id: {key}")
        if gold:
            if not isinstance(row.get("text"), str) or type(row.get("has_files")) is not bool:
                raise ValueError(f"{key}: text must be string and has_files must be boolean")
            if not isinstance(row.get("category"), str) or not row["category"]:
                raise ValueError(f"{key}: category is required")
            if not isinstance(row.get("tags"), list) or any(not isinstance(tag, str) for tag in row["tags"]):
                raise ValueError(f"{key}: tags must be strings")
            if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
                raise ValueError(f"{key}: rationale is required")
        parts = row.get("parts")
        if not isinstance(parts, list) or len(parts) > (3 if gold else 32) or (gold and not parts):
            raise ValueError(f"{key}: expected 1-3 gold parts or 0-32 predicted parts")
        for index, part in enumerate(parts):
            if not isinstance(part, dict) or not isinstance(part.get("intent"), str) or part["intent"] not in INTENTS:
                raise ValueError(f"{key}: invalid intent")
            if not isinstance(part.get("span"), str):
                raise ValueError(f"{key}: span must be string")
            if "instruction" not in part or (part["instruction"] is not None and not isinstance(part["instruction"], str)):
                raise ValueError(f"{key}: instruction must be null or string")
            if 'situation' in part and part['situation'] is not None:
                if (part['intent'] not in {'ask', 'do'} or not isinstance(part['situation'], str)
                        or len(part['situation']) > 60):
                    raise ValueError(f'{key}: invalid situation')
            deps = part.get("depends_on")
            if not isinstance(deps, list) or any(type(dep) is not int or dep < 0 or dep >= len(parts) for dep in deps) or len(set(deps)) != len(deps):
                raise ValueError(f"{key}: invalid dependency indices")
            if gold:
                if part["span"] not in row["text"] or (not part["span"] and not (row["text"] == "" and row["has_files"])):
                    raise ValueError(f"{key}: gold span must be an original substring")
                if part["instruction"] is not None and (part["intent"] not in {"ask", "do"} or len(part["instruction"]) > 200):
                    raise ValueError(f"{key}: invalid gold instruction")
                for dep in deps:
                    if not isinstance(parts[dep], dict) or not isinstance(parts[dep].get("intent"), str):
                        raise ValueError(f"{key}: invalid dependency part")
                    pair = (parts[dep].get("intent"), part["intent"])
                    if pair not in {("remember", "ask"), ("remember", "do"), ("ask", "do")}:
                        raise ValueError(f"{key}: invalid gold dependency direction")
                    if parts[dep].get("depends_on"):
                        raise ValueError(f"{key}: gold dependency chain exceeds two layers")
        if gold and sum(part["intent"] == "do" for part in parts) > 1:
            raise ValueError(f"{key}: at most one do part")
        if gold and not _nonoverlapping_spans(row["text"], parts):
            raise ValueError(f"{key}: gold spans overlap")
        indexed[key] = row
    return indexed


def _match_parts(expected, predicted):
    candidates = [[j for j, guess in enumerate(predicted)
        if part["intent"] == guess["intent"] and normalize_span(part["span"]) == normalize_span(guess["span"])]
        for part in expected]
    best = (0, 0)

    def visit(index, mapping, used):
        nonlocal best
        if index == len(expected):
            dependencies = 0
            for i, j in mapping.items():
                deps = expected[i]["depends_on"]
                if all(dep in mapping for dep in deps) and {mapping[dep] for dep in deps} == set(predicted[j]["depends_on"]):
                    dependencies += 1
            best = max(best, (len(mapping), dependencies))
            return
        visit(index + 1, mapping, used)
        for j in candidates[index]:
            if j not in used:
                visit(index + 1, {**mapping, index: j}, used | {j})

    visit(0, {}, set())
    return best


def _metric(correct, total):
    return {"correct": correct, "total": total, "accuracy": correct / total if total else 0.0}


def evaluate(fixture, predictions=None):
    """Return JSON-serializable scores; None predictions evaluates real rules."""
    gold = _rows(fixture, gold=True)
    guesses = _rows(predictions, gold=False) if predictions is not None else None
    if guesses is not None and set(guesses) - set(gold):
        raise ValueError("unknown prediction case ids: " + ", ".join(sorted(set(guesses) - set(gold))))
    results = []
    for key, row in gold.items():
        fallback = False
        if guesses is None:
            _, _, body = parse_scope_tag(row["text"])
            if body not in row["text"]:
                body, fallback = row["text"], True
            predicted = [{"intent": get('route')(route_intent, row['text'], row['has_files']), "span": body,
                "instruction": None, "depends_on": []}]
        else:
            predicted = guesses.get(key, {"parts": []})["parts"]
        expected = row["parts"]
        span_correct, dependency_correct = _match_parts(expected, predicted)
        results.append({"id": key, "category": row["category"], "predicted_parts": predicted,
            "baseline_span_fallback": fallback, "intent_set_correct": {part["intent"] for part in expected} == {part["intent"] for part in predicted},
            "span_correct": span_correct, "dependency_correct": dependency_correct,
            "part_total": max(len(expected), len(predicted))})

    def aggregate(rows):
        total = sum(row["part_total"] for row in rows)
        return {"intent_set_accuracy": _metric(sum(row["intent_set_correct"] for row in rows), len(rows)),
            "span_accuracy": _metric(sum(row["span_correct"] for row in rows), total),
            "dependency_accuracy": _metric(sum(row["dependency_correct"] for row in rows), total)}

    return {"version": 1, "mode": "rules" if guesses is None else "predictions", "case_count": len(results),
        "metric_definitions": {"intent_set_accuracy": "Exact intent set equality / cases",
            "span_accuracy": "One-to-one intent + boundary-normalized span matches / sum(max(gold parts, predicted parts))",
            "dependency_accuracy": "Span-matched parts with complete remapped dependency sets / same part denominator",
            "normalization": "Strip boundary Unicode punctuation and whitespace only; preserve interior text",
            "baseline": "Old-rule evaluation projection: route_intent returns only an intent, not spans or dependencies. Use cleaned workbench body as one span; noncontiguous cleaned body uses original text and marks fallback",
            "instructions": "Not scored"},
        "metrics": aggregate(results),
        "by_category": {category: aggregate([row for row in results if row["category"] == category])
            for category in sorted({row["category"] for row in results})}, "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "work" / "qa" / "T13.0" / "baseline.json")
    parser.add_argument("--policy", action="append", default=[], metavar="INTERFACE=VERSION")
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(args.cases.read_text(encoding="utf-8-sig"))
        predictions = json.loads(args.predictions.read_text(encoding="utf-8-sig")) if args.predictions else None
        with override(**parse_overrides(args.policy)):
            report = evaluate(fixture, predictions)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "case_count": report["case_count"], "metrics": report["metrics"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
