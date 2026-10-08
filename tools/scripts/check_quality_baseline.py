from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "tools" / "quality" / "ruff-baseline.json"
RUFF_SCOPE = (
    "src/backend/api",
    "src/rebuild/product_core",
    "tools",
)


def _fingerprint(item: dict[str, Any]) -> dict[str, Any]:
    filename = Path(str(item["filename"])).resolve()
    return {
        "path": filename.relative_to(ROOT).as_posix(),
        "code": str(item["code"]),
        "message": str(item["message"]),
        "row": int(item["location"]["row"]),
        "column": int(item["location"]["column"]),
    }


def _findings() -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *RUFF_SCOPE,
            "--config",
            "ruff.toml",
            "--output-format",
            "json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "ruff did not complete")
    payload = json.loads(result.stdout or "[]")
    return sorted((_fingerprint(item) for item in payload), key=lambda item: tuple(item.values()))


def _read_baseline() -> list[dict[str, Any]]:
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"quality baseline is missing: {BASELINE_PATH.relative_to(ROOT)}") from error
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise RuntimeError("quality baseline must be a JSON array of findings")
    return sorted(payload, key=lambda item: tuple(item.values()))


def _write_baseline(findings: list[dict[str, Any]]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(f"{json.dumps(findings, ensure_ascii=False, indent=2)}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail when Ruff finds violations beyond the committed Phase 0 baseline.")
    parser.add_argument("--write-baseline", action="store_true", help="refresh the baseline explicitly after review")
    args = parser.parse_args()
    findings = _findings()
    if args.write_baseline:
        _write_baseline(findings)
        print(f"[quality] wrote {len(findings)} Ruff baseline findings")
        return 0
    baseline = _read_baseline()
    baseline_keys = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in baseline}
    current_keys = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in findings}
    introduced = sorted(current_keys - baseline_keys)
    if introduced:
        print("[quality] new Ruff findings exceed the committed baseline:")
        for item in introduced:
            print(json.dumps(json.loads(item), ensure_ascii=False))
        return 1
    resolved = len(baseline_keys - current_keys)
    print(f"[quality] Ruff baseline respected ({len(findings)} current findings, {resolved} resolved baseline findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
