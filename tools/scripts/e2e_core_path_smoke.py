"""
快速记录 → Intake → Knowledge Item → 日报 AI 合并的 API 级 E2E。

使用临时资料库和 FastAPI TestClient，运行真实路由、SeriesWorkspace 与文件写入。
AI 网关使用确定性本地替身，只替代外部模型调用，不跳过合并与手动文本保护逻辑。

用法: runtime\python.exe tools\scripts\e2e_core_path_smoke.py
"""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.chdir(PROJECT_ROOT)

from backend.api.app import create_app
import backend.api.routes.series as series_routes


class MergeGateway:
    result = ""

    def complete_text(self, messages, *, temperature=0, max_tokens=None, timeout=None):
        if "BASE_MARKDOWN" not in messages[0]["content"]:
            raise AssertionError("合并 Prompt 缺少 BASE_MARKDOWN")
        return self.result


checks = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not condition:
        raise AssertionError(f"{name}: {detail or '条件不成立'}")
    print(f"  PASS {name}" + (f" — {detail}" if detail else ""))


def require_status(response, expected: int, name: str):
    check(name, response.status_code == expected, f"{response.status_code}: {response.text}")
    return response


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="chriptmas_core_path_"))
    gateway = MergeGateway()
    original_gateway = series_routes._merge_gateway
    series_routes._merge_gateway = lambda container, model="": gateway
    today = date.today().isoformat()
    print(f"临时资料库: {root}")

    try:
        app = create_app(SimpleNamespace(root_dir=root))
        with TestClient(app) as client:
            print("\n=== 1. 系列隔离基线 ===")
            series = require_status(client.get("/api/series"), 200, "读取系列").json()
            check("默认系列存在", series[0]["series_id"] == "default")
            other = require_status(
                client.post("/api/series", json={"name": "隔离对照系列"}),
                201,
                "创建隔离对照系列",
            ).json()
            other_id = other["series_id"]

            print("\n=== 2. 快速记录进入待整理区 ===")
            raw_text = "完成核心路径验收，并记录报告合并失败时不得覆盖用户文本。"
            created = require_status(
                client.post(
                    "/api/series/default/intake",
                    json={
                        "type": "quick_note",
                        "title": "核心路径验收",
                        "raw_text": raw_text,
                        "tags": ["e2e", "核心路径"],
                        "links": ["https://example.com/evidence", "https://example.com/evidence"],
                        "source": "manual",
                    },
                ),
                201,
                "创建快速记录",
            ).json()
            intake_id = created["intake_id"]
            check("默认进入 pending", created["status"] == "pending")
            check("原文未丢失", created["raw_text"] == raw_text)
            check("链接去重后保留", created["links"] == ["https://example.com/evidence"])
            check(
                "未静默写入正式知识库",
                client.get("/api/series/default/knowledge-items").json() == [],
            )
            require_status(
                client.get(f"/api/series/{other_id}/intake/{intake_id}"),
                404,
                "其他系列无法读取 Intake",
            )

            pending_file = (
                root / "library" / "series" / "default" / "intake" / "pending" / f"{intake_id}.json"
            )
            check("pending JSON 已落盘", pending_file.is_file())

            print("\n=== 3. 编辑、确认并正式入库 ===")
            edited = require_status(
                client.patch(
                    f"/api/series/default/intake/{intake_id}",
                    json={
                        "title": "核心路径验收记录",
                        "structured_text": "## 完成事项\n\n- 核心路径验收\n\n## 问题记录\n\n- 失败写回保护\n",
                        "summary": "验证从快速记录到正式知识条目的完整路径。",
                        "tags": ["e2e", "知识入库"],
                    },
                ),
                200,
                "编辑 Intake",
            ).json()
            check("编辑不覆盖原文", edited["raw_text"] == raw_text)
            approved = require_status(
                client.post(f"/api/series/default/intake/{intake_id}/approve"),
                200,
                "确认 Intake",
            ).json()
            check("状态变为 approved", approved["status"] == "approved")
            promoted = require_status(
                client.post(f"/api/series/default/intake/{intake_id}/promote"),
                200,
                "正式入库",
            ).json()
            check("入库后状态为 merged", promoted["status"] == "merged")
            check("返回 Knowledge Item 关联", bool(promoted["knowledge_item_id"]))
            knowledge_item_id = promoted["knowledge_item_id"]
            items = client.get("/api/series/default/knowledge-items").json()
            check("Knowledge Item 已生成", len(items) == 1)
            check("Knowledge Item 属于当前系列", items[0]["series_id"] == "default")
            check("其他系列仍无知识项", client.get(f"/api/series/{other_id}/knowledge-items").json() == [])

            print("\n=== 4. 日报 dry_run 与正式 AI 合并 ===")
            report = require_status(
                client.post(f"/api/series/default/reports/daily/{today}"),
                200,
                "创建今日日报",
            ).json()
            report_id = report["report_id"]
            document = require_status(
                client.get(f"/api/series/default/reports/{report_id}/markdown"),
                200,
                "读取日报 Markdown",
            ).json()
            manual_line = "用户手动保留行：这行不能被 AI 删除。"
            base_markdown = document["markdown"].rstrip() + f"\n\n{manual_line}\n"

            candidate = require_status(
                client.post(
                    "/api/series/default/intake",
                    json={
                        "type": "quick_note",
                        "title": "日报候选",
                        "raw_text": "新增事实：核心路径已完成运行验证。",
                        "suggested_report_type": "daily",
                    },
                ),
                201,
                "创建日报候选",
            ).json()
            candidate_id = candidate["intake_id"]
            gateway.result = base_markdown + "\n## AI 合并内容\n\n- 新增事实：核心路径已完成运行验证。\n"
            merge_payload = {
                "base_markdown": base_markdown,
                "incoming_items": [candidate_id],
                "merge_mode": "preserve_manual_edits",
                "dry_run": True,
                "base_revision": document["revision"],
            }
            preview = require_status(
                client.post(f"/api/series/default/reports/{report_id}/merge", json=merge_payload),
                200,
                "AI 合并 dry_run",
            ).json()
            check("预览保留手动文本", manual_line in preview["merged_markdown"])
            check(
                "dry_run 不写回报告",
                manual_line
                not in client.get(f"/api/series/default/reports/{report_id}/markdown").json()["markdown"],
            )
            check(
                "dry_run 不改变 Intake 状态",
                client.get(f"/api/series/default/intake/{candidate_id}").json()["status"] == "pending",
            )
            check(
                "dry_run 不提前写入 report_id",
                client.get(f"/api/series/default/intake/{candidate_id}").json()["report_id"] == "",
            )

            merge_payload["dry_run"] = False
            saved = require_status(
                client.post(f"/api/series/default/reports/{report_id}/merge", json=merge_payload),
                200,
                "AI 合并正式写回",
            ).json()
            check("合并结果标记 changed", saved["changed"] is True)
            persisted = client.get(f"/api/series/default/reports/{report_id}/markdown").json()
            check("正式写回保留手动文本", manual_line in persisted["markdown"])
            check("正式写回包含新增事实", "核心路径已完成运行验证" in persisted["markdown"])
            merged_candidate = client.get(f"/api/series/default/intake/{candidate_id}").json()
            check("成功后 Intake 变为 merged", merged_candidate["status"] == "merged")
            check("成功后 Intake 记录目标报告", merged_candidate["report_id"] == report_id)

            print("\n=== 5. 失败合并不覆盖报告或 Intake ===")
            failed_candidate = client.post(
                "/api/series/default/intake",
                json={"type": "quick_note", "title": "失败候选", "raw_text": "不得写入的候选事实"},
            ).json()
            failed_id = failed_candidate["intake_id"]
            before_failure = persisted["markdown"]
            gateway.result = "# 只剩 AI 内容\n\n- 手动文本已被删除\n"
            failed_payload = {
                "base_markdown": before_failure,
                "incoming_items": [failed_id],
                "dry_run": False,
                "base_revision": persisted["revision"],
            }
            require_status(
                client.post(f"/api/series/default/reports/{report_id}/merge", json=failed_payload),
                409,
                "缺失手动文本的合并被拒绝",
            )
            after_failure = client.get(f"/api/series/default/reports/{report_id}/markdown").json()
            check("失败后报告字节不变", after_failure["markdown"] == before_failure)
            check("失败后 revision 不变", after_failure["revision"] == persisted["revision"])
            check(
                "失败后 Intake 保持 pending",
                client.get(f"/api/series/default/intake/{failed_id}").json()["status"] == "pending",
            )
            check(
                "失败后 Intake 不记录 report_id",
                client.get(f"/api/series/default/intake/{failed_id}").json()["report_id"] == "",
            )
            check(
                "失败后 Intake 记录可审计 warning",
                "AI 合并失败，报告与待整理项未修改。"
                in client.get(f"/api/series/default/intake/{failed_id}").json()["warnings"],
            )
            check(
                "关闭前 Knowledge Item 仍存在",
                knowledge_item_id
                in {
                    item["id"]
                    for item in client.get("/api/series/default/knowledge-items").json()
                },
            )
            check(
                "日报自身登记为报告知识项",
                f"report_{report_id}"
                in {
                    item["id"]
                    for item in client.get("/api/series/default/knowledge-items").json()
                },
            )
            require_status(
                client.get(f"/api/series/{other_id}/reports/{report_id}/markdown"),
                404,
                "其他系列无法读取日报",
            )

        print("\n=== 6. 重启后持久化与本地可审计性 ===")
        with TestClient(create_app(SimpleNamespace(root_dir=root))) as restarted:
            report_after_restart = require_status(
                restarted.get(f"/api/series/default/reports/{report_id}/markdown"),
                200,
                "重启后读取日报",
            ).json()
            check("重启后 Markdown 保持一致", report_after_restart["markdown"] == persisted["markdown"])
            check(
                "重启后 Knowledge Item 保持存在",
                knowledge_item_id
                in {
                    item["id"]
                    for item in restarted.get("/api/series/default/knowledge-items").json()
                },
            )
            check(
                "重启后 Intake 状态保持 merged",
                restarted.get(f"/api/series/default/intake/{candidate_id}").json()["status"] == "merged",
            )
            check(
                "重启后报告关联保持存在",
                restarted.get(f"/api/series/default/intake/{candidate_id}").json()["report_id"]
                == report_id,
            )

        series_root = root / "library" / "series" / "default"
        report_md = next((series_root / "reports" / "daily").glob("*.md"))
        report_json = next((series_root / "reports" / "daily").glob("*.json"))
        metadata = json.loads(report_json.read_text(encoding="utf-8"))
        check("Markdown 正文可直接读取", manual_line in report_md.read_text(encoding="utf-8"))
        check("JSON 不重复保存 Markdown 正文", "markdown" not in metadata)
        check(
            "系列内无跨系列 ID 污染",
            all(
                json.loads(path.read_text(encoding="utf-8")).get("series_id") == "default"
                for path in series_root.rglob("*.json")
                if "series_id" in json.loads(path.read_text(encoding="utf-8"))
            ),
        )

        print(f"\n结果: {checks}/{checks} passed")
        return 0
    except Exception as error:
        print(f"\nFAIL: {type(error).__name__}: {error}")
        return 1
    finally:
        series_routes._merge_gateway = original_gateway
        shutil.rmtree(root, ignore_errors=True)
        print(f"临时资料库已清理: {not root.exists()}")


if __name__ == "__main__":
    raise SystemExit(main())
