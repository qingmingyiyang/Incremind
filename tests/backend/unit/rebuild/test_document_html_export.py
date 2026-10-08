from __future__ import annotations

from pathlib import Path

import pytest

from core.product_core.document_html_export import (
    DocumentHtmlExportError,
    DocumentHtmlExportResult,
    DocumentHtmlExportService,
)
from core.storage_provider import JsonObjectStore


# ── 测试夹具 ──


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _document(
    *,
    document_id: str = "doc-001",
    title: str = "测试文档",
    revision: int | None = 3,
    markdown: str = "# 标题\n\n正文内容",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": document_id,
        "type": "answer_manual",
        "title": title,
        "markdown": markdown,
        "source_refs": [],
        "status": "draft",
    }
    if revision is not None:
        payload["revision"] = revision
    return payload


# ── 基础校验 ──


def test_export_rejects_non_mapping_document(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    with pytest.raises(DocumentHtmlExportError, match="document must be a mapping"):
        service.export("not-a-mapping", "# title")  # type: ignore[arg-type]


def test_export_rejects_missing_document_id(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    with pytest.raises(DocumentHtmlExportError, match="document.id is required"):
        service.export({"title": "no id"}, "# title")


def test_export_rejects_unsafe_document_id_dotdot(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    with pytest.raises(DocumentHtmlExportError, match="document_id is invalid"):
        service.export({"id": ".."}, "# title")


def test_export_rejects_unsafe_document_id_dot(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    with pytest.raises(DocumentHtmlExportError, match="document_id is invalid"):
        service.export({"id": "."}, "# title")


def test_export_rejects_unsafe_document_id_with_null_byte(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    with pytest.raises(DocumentHtmlExportError, match="document_id is invalid"):
        service.export({"id": "doc\x00evil"}, "# title")


def test_export_rejects_empty_document_id(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    # 空 id 先触发 "document.id is required"（在 path segment 校验之前）
    with pytest.raises(DocumentHtmlExportError, match="document.id is required"):
        service.export({"id": ""}, "# title")


def test_export_rejects_non_string_markdown(tmp_path: Path) -> None:
    service = DocumentHtmlExportService(object_store=_store(tmp_path), runtime_root=tmp_path)
    with pytest.raises(DocumentHtmlExportError, match="markdown must be a string"):
        service.export({"id": "doc-1"}, 123)  # type: ignore[arg-type]


# ── 正常导出 ──


def test_export_writes_html_file_to_correct_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(), "# 标题\n\n正文")
    expected_path = tmp_path / "exports" / "document-html" / "doc-001-r3.html"
    assert result.file_path == str(expected_path)
    assert expected_path.exists()
    assert expected_path.is_file()


def test_export_file_name_includes_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(document_id="doc-xyz", revision=7), "正文")
    assert result.file_name == "doc-xyz-r7.html"


def test_export_uses_revision_0_when_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(revision=None), "正文")
    assert result.file_name == "doc-001-r0.html"
    assert "doc-001-r0.html" in result.file_path


def test_export_file_content_matches_renderer_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    doc = _document(markdown="# 标题\n\n正文")
    result = service.export(doc, "# 标题\n\n正文")
    file_content = Path(result.file_path).read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in file_content
    assert "cr-doc" in file_content
    assert "标题" in file_content
    assert "正文" in file_content


def test_export_returns_result_with_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(), "正文")
    assert isinstance(result, DocumentHtmlExportResult)
    assert result.status == "exported"
    assert result.document_id == "doc-001"
    assert result.revision == 3
    assert result.title == "测试文档"
    assert result.file_name == "doc-001-r3.html"
    assert result.file_path.endswith("doc-001-r3.html")
    assert result.output_ref.startswith("crp://default/document-html/")


def test_export_uses_custom_namespace_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(
        object_store=store,
        runtime_root=tmp_path,
        namespace_id="custom-ns",
    )
    result = service.export(_document(), "正文")
    assert result.output_ref.startswith("crp://custom-ns/document-html/")


def test_export_output_ref_includes_file_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(revision=5), "正文")
    assert result.output_ref == f"crp://default/document-html/{result.file_name}"


# ── ObjectStore 元数据记录 ──


def test_export_records_metadata_in_object_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(), "正文")
    record_id = f"doc-html-{result.document_id}-r{result.revision}"
    saved = store.read("document_html_exports", record_id)
    assert saved is not None
    assert saved["document_id"] == "doc-001"
    assert saved["revision"] == 3
    assert saved["title"] == "测试文档"
    assert saved["file_name"] == result.file_name
    assert saved["file_path"] == result.file_path
    assert saved["output_ref"] == result.output_ref
    assert saved["schema_version"] == "1.0.0"
    assert saved["id"] == record_id


def test_export_overwrites_same_revision_idempotently(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    doc = _document(markdown="第一次内容")
    result1 = service.export(doc, "第一次内容")
    # 第二次导出同 revision，应覆盖文件并刷新 ObjectStore 记录
    result2 = service.export(doc, "第二次内容")
    assert result1.file_path == result2.file_path
    file_content = Path(result2.file_path).read_text(encoding="utf-8")
    assert "第二次内容" in file_content
    # ObjectStore 仍只有一条记录
    record_id = f"doc-html-{result2.document_id}-r{result2.revision}"
    saved = store.read("document_html_exports", record_id)
    assert saved is not None


# ── 路径处理 ──


def test_export_creates_export_directory_if_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    export_dir = tmp_path / "exports" / "document-html"
    assert not export_dir.exists()
    service.export(_document(), "正文")
    assert export_dir.exists()
    assert export_dir.is_dir()


def test_export_sanitizes_slashes_in_document_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    # 斜杠在 document_id 中会被替换为连字符，避免目录穿越
    result = service.export({"id": "doc/with/slash", "revision": 1}, "正文")
    assert result.file_name == "doc-with-slash-r1.html"
    # 文件确实写到 exports/document-html/ 下，没有创建子目录
    assert (tmp_path / "exports" / "document-html" / "doc-with-slash-r1.html").is_file()


def test_export_sanitizes_backslashes_in_document_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export({"id": "doc\\with\\back", "revision": 1}, "正文")
    assert result.file_name == "doc-with-back-r1.html"


# ── to_payload ──


def test_result_to_payload_serializes_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(), "正文")
    payload = result.to_payload()
    assert payload["status"] == "exported"
    assert payload["document_id"] == "doc-001"
    assert payload["revision"] == 3
    assert payload["title"] == "测试文档"
    assert payload["file_name"] == "doc-001-r3.html"
    assert isinstance(payload["file_path"], str)
    assert payload["output_ref"].startswith("crp://default/document-html/")


# ── 隐私：导出文件不泄露敏感字段 ──


def test_export_file_does_not_leak_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DocumentHtmlExportService(object_store=store, runtime_root=tmp_path)
    result = service.export(_document(), "正文")
    file_content = Path(result.file_path).read_text(encoding="utf-8").lower()
    assert "sk-" not in file_content
    assert "cookie" not in file_content
    assert "authorization" not in file_content
    assert "password" not in file_content
    assert "token" not in file_content
