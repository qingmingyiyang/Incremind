r"""Read-only structure audit: routes, reverse imports, large files, doc drift.

Run from the repository root:

    .\.venv\Scripts\python.exe tools\structure_audit.py
    .\.venv\Scripts\python.exe tools\structure_audit.py --check

The default output is ``work/structure-audit-20261017``.  The audit never
imports the application, starts a server or touches ``runtime`` data.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    # 直接运行脚本时也从仓库工具包导入同一份收集逻辑。
    sys.path.insert(0, str(ROOT))

from tools.python_imports import collect_package_imports  # noqa: E402

SRC = ROOT / "src"
DEFAULT_OUTPUT = ROOT / "work" / "structure-audit-20261017"

ROUTE_DECORATOR = re.compile(
    r'@(?P<owner>router|app|application)\.(?P<method>get|post|put|patch|delete)\(\s*'
    r'[fru]?["\'](?P<path>[^"\']+)["\']'
)
FRONTEND_LEGACY = re.compile(r'["\'`](/api/(?:rebuild|workspace|recognition|ai)[^"\'`$\s]*)')
CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
LARGE_BYTES = 50 * 1024
LARGE_LINES = 1000


def _display(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _python_files(root: Path):
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _route_group(path: Path) -> str:
    relative = path.relative_to(SRC).as_posix()
    if relative.startswith("backend/api/"):
        return "backend.api"
    if relative.startswith("backend/memory_app/v2/"):
        return "memory_app.v2"
    if relative.startswith("backend/memory_app/"):
        return "memory_app"
    return "other"


def scan_routes() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in _python_files(SRC / "backend"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ROUTE_DECORATOR.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            rows.append(
                {
                    "group": _route_group(path),
                    "method": match.group("method").upper(),
                    "path": match.group("path"),
                    "file": _display(path),
                    "line": line,
                }
            )
    rows.sort(key=lambda row: (row["group"], row["file"], row["line"], row["path"]))
    return rows


def scan_reverse_imports() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    memory_app = SRC / "backend" / "memory_app"
    for path in _python_files(memory_app):
        for line, module in collect_package_imports(path, SRC, "backend.api"):
            rows.append(
                {
                    "file": _display(path),
                    "line": line,
                    "module": module,
                }
            )
    rows.sort(key=lambda row: (row["file"], row["line"], row["module"]))
    return rows


def scan_large_files() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        size = path.stat().st_size
        lines = path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        if size <= LARGE_BYTES and lines <= LARGE_LINES:
            continue
        rows.append({"file": _display(path), "bytes": size, "lines": lines})
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows


def scan_drift() -> dict[str, object]:
    source_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in _python_files(SRC)
    )
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    importlinter = (ROOT / ".importlinter").read_text(encoding="utf-8")
    contract_names = re.findall(r"^\[importlinter:contract:([^\]]+)\]", importlinter, re.MULTILINE)
    core_contract = any(
        re.search(r"^source_modules\s*=\s*(?:\n\s+)*core\s*$", importlinter, re.MULTILINE)
        and "core must not" in importlinter
        for _ in [0]
    )
    frontend_files = sorted((SRC / "frontend" / "src").rglob("*"))
    legacy_calls: list[dict[str, object]] = []
    for path in frontend_files:
        if not path.is_file() or path.suffix not in CODE_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        paths = sorted(set(FRONTEND_LEGACY.findall(text)))
        if paths:
            legacy_calls.append({"file": _display(path), "paths": paths})
    return {
        "architecture_claims": {
            "steer_or_stop_or_approve_routes_in_source": {
                token: source_text.count(f'"/{token}"') + source_text.count(f"'/{token}'")
                for token in ("steer", "stop", "approve")
            },
            "shared_project_in_source": source_text.count("共享项目"),
            "architecture_document_mentions_shared_project": architecture.count("共享项目"),
        },
        "import_contracts": {
            "contract_count": len(contract_names),
            "names": contract_names,
            "has_core_must_not_import_backend_contract": core_contract,
        },
        "frontend_legacy_calls": {
            "files": len(legacy_calls),
            "distinct_paths": len({p for row in legacy_calls for p in row["paths"]}),
            "rows": legacy_calls,
        },
    }


THRESHOLD = re.compile(r"(?:>=|<=|>|<)\s*(\d+(?:\.\d+)?)")
CHINESE_TEXT = re.compile(r'"([^"\n]*[\u4e00-\u9fff][^"\n]{20,})"|\'([^\'\n]*[\u4e00-\u9fff][^\'\n]{20,})\'')


def scan_policy_candidates() -> list[dict[str, object]]:
    """Suspicious thresholds and instruction-like Chinese strings outside policies/."""
    rows: list[dict[str, object]] = []
    instruction_words = ("你", "请", "如果", "不要", "必须", "应该", "回答", "提取", "总结")
    for path in _python_files(SRC / "backend" / "memory_app" / "v2"):
        if "policies" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in THRESHOLD.finditer(text):
            raw = float(match.group(1))
            if not (0 < raw < 1 or raw >= 30):
                continue
            rows.append({"kind": "threshold", "value": match.group(1),
                         "file": _display(path), "line": text.count("\n", 0, match.start()) + 1})
        for match in CHINESE_TEXT.finditer(text):
            value = match.group(1) or match.group(2) or ""
            if len(value) < 30 or not any(word in value for word in instruction_words):
                continue
            if not any(punct in value for punct in ("。", "：", "，")):
                continue
            rows.append({"kind": "prompt", "value": value[:80],
                         "file": _display(path), "line": text.count("\n", 0, match.start()) + 1})
    rows.sort(key=lambda row: (row["file"], row["line"], row["kind"]))
    return rows


def scan_silent_exceptions() -> list[dict[str, object]]:
    """Broad except blocks that neither log nor re-raise."""
    rows: list[dict[str, object]] = []
    for path in _python_files(SRC / "backend" / "memory_app" / "v2"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            name = ast.unparse(node.type)
            if name not in {"Exception", "BaseException"}:
                continue
            body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            if any(token in body for token in ("logging.", "logger.", "_LOGGER.", "log.")):
                continue
            if any(isinstance(stmt, ast.Raise) for stmt in node.body):
                continue
            rows.append({"file": _display(path), "line": node.lineno, "type": name})
    rows.sort(key=lambda row: (row["file"], row["line"]))
    return rows


def scan_policy_usage() -> dict[str, object]:
    init = (SRC / "backend" / "memory_app" / "v2" / "policies" / "__init__.py").read_text(encoding="utf-8")
    block = re.search(r"ACTIVE = \{(.*?)\n\}", init, re.DOTALL)
    registered = sorted(re.findall(r"['\"]([a-z][a-z0-9_]*)['\"]\s*:", block.group(1))) if block else []
    registered_set = set(registered)
    called: set[str] = set()
    explicit: set[str] = set()
    for path in _python_files(SRC / "backend"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"from (?:\.+|backend\.memory_app\.v2\.)policies import [^\n]*\bget\b", text):
            continue
        called.update(
            name
            for name in re.findall(r"\bget\(\s*['\"]([a-z][a-z0-9_]*)['\"]\s*(?:,|\))", text)
            if name in registered_set
        )
        explicit.update(re.findall(r"\bget\(\s*['\"]([a-z][a-z0-9_]*)['\"]\s*,\s*version=", text))
    return {
        "registered": registered,
        "called": sorted(called),
        "registered_not_called": sorted(registered_set - called),
        "called_not_registered": sorted(explicit - registered_set),
    }


def _counts(rows: list[dict[str, object]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(row[key] for row in rows).items()))


def build_report(output: Path, routes, reverse, large, drift) -> str:
    route_counts = _counts(routes, "group")
    reverse_files = _counts(reverse, "file")
    lines = [
        "# 结构体检（只读）",
        "",
        f"输出目录：`{_display(output)}`",
        "",
        "## 路由台账",
        "",
        f"- 装饰器总数：{len(routes)}",
    ]
    lines += [f"- {group}：{count}" for group, count in route_counts.items()]
    lines += [
        "",
        "## 反向依赖（memory_app → backend.api）",
        "",
        f"- 导入行总数：{len(reverse)}",
        f"- 涉及文件：{len(reverse_files)}",
    ]
    lines += [f"- `{name}`：{count}" for name, count in reverse_files.items()]
    lines += [
        "",
        "## 大文件（>50KB 或 >1000 行）",
        "",
        f"- 数量：{len(large)}",
        "",
        "| 文件 | KB | 行 |",
        "| --- | ---: | ---: |",
    ]
    lines += [f"| `{row['file']}` | {row['bytes'] // 1024} | {row['lines']} |" for row in large]
    lines += [
        "",
        "## 文档漂移",
        "",
        "```json",
        json.dumps(drift, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 复跑",
        "",
        "```powershell",
        ".\\\\.venv\\\\Scripts\\\\python.exe tools\\\\structure_audit.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="print a summary without writing files")
    args = parser.parse_args()

    routes = scan_routes()
    reverse = scan_reverse_imports()
    large = scan_large_files()
    drift = scan_drift()
    policy = scan_policy_usage()
    silent = scan_silent_exceptions()
    candidates = scan_policy_candidates()

    summary = {
        "routes": len(routes),
        "route_counts": _counts(routes, "group"),
        "reverse_imports": len(reverse),
        "reverse_files": len(_counts(reverse, "file")),
        "large_files": len(large),
        "frontend_legacy_files": drift["frontend_legacy_calls"]["files"],
        "policy_registered": len(policy["registered"]),
        "policy_called": len(policy["called"]),
        "policy_registered_not_called": policy["registered_not_called"],
        "policy_called_not_registered": policy["called_not_registered"],
        "silent_exceptions": len(silent),
        "policy_candidates": len(candidates),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.check:
        return 0

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    (output / "routes.json").write_text(
        json.dumps({"counts": _counts(routes, "group"), "routes": routes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "reverse_imports.json").write_text(
        json.dumps({"count": len(reverse), "rows": reverse}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "large_files.json").write_text(
        json.dumps(large, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "drift.json").write_text(
        json.dumps(drift, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "policy_usage.json").write_text(
        json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "silent_exceptions.json").write_text(
        json.dumps(silent, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "policy_candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "REPORT.md").write_text(build_report(output, routes, reverse, large, drift), encoding="utf-8")
    print(f"written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
