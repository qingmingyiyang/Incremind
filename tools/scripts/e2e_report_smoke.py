"""
报告 Markdown 读写 + revision 乐观锁 E2E 冒烟测试。

不依赖 FastAPI / uvicorn，直接使用 SeriesWorkspace 操作临时目录，
验证"创建报告 → Markdown 写入 → 读取 → revision 校验 → 重新读取"链路。

用法: runtime\python.exe tools\scripts\e2e_report_smoke.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.chdir(str(PROJECT_ROOT))

from backend.replay.series_workspace import SeriesWorkspace
from backend.replay.contracts import Report, DateRange
from backend.replay.library import ReplayLibrary


results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"name": name, "ok": ok, "detail": detail})
    tag = "PASS" if ok else "FAIL"
    print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="chriptmas_e2e_report_"))
    print(f"测试目录: {tmp}")
    print()

    try:
        ws = SeriesWorkspace(tmp)
        print("[setup] SeriesWorkspace 初始化完成")

        # ── 确保默认系列存在 ──────────────────────────────
        print()
        print("=== Step 1: 获取/创建默认系列 ===")
        series_list = ws.list_series()
        if series_list:
            series_id = series_list[0].series_id
        else:
            series_id = "default"
            ws.series_registry["default"] = {
                "name": "测试系列", "description": "", "color": "#ccc", "archived": False
            }
            ws._save_registry()
        ws.activate_series(series_id)
        series = ws.get_series(series_id)
        check("默认系列存在", series is not None, f"series_id={series_id}")
        print(f"  系列: {series.name}")

        # ── 创建报告 ──────────────────────────────────────
        print()
        print("=== Step 2: 创建报告（save_report）===")
        library: ReplayLibrary = ws.replay_library(series_id)

        report_id = "daily_2026-06-26"
        test_markdown = (
            "# 2026-06-26 日报\n\n"
            "## 今日完成\n\n"
            "- 功能 A 已上线\n"
            "- 功能 B 开发中\n\n"
            "## 明日计划\n\n"
            "- 修复 bug C\n"
        )
        report = Report(
            report_id=report_id,
            series_id=series_id,
            type="daily",
            title="2026-06-26 日报",
            date_range=DateRange(start="2026-06-26", end="2026-06-26"),
        )
        ws.replay_library(series_id).save_report(report, test_markdown)
        check("save_report 完成", True)

        # ── 验证文件落盘 ──────────────────────────────────
        print()
        print("=== Step 3: 文件落盘验证 ===")
        series_dir = tmp / "library" / "series" / series_id
        reports_dir = series_dir / "reports" / "daily"
        slug = "2026-06-26"
        md_file = reports_dir / f"{slug}.md"
        json_file = reports_dir / f"{slug}.json"
        index_file = series_dir / "knowledge" / "indexes" / "reports" / f"{report_id}.json"

        check("reports/daily/ 目录存在", reports_dir.is_dir())
        check("Markdown 文件存在", md_file.is_file())
        check("JSON 文件存在", json_file.is_file())
        if md_file.is_file():
            persisted = md_file.read_text("utf-8")
            check("Markdown 内容一致", persisted.rstrip() == test_markdown.rstrip())
        if json_file.is_file():
            meta = json.loads(json_file.read_text("utf-8"))
            check("JSON 包含 report_id", meta.get("report_id") == report_id)
            check("JSON 包含 type=daily", meta.get("type") == "daily")
        check("索引文件存在", index_file.is_file())

        # ── 通过 SeriesWorkspace 读取 Markdown ────────────
        print()
        print("=== Step 4: 读取报告 Markdown + revision ===")
        doc = ws.read_report_markdown(series_id, report_id)
        check("read_report_markdown 返回非空", doc is not None)
        check("series_id 正确", doc.series_id == series_id)
        check("report_id 正确", doc.report_id == report_id)
        check("markdown 内容一致", doc.markdown.rstrip() == test_markdown.rstrip())

        expected_revision = hashlib.sha256(test_markdown.encode("utf-8")).hexdigest()
        check("revision 匹配 sha256", doc.revision == expected_revision)

        # ── updated_at 验证 ─────────────────────────────-
        print()
        print("=== Step 4b: updated_at 时间戳验证 ===")
        import re
        iso8601_re = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$"
        )
        check("updated_at 不为空", bool(doc.updated_at))
        check("updated_at 格式为 ISO 8601", bool(iso8601_re.match(doc.updated_at)),
              doc.updated_at)

        # ── 写入（更新）Markdown ──────────────────────────
        print()
        print("=== Step 5: 写入 Markdown + revision 乐观锁 ===")
        updated = "# 2026-06-26 日报（更新版）\n\n已更新内容。\n"
        result = ws.write_report_markdown(series_id, report_id, updated, base_revision=doc.revision)
        check("写入成功", result is not None)
        check("新 markdown 已更新", result.markdown.rstrip() == updated.rstrip())

        new_revision = hashlib.sha256(updated.encode("utf-8")).hexdigest()
        check("新 revision 匹配", result.revision == new_revision)

        # ── 历史版本验证 ──────────────────────────────────
        print()
        print("=== Step 6: 历史版本验证 ===")
        history_dir = series_dir / "reports" / "history" / report_id
        check("history 目录存在", history_dir.is_dir())
        versions = sorted(history_dir.glob("*.md"))
        check("至少有 1 个历史版本", len(versions) >= 1)
        if versions:
            saved_original = versions[0].read_text("utf-8")
            check("历史文件内容为写入前版本", saved_original.rstrip() == test_markdown.rstrip())

        # ── 乐观锁冲突检测 ────────────────────────────────
        print()
        print("=== Step 7: 乐观锁冲突检测 ===")
        try:
            ws.write_report_markdown(series_id, report_id, "冲突写入", base_revision=doc.revision)
            check("旧 revision 写入应拒绝", False, "未抛异常")
        except RuntimeError:
            check("旧 revision 被正确拒绝", True, "RuntimeError: 报告已被其他操作修改")
        except Exception as e:
            check("旧 revision 被拒绝", True, f"异常: {type(e).__name__}: {e}")

        # ── 报告列表读取 ──────────────────────────────────
        print()
        print("=== Step 8: 报告列表 ===")
        lib = ws.replay_library(series_id)
        reports = lib.list_reports()
        check("列表不为空", len(reports) > 0)
        check("新报告在列表中", any(r.report_id == report_id for r in reports))

        # ── cleanup ────────────────────────────────────────
        print()
        print("=== Step 9: 清理 ===")
        shutil.rmtree(tmp, ignore_errors=True)
        check("测试目录已清理", not tmp.exists())

    except Exception as e:
        print(f"\n  异常: {e}")
        import traceback
        traceback.print_exc()
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
