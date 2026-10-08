from __future__ import annotations

from backend.video_intake.bilibili import (
    EdgeCookieLockedError,
    _extract_bvid,
    _is_cookie_decryption_error,
    _normalize_access_error,
    _page,
    _source_type,
)
from backend.video_intake.models import ResolvedVideoItem
from backend.video_intake.storage import _safe_name


def test_extract_bvid_reads_multi_page_url() -> None:
    assert _extract_bvid({}, "https://www.bilibili.com/video/BV1xx411c7mD/?p=3") == "BV1xx411c7mD"


def test_page_reads_multi_page_url() -> None:
    assert _page({}, "https://www.bilibili.com/video/BV1xx411c7mD/?p=3") == 3


def test_safe_name_removes_windows_forbidden_chars() -> None:
    assert _safe_name('标题:第/一<P>|"测试"*?', 100) == "标题_第_一_P_测试"


def test_cookie_database_lock_has_actionable_message() -> None:
    error = _normalize_access_error(RuntimeError("Could not copy Chrome cookie database"))

    assert isinstance(error, EdgeCookieLockedError)
    assert "完整关闭 Edge" in str(error)


def test_edge_app_bound_cookie_error_is_recognized() -> None:
    assert _is_cookie_decryption_error(RuntimeError("Failed to decrypt with DPAPI")) is True


def test_multiple_pages_are_classified_as_multi_page() -> None:
    items = [
        ResolvedVideoItem(
            key=f"BV1xx411c7mD:p{page}",
            bvid="BV1xx411c7mD",
            page=page,
            title=f"P{page}",
            source_url=f"https://www.bilibili.com/video/BV1xx411c7mD/?p={page}",
        )
        for page in (1, 2)
    ]

    assert _source_type(items[0].source_url, {}, items) == "multi_page"
