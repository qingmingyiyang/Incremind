from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.bootstrap import build_api_container


DEFAULT_BVID = "BV1Tejc6oEik"
DEFAULT_URL = f"https://www.bilibili.com/video/{DEFAULT_BVID}/"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate an existing real Bilibili record through the complete replay chain.",
    )
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--bvid", default=DEFAULT_BVID)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--series-id", default="default")
    parser.add_argument(
        "--live-resolve",
        action="store_true",
        help="Resolve the source URL against Bilibili before validating local artifacts.",
    )
    return parser.parse_args()


def _json(response, label: str) -> Any:
    if response.status_code != 200:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}: {response.text}")
    return response.json()


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _require_file(path: Path, message: str) -> Path:
    _require(path.is_file() and path.stat().st_size > 0, f"{message}: {path}")
    return path


def _validate_artifacts(root: Path, detail: dict[str, Any], bvid: str, url: str) -> dict[str, Any]:
    record = detail["record"]
    structured = detail["structured"]
    _require(record["id"] == bvid and record["bvid"] == bvid, "record BVID mismatch")
    _require(record["source_url"] == url, "record source URL mismatch")
    _require(record["status"] == "completed" and record["progress"] == 100.0, "record is incomplete")
    _require(record["transcript_source"] == "whisper", "expected the verified Whisper fallback")
    _require(record["official_subtitle_available"] is False, "unexpected official subtitle artifact")
    _require(record["asr_available"] is True, "ASR artifact is unavailable")
    _require(record["cleaned_transcript_available"] is True, "cleaned transcript is unavailable")
    _require(record["summary_available"] is True, "summary artifact is unavailable")
    _require(record["visual_available"] is True, "visual artifact is unavailable")
    _require(record["index_available"] is True, "index artifact is unavailable")
    _require(record["export_available"] is True, "Markdown export is unavailable")

    record_dir = (root / "library" / record["relative_dir"]).resolve()
    library_root = (root / "library").resolve()
    record_dir.relative_to(library_root)
    _require_file(record_dir / record["media_file"], "source media is missing")
    _require_file(record_dir / record["cover_file"], "cover is missing")
    _require_file(record_dir / record["summary_file"], "summary Markdown is missing")
    _require_file(record_dir / record["transcript_file"], "transcript Markdown is missing")

    sources = structured["sources"]
    chunks = structured["chunks"]
    timeline = structured["timeline"]
    claims = structured["claims"]
    actions = structured["actions"]
    visual = structured["visual_analysis"]
    _require(len(structured) == 12, "structured.json does not have the fixed 12 top-level fields")
    _require(sources["official_subtitle"]["available"] is False, "official subtitle state mismatch")
    _require(sources["asr_transcript"]["available"] is True, "structured ASR state mismatch")
    _require(sources["asr_transcript"]["segment_count"] == 557, "ASR segment count mismatch")
    _require(sources["cleaned_transcript"]["segment_count"] == 557, "cleaned segment count mismatch")
    _require(len(chunks) == 8, "chunk count mismatch")
    _require(len(timeline) == 21, "timeline count mismatch")
    _require(len(claims) == 66, "claim count mismatch")
    _require(len(actions) == 12, "action count mismatch")
    _require(visual["visual_importance"] == "high", "visual importance mismatch")
    _require(visual["sampled_frame_count"] == 56, "sampled frame count mismatch")
    _require(len(visual["keyframes"]) == 12, "keyframe count mismatch")
    _require(visual["cloud_vision_mode"] == "mock" and visual["is_mock"] is True, "Mock Vision label mismatch")
    for frame in visual["keyframes"]:
        _require_file(record_dir / frame["file"], f"keyframe {frame['frame_id']} is missing")

    return {
        "record_dir": str(record_dir),
        "duration_seconds": record["duration_seconds"],
        "transcript_source": record["transcript_source"],
        "asr_segments": sources["asr_transcript"]["segment_count"],
        "cleaned_segments": sources["cleaned_transcript"]["segment_count"],
        "chunks": len(chunks),
        "timeline_nodes": len(timeline),
        "claims": len(claims),
        "actions": len(actions),
        "sampled_frames": visual["sampled_frame_count"],
        "keyframes": len(visual["keyframes"]),
        "vision_mode": visual["cloud_vision_mode"],
    }


def _validate_replay(client: TestClient, detail: dict[str, Any], bvid: str) -> dict[str, Any]:
    imported_day = detail["record"]["imported_at"][:10]
    year, month, day = (int(value) for value in imported_day.split("-"))
    from datetime import date

    iso = date(year, month, day).isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    month_key = f"{year:04d}-{month:02d}"
    year_key = f"{year:04d}"

    item = _json(
        client.post(
            f"/api/replay/videos/{bvid}/knowledge-item",
            json={"include_in_daily": True, "high_value": True, "need_review": True},
        ),
        "video knowledge conversion",
    )
    _require(item["id"] == f"video_{bvid}", "knowledge item ID mismatch")
    _require(item["source"]["bvid"] == bvid, "knowledge item BVID mismatch")
    _require(item["status"]["in_daily"], "knowledge item was not added to the daily journal")
    _require(item["status"]["high_value"] and item["status"]["need_review"], "review flags are missing")
    _require(len(item["evidence"]["chunk_ids"]) == 8, "knowledge item lost chunk evidence")
    _require(len(item["evidence"]["frame_ids"]) == 12, "knowledge item lost frame evidence")
    _require(item["evidence"]["timestamps"] and item["evidence"]["quotes"], "knowledge item lost time or quote evidence")

    report_requests = [
        ("daily", imported_day),
        ("weekly", week),
        ("monthly", month_key),
        ("yearly", year_key),
    ]
    reports: dict[str, dict[str, Any]] = {}
    for report_type, period in report_requests:
        report = _json(client.post(f"/api/replay/reports/{report_type}/{period}"), f"{report_type} report")
        reports[report_type] = report
        _require(report["type"] == report_type, f"{report_type} report type mismatch")
        video = next((entry for entry in report["video_knowledge"] if entry["bvid"] == bvid), None)
        _require(video is not None, f"{report_type} report lost the real video")
        evidence = next((entry for entry in report["evidence"] if entry["bvid"] == bvid), None)
        _require(evidence is not None, f"{report_type} report lost BVID evidence")
        _require(evidence["chunk_id"], f"{report_type} report lost chunk evidence")
        _require(evidence["frame_id"], f"{report_type} report lost frame evidence")
        _require(evidence["timestamp"], f"{report_type} report lost time evidence")
        _require(evidence["quote"], f"{report_type} report lost quote evidence")
        markdown = client.get(f"/api/replay/reports/{report['report_id']}/export.md")
        exported_json = client.get(f"/api/replay/reports/{report['report_id']}/export.json")
        _require(markdown.status_code == 200 and bvid in markdown.text, f"{report_type} Markdown export mismatch")
        _require(exported_json.status_code == 200, f"{report_type} JSON export failed")
        _require(bvid in exported_json.text, f"{report_type} JSON export lost BVID")

    _require(
        reports["weekly"]["sources"]["previous_reports"] == [reports["daily"]["report_id"]],
        "weekly report did not read only the expected daily report",
    )
    _require(
        reports["monthly"]["sources"]["previous_reports"] == [reports["weekly"]["report_id"]],
        "monthly report did not read only the expected weekly report",
    )
    _require(
        reports["yearly"]["sources"]["previous_reports"] == [reports["monthly"]["report_id"]],
        "yearly report did not read only the expected monthly report",
    )

    answer = _json(
        client.post(
            "/api/replay/memory/ask",
            json={
                "question": f"{bvid} 中 AI 完成 APP 开发的流程是什么？",
                "scope": "video",
                "scope_value": bvid,
                "persist": False,
            },
        ),
        "memory question",
    )
    _require(answer["evidence_sufficient"] is True, "memory answer unexpectedly refused")
    _require(answer["scope"] == "video" and answer["scope_value"] == bvid, "memory scope mismatch")
    reference = next((entry for entry in answer["references"] if entry["bvid"] == bvid), None)
    _require(reference is not None, "memory answer lost BVID reference")
    for field in ["video_id", "chunk_id", "frame_id", "timestamp", "quote"]:
        _require(reference[field], f"memory reference lost {field}")

    return {
        "item_id": item["id"],
        "item_flags": item["status"],
        "report_ids": {key: value["report_id"] for key, value in reports.items()},
        "report_chain": [
            reports["daily"]["report_id"],
            reports["weekly"]["report_id"],
            reports["monthly"]["report_id"],
            reports["yearly"]["report_id"],
        ],
        "memory": {
            "evidence_sufficient": answer["evidence_sufficient"],
            "matched_count": answer["matched_count"],
            "reference_count": len(answer["references"]),
            "reference": reference,
        },
    }


def main() -> int:
    args = _arguments()
    root = args.root.resolve()
    app = create_app(build_api_container(root))
    result: dict[str, Any] = {
        "bvid": args.bvid,
        "source_url": args.url,
        "root": str(root),
        "series_id": args.series_id,
    }

    with TestClient(app) as client:
        client.headers.update({"X-Series-Id": args.series_id})
        intake_status = _json(client.get("/api/intake/status"), "intake status")
        result["intake_status"] = {
            "cookie_browser": intake_status["cookie_browser"],
            "cookie_snapshot_ready": intake_status["cookie_snapshot_ready"],
            "cookie_access": intake_status["cookie_access"],
            "cookie_warning": intake_status["cookie_warning"],
            "vision_mode": intake_status["vision_mode"],
            "vision_real_configured": intake_status["vision_real_configured"],
            "record_count": intake_status["record_count"],
        }
        if args.live_resolve:
            resolved = _json(client.post("/api/intake/resolve", json={"url": args.url}), "live source resolve")
            _require(resolved["source_type"] == "single", "live resolve did not return a single video")
            item = next((entry for entry in resolved["items"] if entry["bvid"] == args.bvid), None)
            _require(item is not None, "live resolve did not return the expected BVID")
            result["live_resolve"] = {
                "source_type": resolved["source_type"],
                "title": item["title"],
                "bvid": item["bvid"],
                "duration_seconds": item["duration_seconds"],
                "cookie_access": resolved["cookie_access"],
                "cookie_warning": resolved["cookie_warning"],
            }

        detail = _json(client.get(f"/api/intake/library/{args.bvid}"), "record detail")
        result["artifacts"] = _validate_artifacts(root, detail, args.bvid, args.url)
        result["replay"] = _validate_replay(client, detail, args.bvid)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
