from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from backend.bilibili.media_selection import (
    BilibiliMediaSelectionError,
    require_api_data,
    require_bilibili_cdn_url,
)


BILIBILI_API_ORIGIN = "https://api.bilibili.com"
BILIBILI_REFERER_ORIGIN = "https://www.bilibili.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
MAX_PUBLIC_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024
MAX_REDIRECTS = 3
_BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")


class AuthorizedPublicDownloadError(RuntimeError):
    """Raised when the bounded anonymous Bilibili fallback cannot proceed safely."""


def download_authorized_bilibili_video(
    *,
    url: str,
    output_template: str,
    format_selector: str,
    user_agent: str = DEFAULT_USER_AGENT,
    cookie_mode: str = "none",
    cookies_from_browser: str = "",
    cookies_file: str = "",
    yt_dlp_runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    http_client: httpx.Client | None = None,
    max_download_bytes: int = MAX_PUBLIC_DOWNLOAD_BYTES,
    media_validator: Callable[[Path], bool] | None = None,
) -> Path:
    clean_url, bvid, page = _authorized_video_identity(url)
    clean_cookie_mode = _cookie_mode(cookie_mode)
    final_path = _output_path(output_template)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    yt_dlp_command = _yt_dlp_command(
        url=clean_url,
        output_template=output_template,
        format_selector=format_selector,
        user_agent=user_agent,
        cookie_mode=clean_cookie_mode,
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
    )
    completed = (yt_dlp_runner or _run_yt_dlp)(yt_dlp_command)
    validate_media = media_validator or _media_has_audio
    primary_without_audio = False
    if completed.returncode == 0:
        output = _find_output(final_path.parent, final_path.stem)
        if output is None:
            raise AuthorizedPublicDownloadError("yt-dlp completed but no output file was found")
        if validate_media(output):
            return output
        if clean_cookie_mode != "none":
            raise AuthorizedPublicDownloadError("yt-dlp completed without an audio stream")
        output.unlink()
        primary_without_audio = True

    failure = (completed.stderr or completed.stdout or f"yt-dlp exited {completed.returncode}").strip()
    if clean_cookie_mode != "none" or not (primary_without_audio or _is_http_412_failure(failure)):
        raise AuthorizedPublicDownloadError(failure)
    if final_path.exists():
        raise AuthorizedPublicDownloadError("yt-dlp failure left an unexpected completed output")

    owns_client = http_client is None
    client = http_client or httpx.Client(follow_redirects=False, timeout=httpx.Timeout(30.0, read=120.0))
    try:
        cid = _resolve_cid(client, bvid=bvid, page=page, user_agent=user_agent)
        media_url, expected_bytes = _resolve_public_media(
            client,
            bvid=bvid,
            cid=cid,
            user_agent=user_agent,
            max_download_bytes=max_download_bytes,
        )
        _stream_atomic_media(
            client,
            media_url=media_url,
            output_path=final_path,
            expected_bytes=expected_bytes,
            user_agent=user_agent,
            bvid=bvid,
            max_download_bytes=max_download_bytes,
        )
        if not validate_media(final_path):
            final_path.unlink(missing_ok=True)
            raise AuthorizedPublicDownloadError("Bilibili public media has no audio stream")
        return final_path
    finally:
        if owns_client:
            client.close()


def _resolve_cid(client: httpx.Client, *, bvid: str, page: int, user_agent: str) -> int:
    response = client.get(
        f"{BILIBILI_API_ORIGIN}/x/web-interface/view",
        params={"bvid": bvid},
        headers=_api_headers(user_agent, bvid),
    )
    response.raise_for_status()
    data = _api_data(response, "view")
    if data.get("bvid") != bvid:
        raise AuthorizedPublicDownloadError("Bilibili view response identity drifted")
    pages = data.get("pages")
    if not isinstance(pages, list) or page < 1 or page > len(pages):
        raise AuthorizedPublicDownloadError("Bilibili view response does not contain the authorized page")
    selected = pages[page - 1]
    if not isinstance(selected, dict) or selected.get("page") != page:
        raise AuthorizedPublicDownloadError("Bilibili view page identity drifted")
    cid = selected.get("cid")
    if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0:
        raise AuthorizedPublicDownloadError("Bilibili view response has invalid cid")
    return cid


def _resolve_public_media(
    client: httpx.Client,
    *,
    bvid: str,
    cid: int,
    user_agent: str,
    max_download_bytes: int,
) -> tuple[str, int]:
    response = client.get(
        f"{BILIBILI_API_ORIGIN}/x/player/playurl",
        params={"bvid": bvid, "cid": cid, "qn": 64, "fnval": 0, "fnver": 0, "fourk": 0},
        headers=_api_headers(user_agent, bvid),
    )
    response.raise_for_status()
    data = _api_data(response, "playurl")
    durl = data.get("durl")
    if not isinstance(durl, list) or len(durl) != 1 or not isinstance(durl[0], dict):
        raise AuthorizedPublicDownloadError("Bilibili public fallback requires one bounded media segment")
    media = durl[0]
    media_url = media.get("url")
    expected_bytes = media.get("size")
    if not isinstance(media_url, str) or not media_url:
        raise AuthorizedPublicDownloadError("Bilibili playurl response has no media URL")
    _require_media_url(media_url)
    if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes <= 0:
        raise AuthorizedPublicDownloadError("Bilibili playurl response has invalid media size")
    if expected_bytes > max_download_bytes:
        raise AuthorizedPublicDownloadError("Bilibili public media exceeds the download byte limit")
    return media_url, expected_bytes


def _stream_atomic_media(
    client: httpx.Client,
    *,
    media_url: str,
    output_path: Path,
    expected_bytes: int,
    user_agent: str,
    bvid: str,
    max_download_bytes: int,
) -> None:
    part_path = output_path.with_name(f".{output_path.name}.download.part")
    if part_path.exists():
        part_path.unlink()
    current_url = media_url
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _require_media_url(current_url)
            with client.stream("GET", current_url, headers=_media_headers(user_agent, bvid)) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REDIRECTS:
                        raise AuthorizedPublicDownloadError("Bilibili media redirect limit exceeded")
                    location = response.headers.get("location")
                    if not location:
                        raise AuthorizedPublicDownloadError("Bilibili media redirect has no location")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_length = _required_content_length(response)
                if content_length != expected_bytes:
                    raise AuthorizedPublicDownloadError("Bilibili media Content-Length drifted")
                if content_length > max_download_bytes:
                    raise AuthorizedPublicDownloadError("Bilibili media exceeds the download byte limit")
                written = 0
                with part_path.open("xb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > expected_bytes or written > max_download_bytes:
                            raise AuthorizedPublicDownloadError("Bilibili media stream exceeded the byte limit")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if written != expected_bytes:
                    raise AuthorizedPublicDownloadError("Bilibili media stream ended before the declared size")
                os.replace(part_path, output_path)
                return
        raise AuthorizedPublicDownloadError("Bilibili media redirect did not resolve")
    except Exception:
        part_path.unlink(missing_ok=True)
        raise


def _api_data(response: httpx.Response, operation: str) -> dict[str, object]:
    try:
        return dict(require_api_data(response.json(), operation=operation))
    except BilibiliMediaSelectionError as error:
        raise AuthorizedPublicDownloadError(f"Bilibili {operation} API returned invalid data") from error


def _required_content_length(response: httpx.Response) -> int:
    value = response.headers.get("content-length")
    if not isinstance(value, str) or not value.isdigit():
        raise AuthorizedPublicDownloadError("Bilibili media response requires Content-Length")
    parsed = int(value)
    if parsed <= 0:
        raise AuthorizedPublicDownloadError("Bilibili media response has invalid Content-Length")
    return parsed


def _authorized_video_identity(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        raise AuthorizedPublicDownloadError("authorized Bilibili download requires an HTTPS bilibili.com URL")
    match = _BVID_RE.search(parsed.path)
    if match is None:
        raise AuthorizedPublicDownloadError("authorized Bilibili download requires a BV id")
    values = parse_qs(parsed.query).get("p", ["1"])
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
        raise AuthorizedPublicDownloadError("authorized Bilibili download requires a positive page")
    return url.strip(), match.group(1), int(values[0])


def _require_media_url(url: str) -> None:
    try:
        require_bilibili_cdn_url(url)
    except BilibiliMediaSelectionError as error:
        raise AuthorizedPublicDownloadError(
            "Bilibili media URL escaped the allowlisted CDN"
        ) from error


def _output_path(output_template: str) -> Path:
    if output_template.count("%(ext)s") != 1:
        raise AuthorizedPublicDownloadError("authorized output template requires one extension placeholder")
    path = Path(output_template.replace("%(ext)s", "mp4")).expanduser().resolve(strict=False)
    if not path.name or path.name in {".", ".."}:
        raise AuthorizedPublicDownloadError("authorized output path is invalid")
    return path


def _find_output(output_dir: Path, stem: str) -> Path | None:
    candidates = sorted(path for path in output_dir.glob(f"{stem}.*") if path.is_file() and not path.name.endswith(".part"))
    return candidates[0] if candidates else None


def _is_http_412_failure(value: str) -> bool:
    lowered = value.lower()
    return "http error 412" in lowered or "precondition failed" in lowered


def _cookie_mode(value: str) -> str:
    clean = value.strip().lower()
    if clean not in {"none", "browser", "file"}:
        raise AuthorizedPublicDownloadError("cookie mode must be none, browser or file")
    if clean != "none":
        raise AuthorizedPublicDownloadError(
            "credentialed Bilibili subprocess mode is prohibited"
        )
    return clean


def _api_headers(user_agent: str, bvid: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Referer": f"{BILIBILI_REFERER_ORIGIN}/video/{bvid}/",
        "Origin": BILIBILI_REFERER_ORIGIN,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _media_headers(user_agent: str, bvid: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Referer": f"{BILIBILI_REFERER_ORIGIN}/video/{bvid}/"}


def _yt_dlp_command(
    *,
    url: str,
    output_template: str,
    format_selector: str,
    user_agent: str,
    cookie_mode: str,
    cookies_from_browser: str,
    cookies_file: str,
) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--user-agent",
        user_agent,
        "--referer",
        f"{BILIBILI_REFERER_ORIGIN}/",
        "--add-header",
        f"Origin:{BILIBILI_REFERER_ORIGIN}",
        "--add-header",
        "Accept-Language:zh-CN,zh;q=0.9,en;q=0.8",
        "--no-playlist",
        "--format",
        format_selector,
        "--merge-output-format",
        "mp4",
        "--ffmpeg-location",
        str(_bundled_ffmpeg_dir()),
        "--output",
        output_template,
        "--newline",
        url,
    ]
    del cookies_from_browser, cookies_file
    if cookie_mode != "none":
        raise AuthorizedPublicDownloadError(
            "credentialed Bilibili subprocess mode is prohibited"
        )
    return tuple(command)


def _bundled_ffmpeg_dir() -> Path:
    runtime_root = Path(sys.executable).resolve().parent
    candidates = (
        runtime_root / "Library" / "bin",
        runtime_root,
    )
    executable = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for candidate in candidates:
        if (candidate / executable).is_file():
            return candidate
    raise AuthorizedPublicDownloadError("bundled FFmpeg is unavailable")


def _media_has_audio(path: Path) -> bool:
    ffprobe_name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    ffprobe = _bundled_ffmpeg_dir() / ffprobe_name
    if not ffprobe.is_file():
        raise AuthorizedPublicDownloadError("bundled FFprobe is unavailable")
    completed = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        return False
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False
    streams = payload.get("streams") if isinstance(payload, dict) else None
    return isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )


def _run_yt_dlp(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authorized Bilibili downloader with a bounded public fallback")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--format", default="bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--cookie-mode", choices=("none", "browser", "file"), default="none")
    parser.add_argument("--cookies-from-browser", default="")
    parser.add_argument("--cookies-file", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_cli_parser().parse_args(argv)
    try:
        output = download_authorized_bilibili_video(
            url=args.url,
            output_template=args.output,
            format_selector=args.format,
            user_agent=args.user_agent,
            cookie_mode=args.cookie_mode,
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies_file,
        )
    except Exception as error:  # noqa: BLE001 - CLI must return one bounded diagnostic.
        print(str(error), file=sys.stderr)
        return 1
    print(output.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
