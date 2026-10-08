from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Callable

from backend.video_intake.models import ResolvedSource, ResolvedVideoItem


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
)


class BilibiliAccessError(RuntimeError):
    pass


class EdgeCookieLockedError(BilibiliAccessError):
    pass


class BilibiliClient:
    def __init__(self, root_dir: Path, *, browser: str = "edge") -> None:
        self._root_dir = root_dir
        del browser
        self.cookie_access = "anonymous"
        self.cookie_warning = "旧视频 intake 仅允许匿名公开内容。"

    @property
    def has_cookie_snapshot(self) -> bool:
        """The preserved anonymous client never loads a browser cookie snapshot."""
        return False

    async def resolve(self, url: str) -> ResolvedSource:
        return await asyncio.to_thread(self._resolve_sync, url)

    def _resolve_sync(self, url: str) -> ResolvedSource:
        from yt_dlp import YoutubeDL

        options = self._base_options()
        options.update({"extract_flat": "in_playlist", "skip_download": True})
        try:
            with YoutubeDL(options) as ydl:
                payload = ydl.extract_info(url, download=False)
        except Exception as error:
            raise _normalize_access_error(error, cookie_warning=self.cookie_warning) from error
        if not isinstance(payload, dict):
            raise BilibiliAccessError("链接解析没有返回有效信息。")

        raw_entries = payload.get("entries")
        entries = [entry for entry in raw_entries or [] if isinstance(entry, dict)] if raw_entries else [payload]
        items = [_to_item(entry, fallback_url=url, fallback_uploader=_text(payload.get("uploader"))) for entry in entries]
        items = [item for item in items if item.bvid]
        if not items:
            raise BilibiliAccessError("没有从链接中识别到可处理的视频。")

        source_type = _source_type(url, payload, items)
        return ResolvedSource(
            source_type=source_type,
            title=_text(payload.get("title")) or items[0].title,
            source_url=_text(payload.get("webpage_url")) or url,
            cover_url=_text(payload.get("thumbnail")) or items[0].cover_url,
            items=items,
            cookie_browser="",
            cookie_access=self.cookie_access,
            cookie_warning=self.cookie_warning,
            requires_selection=len(items) > 1,
        )

    async def download(
        self,
        item: ResolvedVideoItem,
        record_dir: Path,
        *,
        media_mode: str,
        on_progress: Callable[[float, str], None] | None = None,
    ) -> Path:
        return await asyncio.to_thread(
            self._download_sync,
            item,
            record_dir,
            media_mode,
            on_progress,
        )

    def _download_sync(
        self,
        item: ResolvedVideoItem,
        record_dir: Path,
        media_mode: str,
        on_progress: Callable[[float, str], None] | None,
    ) -> Path:
        from yt_dlp import YoutubeDL

        media_dir = record_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        options = self._base_options()
        options.update(
            {
                "noplaylist": True,
                "outtmpl": str(media_dir / "source.%(ext)s"),
                "writethumbnail": True,
                "writeinfojson": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["zh-CN", "zh-Hans", "zh-Hant", "zh", "en"],
                "subtitlesformat": "srt/best",
                "convertsubtitles": "srt",
                "progress_hooks": [lambda data: _progress_hook(data, on_progress)],
                "overwrites": True,
                "nopart": True,
            }
        )
        if media_mode == "video":
            options.update(
                {
                    "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
                    "merge_output_format": "mp4",
                }
            )
        else:
            options.update(
                {
                    "format": "ba/b",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "m4a",
                            "preferredquality": "0",
                        }
                    ],
                }
            )

        try:
            with YoutubeDL(options) as ydl:
                ydl.download([item.source_url])
        except Exception as error:
            raise _normalize_access_error(error, cookie_warning=self.cookie_warning) from error

        candidates = [
            path
            for path in media_dir.glob("source.*")
            if path.suffix.lower() in {".m4a", ".mp3", ".opus", ".webm", ".mp4", ".mkv", ".mov"}
        ]
        if not candidates:
            raise BilibiliAccessError("下载完成后没有找到音视频文件。")
        return max(candidates, key=lambda path: path.stat().st_size)

    async def download_visual_probe(self, item: ResolvedVideoItem, record_dir: Path) -> Path | None:
        return await asyncio.to_thread(self._download_visual_probe_sync, item, record_dir)

    def _download_visual_probe_sync(self, item: ResolvedVideoItem, record_dir: Path) -> Path | None:
        from yt_dlp import YoutubeDL

        probe_dir = record_dir / "data" / "visual-probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        options = self._base_options()
        options.update(
            {
                "noplaylist": True,
                "format": "wv*[height<=360]/w[height<=360]/worst",
                "outtmpl": str(probe_dir / "probe.%(ext)s"),
                "overwrites": True,
                "nopart": True,
                "quiet": True,
            }
        )
        try:
            with YoutubeDL(options) as ydl:
                ydl.download([item.source_url])
        except Exception:
            return None
        return next((path for path in probe_dir.glob("probe.*") if path.is_file()), None)

    def _base_options(self) -> dict[str, object]:
        options: dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }
        ffmpeg_dir = self._root_dir / "runtime" / "Library" / "bin"
        if ffmpeg_dir.exists():
            options["ffmpeg_location"] = str(ffmpeg_dir)
        return options

def _to_item(payload: dict[str, object], *, fallback_url: str, fallback_uploader: str) -> ResolvedVideoItem:
    source_url = _text(payload.get("webpage_url")) or _text(payload.get("url")) or fallback_url
    bvid = _extract_bvid(payload, source_url)
    page = _page(payload, source_url)
    if bvid and not source_url.startswith("http"):
        source_url = f"https://www.bilibili.com/video/{bvid}/"
    if bvid and page > 1 and "?p=" not in source_url:
        source_url = f"https://www.bilibili.com/video/{bvid}/?p={page}"
    subtitles = payload.get("subtitles")
    subtitle_languages = list(subtitles.keys()) if isinstance(subtitles, dict) else []
    tags = payload.get("tags")
    upload_date = _text(payload.get("upload_date"))
    published_at = (
        f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        if len(upload_date) == 8 and upload_date.isdigit()
        else upload_date
    )
    return ResolvedVideoItem(
        key=f"{bvid}:p{page}",
        bvid=bvid,
        page=page,
        title=_text(payload.get("title")) or bvid or "未命名视频",
        duration_seconds=_number(payload.get("duration")),
        cover_url=_text(payload.get("thumbnail")),
        source_url=source_url,
        uploader=_text(payload.get("uploader")) or fallback_uploader,
        published_at=published_at,
        description=_text(payload.get("description")),
        tags=[str(tag).strip() for tag in tags or [] if str(tag).strip()] if isinstance(tags, list) else [],
        subtitle_languages=subtitle_languages,
    )


def _source_type(url: str, payload: dict[str, object], items: list[ResolvedVideoItem]) -> str:
    lowered = url.lower()
    if "favlist" in lowered or "/fav" in lowered:
        return "favorite"
    if "collectiondetail" in lowered or "medialist" in lowered or "seriesdetail" in lowered:
        return "collection"
    if len(items) > 1:
        if len({item.bvid for item in items}) == 1:
            return "multi_page"
        return "playlist"
    return "single"


def _extract_bvid(payload: dict[str, object], fallback: str) -> str:
    for value in (payload.get("id"), payload.get("display_id"), payload.get("url"), fallback):
        match = re.search(r"(BV[a-zA-Z0-9]{10})", str(value or ""))
        if match:
            return match.group(1)
    return ""


def _page(payload: dict[str, object], url: str) -> int:
    for key in ("page_number", "playlist_index"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
    match = re.search(r"[?&]p=(\d+)", url)
    return int(match.group(1)) if match else 1


def _progress_hook(data: dict[str, object], callback: Callable[[float, str], None] | None) -> None:
    if callback is None:
        return
    status = data.get("status")
    if status == "finished":
        callback(100.0, "下载完成")
        return
    downloaded = _number(data.get("downloaded_bytes"))
    total = _number(data.get("total_bytes")) or _number(data.get("total_bytes_estimate"))
    ratio = downloaded / total if total > 0 else 0.0
    callback(max(0.0, min(99.0, ratio * 100.0)), "正在下载音视频与字幕")


def _normalize_access_error(error: Exception, *, cookie_warning: str = "") -> BilibiliAccessError:
    message = str(error)
    lowered = message.lower()
    if "http error 412" in lowered or "precondition failed" in lowered:
        return BilibiliAccessError(
            "哔哩哔哩拒绝了当前匿名公开请求。"
        )
    if "sign in" in lowered or "login" in lowered or "cookie" in lowered:
        return BilibiliAccessError("该内容需要登录凭据，旧视频 intake 不允许读取浏览器凭据。")
    suffix = f"；Cookie 状态：{cookie_warning}" if cookie_warning else ""
    return BilibiliAccessError(f"哔哩哔哩处理失败：{message}{suffix}")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0
