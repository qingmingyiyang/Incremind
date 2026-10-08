from __future__ import annotations

from pathlib import Path

from backend.video_intake.models import ResolvedVideoItem
from backend.video_intake.storage import LibraryStorage
from backend.video_intake.transcript import parse_subtitle, render_transcript_markdown, transcript_payload


def test_library_storage_creates_human_readable_date_folder(tmp_path: Path) -> None:
    storage = LibraryStorage(tmp_path)
    item = ResolvedVideoItem(
        key="BV1xx411c7mD:p1",
        bvid="BV1xx411c7mD",
        title="测试：视频/标题",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
        uploader="作者",
    )

    record, record_dir = storage.create_record(item, media_mode="audio")

    assert record.id == "BV1xx411c7mD"
    assert record_dir.parent.parent.parent.name.isdigit()
    assert (record_dir / "README.md").exists()
    assert (record_dir / "个人笔记.md").exists()
    assert storage.find_record(record.id) is not None


def test_subtitle_parser_keeps_timestamps_and_writes_readable_markdown(tmp_path: Path) -> None:
    subtitle = tmp_path / "source.zh-Hans.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,500\n第一句话\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n第二句话\n",
        encoding="utf-8",
    )

    transcript = parse_subtitle(subtitle)
    payload = transcript_payload("标题", 6.0, transcript, source="official_subtitle")
    markdown = render_transcript_markdown(payload)

    assert transcript.language == "zh"
    assert transcript.segments[0].start_seconds == 1.0
    assert "**[00:01–00:03]** 第一句话" in markdown
    assert "转写来源：official_subtitle" in markdown


def test_library_storage_reports_artifact_status_and_deletes_confirmed_record(tmp_path: Path) -> None:
    storage = LibraryStorage(tmp_path)
    item = ResolvedVideoItem(
        key="BV1xx411c7mD:p1",
        bvid="BV1xx411c7mD",
        title="待管理资料",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
    )
    record, record_dir = storage.create_record(item, media_mode="audio")
    (record_dir / "data" / "transcript.cleaned.json").write_text("{}", encoding="utf-8")
    (record_dir / "data" / "summary.json").write_text("{}", encoding="utf-8")
    (record_dir / record.summary_file).write_text("# summary", encoding="utf-8")

    loaded = storage.find_record(record.id)

    assert loaded is not None
    assert loaded.cleaned_transcript_available is True
    assert loaded.summary_available is True
    assert loaded.export_available is True
    assert storage.delete_records([record.id]) == [record.id]
    assert not record_dir.exists()


def test_same_video_can_exist_in_two_isolated_series(tmp_path: Path) -> None:
    item = ResolvedVideoItem(
        key="BV1xx411c7mD:p1",
        bvid="BV1xx411c7mD",
        title="同一视频",
        source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
    )
    default_storage = LibraryStorage(tmp_path, "default")
    research_storage = LibraryStorage(tmp_path, "research")

    default_record, default_dir = default_storage.create_record(item, media_mode="audio")
    research_record, research_dir = research_storage.create_record(item, media_mode="video")

    assert default_record.id == research_record.id
    assert default_record.series_id == "default"
    assert research_record.series_id == "research"
    assert default_dir != research_dir
    assert [record.series_id for record in default_storage.list_records()] == ["default"]
    assert [record.series_id for record in research_storage.list_records()] == ["research"]
