"""DocumentHtmlExportService — 把 Document 渲染为 HTML 并写到磁盘。

写入位置：{runtime_root}/exports/document-html/{document_id}-r{revision}.html
元数据记录：ObjectStore collection "document_html_exports"

设计要点：
- 复用 DocumentHtmlRenderer 生成 HTML 字符串
- 文件名含 revision，避免覆盖旧版本
- 路径校验防止目录穿越
- 不读取文件内容、不调用外部 API
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.product_core.document_html_render import DocumentHtmlRenderError, DocumentHtmlRenderer
from .ports import ObjectStorePort


class DocumentHtmlExportError(ValueError):
    """Raised when HTML export fails."""


@dataclass(frozen=True, slots=True)
class DocumentHtmlExportResult:
    """HTML 导出结果。"""

    status: str
    document_id: str
    revision: int | None
    title: str
    file_name: str
    file_path: str
    output_ref: str

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "document_id": self.document_id,
            "revision": self.revision,
            "title": self.title,
            "file_name": self.file_name,
            "file_path": self.file_path,
            "output_ref": self.output_ref,
        }


@dataclass(frozen=True, slots=True)
class DocumentHtmlExportService:
    """把 Document 渲染为 HTML 文件并写到磁盘。

    用法：
        service = DocumentHtmlExportService(object_store=store, runtime_root=Path(...))
        result = service.export(document, markdown)
    """

    object_store: ObjectStorePort
    runtime_root: Path
    namespace_id: str = "default"
    renderer: DocumentHtmlRenderer = DocumentHtmlRenderer()

    def export(self, document: Mapping[str, object], markdown: str) -> DocumentHtmlExportResult:
        if not isinstance(document, Mapping):
            raise DocumentHtmlExportError("document must be a mapping")
        document_id = str(document.get("id") or "")
        if not document_id:
            raise DocumentHtmlExportError("document.id is required")
        if _is_unsafe_path_segment(document_id):
            raise DocumentHtmlExportError("document_id is invalid")
        try:
            render_result = self.renderer.render(document, markdown)
        except DocumentHtmlRenderError as error:
            raise DocumentHtmlExportError(str(error)) from error

        file_name = _file_name(document_id, render_result.revision)
        output_dir = _export_dir(self.runtime_root)
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / file_name
        # 写文件（覆盖同 revision 旧文件，幂等）
        file_path.write_text(render_result.html, encoding="utf-8")

        output_ref = f"crp://{self.namespace_id}/document-html/{file_name}"
        # ObjectStore record id 必须是安全的 repository segment，
        # 对 document_id 做同样的 sanitize（替换 / 和 \ 为 -）
        safe_id_segment = _sanitize_segment(document_id)
        record_id = f"doc-html-{safe_id_segment}-r{render_result.revision or 0}"
        export_record = {
            "schema_version": "1.0.0",
            "id": record_id,
            "document_id": document_id,
            "revision": render_result.revision,
            "title": render_result.title,
            "file_name": file_name,
            "file_path": str(file_path),
            "output_ref": output_ref,
        }
        self.object_store.write(
            "document_html_exports",
            record_id,
            export_record,
            expected_revision=None,
        )
        return DocumentHtmlExportResult(
            status="exported",
            document_id=document_id,
            revision=render_result.revision,
            title=render_result.title,
            file_name=file_name,
            file_path=str(file_path),
            output_ref=output_ref,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _export_dir(runtime_root: Path) -> Path:
    return (runtime_root / "exports" / "document-html").resolve()


def _sanitize_segment(document_id: str) -> str:
    """把 document_id 中的路径分隔符替换为连字符，避免目录穿越。"""
    return document_id.replace("/", "-").replace("\\", "-")


def _file_name(document_id: str, revision: int | None) -> str:
    safe_id = _sanitize_segment(document_id)
    rev = revision if isinstance(revision, int) else 0
    return f"{safe_id}-r{rev}.html"


def _is_unsafe_path_segment(segment: str) -> bool:
    if not segment:
        return True
    if segment in {".", ".."}:
        return True
    if any(char in segment for char in ("\x00",)):
        return True
    return False
