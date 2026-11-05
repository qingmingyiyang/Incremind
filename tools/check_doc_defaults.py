from __future__ import annotations

import argparse
import ast
from collections import Counter
from pathlib import Path
import re
from collections.abc import Mapping


DOCUMENTS = ("README.md", "README.zh-CN.md", "ARCHITECTURE.md")
POLICY_PATH = "src/backend/memory_app/v2/policies/__init__.py"
INPUT_PATHS = (*DOCUMENTS, POLICY_PATH)
INTERFACES = ("compose", "ladder", "retrieve", "rerank")
START_MARKER = "<!-- current-policy-defaults:start -->"
END_MARKER = "<!-- current-policy-defaults:end -->"
_VERSION = re.compile(r"@[1-9][0-9]*\Z")
_TOKEN = re.compile(r"(?<![\w@])[a-z][a-z0-9_]*@[^\s`/]+")


def read_inputs(root: Path) -> dict[str, str]:
    return {name: (root / name).read_text(encoding="utf-8") for name in INPUT_PATHS}


def read_active(source: str) -> dict[str, str]:
    # 用 Python 自带解析器读取登记值，检查时不加载应用和可选模型依赖。
    assignments = [node.value for node in ast.parse(source).body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "ACTIVE"
                           for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError("expected exactly one literal ACTIVE assignment")
    active = ast.literal_eval(assignments[0])
    if not isinstance(active, dict):
        raise ValueError("ACTIVE must be a dictionary")
    selected = {name: active.get(name) for name in INTERFACES}
    if any(not isinstance(value, str) or not _VERSION.fullmatch(value)
           for value in selected.values()):
        raise ValueError("ACTIVE requires valid compose, ladder, retrieve and rerank versions")
    return selected


def check_defaults(inputs: Mapping[str, str]) -> list[str]:
    try:
        active = read_active(inputs[POLICY_PATH])
    except (KeyError, SyntaxError, ValueError, TypeError) as error:
        return [f"{POLICY_PATH}: {error}"]
    expected = " / ".join(name + active[name] for name in INTERFACES)
    errors = []
    for document in DOCUMENTS:
        source = inputs.get(document, "")
        if source.count(START_MARKER) != 1 or source.count(END_MARKER) != 1:
            errors.append(f"{document}: expected exactly one marked current-defaults block")
            continue
        start = source.index(START_MARKER) + len(START_MARKER)
        end = source.index(END_MARKER)
        if end <= start:
            errors.append(f"{document}: current-defaults markers are reversed or empty")
            continue
        block = source[start:end]
        tokens = _TOKEN.findall(block)
        if Counter(tokens) != Counter(name + active[name] for name in INTERFACES):
            actual = " / ".join(tokens) or "(none)"
            errors.append(f"{document}: current defaults {actual}; expected ACTIVE {expected}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check only marked current policy defaults against ACTIVE.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        errors = check_defaults(read_inputs(args.root))
    except OSError as error:
        errors = [str(error)]
    for error in errors:
        print("check_doc_defaults: " + error)
    if errors:
        return 1
    print("check_doc_defaults: marked defaults in all three documents match ACTIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
