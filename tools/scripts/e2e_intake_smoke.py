"""
快速记录 → Intake 已入库真实 E2E 冒烟测试。

不依赖 FastAPI / uvicorn，直接使用 SeriesWorkspace 操作临时目录，
验证整个"创建 Intake → 文件落盘 → 列表读取 → approve → promote"链路。

用法: runtime\python.exe tools\scripts\e2e_intake_smoke.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 将项目 src 加入路径（必须在业务 import 之前）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.chdir(str(PROJECT_ROOT))

from backend.replay.series_workspace import SeriesWorkspace
from backend.replay.contracts import CreateIntakeRequest, Series


def pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"name": name, "ok": ok, "detail": detail})
    print(f"  {pass_fail(ok)} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="chriptmas_e2e_"))
    print(f"测试目录: {tmp}")
    print()

    try:
        ws = SeriesWorkspace(tmp)
        print("[setup] SeriesWorkspace 初始化完成")

        # ── 创建默认系列 ──────────────────────────────────
        print()
        print("=== Step 1: 创建/获取默认系列 ===")
        series_list = ws.list_series()
        if not series_list:
            series = ws.create_series(
                CreateIntakeRequest(type="quick_note", raw_text="")
            )
            # create_series 不返回 Series 对象，我们直接创建
            ws.series_registry["default"] = {"name": "测试系列", "description": "", "color": "#ccc", "archived": False}
            ws._save_registry()
            series_id = "default"
        else:
            series_id = series_list[0].series_id

        ws.activate_series(series_id)
        series = ws.get_series(series_id)
        check("默认系列存在", series is not None, f"series_id={series_id}")
        print(f"  系列名称: {series.name}")

        # ── 创建 Intake ───────────────────────────────────
        print()
        print("=== Step 2: 快速记录 → Intake 创建 ===")
        intake = ws.create_intake(
            series_id,
            CreateIntakeRequest(
                type="quick_note",
                raw_text="这是一条 E2E 冒烟测试的快速记录。验证 Intake 创建、文件落盘和读取。",
                title="E2E 测试记录",
                tags=["e2e", "test"],
                source="manual",
            ),
        )
        check("Intake 创建成功", intake is not None, f"intake_id={intake.intake_id}")
        check("Type 正确", intake.type == "quick_note")
        check("状态为 pending", intake.status == "pending")

        # ── 验证文件落盘 ──────────────────────────────────
        print()
        print("=== Step 3: 文件落盘验证 ===")
        pending_dir = tmp / "library" / "series" / series_id / "intake" / "pending"
        intake_file = pending_dir / f"{intake.intake_id}.json"
        check("pending 目录存在", pending_dir.is_dir())
        check("intake JSON 文件存在", intake_file.is_file())
        if intake_file.is_file():
            payload = json.loads(intake_file.read_text("utf-8"))
            check("JSON 包含 raw_text", payload.get("raw_text") == intake.raw_text)
            check("JSON 包含 intake_id", payload.get("intake_id") == intake.intake_id)

        # ── 列出 Intake ───────────────────────────────────
        print()
        print("=== Step 4: Intake 列表读取 ===")
        items = ws.list_intake(series_id)
        check("列表不为空", len(items) > 0)
        check("新创建的在列表中", any(i.intake_id == intake.intake_id for i in items))

        # ── approve ───────────────────────────────────────
        print()
        print("=== Step 5: approve → promote ===")
        approved = ws.approve_intake(series_id, intake.intake_id)
        check("approve 成功", approved is not None)
        check("状态变为 approved", approved.status == "approved")

        # ── promote → knowledge item ──────────────────────
        promoted = ws.promote_intake(series_id, intake.intake_id)
        check("promote 成功", promoted is not None)
        check("状态变为 merged", promoted.status == "merged")

        # ── Verify promoted file moved from pending → merged ──
        print()
        print("=== Step 6: 文件状态迁移验证 ===")
        merged_dir = tmp / "library" / "series" / series_id / "intake" / "merged"
        merged_file = merged_dir / f"{intake.intake_id}.json"
        check("pending 文件已移除", not intake_file.is_file())
        check("merged 文件已创建", merged_file.is_file())

        # ── cleanup ────────────────────────────────────────
        print()
        print("=== Step 7: 清理 ===")
        shutil.rmtree(tmp, ignore_errors=True)
        check("测试目录已清理", not tmp.exists())

    except Exception as e:
        print(f"\n  异常: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    # ── 汇总 ───────────────────────────────────────────
    print()
    print("=" * 50)
    passed = sum(1 for r in results if r["ok"])
    failed = sum(1 for r in results if not r["ok"])
    print(f"结果: {passed}/{len(results)} passed, {failed} failed")
    for r in results:
        if not r["ok"]:
            print(f"  FAIL {r['name']}: {r['detail']}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
