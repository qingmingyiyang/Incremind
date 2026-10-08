"""阶段 1.6：External LLM Import Framework — 外部 LLM 平台导入框架。

基于 adapter-based architecture，不把某个平台格式硬编码进主流程。

抽象来源：
1. Conversation Export — 外部平台聊天记录导出
2. Project / Workspace Export — 外部平台项目空间导出
3. Memory / Custom Instruction Export — 外部平台已有记忆 / 自定义指令
4. Knowledge Document Export — Markdown / JSON / JSONL / TXT / PDF / DOCX / CSV / HTML / ZIP

Adapter interface:
- detect(source_bundle) -> bool
- parse(source_bundle) -> ParsedBundle
- normalize(parsed_data) -> NormalizedBundle
- extract_sources(normalized_data) -> list[SourceRecord]
- to_memory_candidates(normalized_data) -> list[MemoryCandidate]
- validate_import_result(result) -> ValidationResult

信任等级规则（与 memory_quality_gate.compute_trust_level 对齐）：
- user + file/document/workspace/custom_instruction → high
- user + conversation → medium
- assistant → low（默认不直接进入长期事实）
- system → medium
- tool → medium（需保留原始引用）
- 冲突内容 → 必须进入待确认
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


class ExternalImportError(ValueError):
    """Raised when external import fails."""


# ── 数据结构 ──


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """外部对话记录中的单条消息。"""

    role: str  # user | assistant | system | tool | attachment | unknown
    content: str
    # ``timestamp`` is a legacy input alias.  Adapters must set ``occurred_at``
    # from it when an external export actually supplies a time.
    timestamp: str = ""
    message_id: str = ""
    thread_id: str = ""
    project_id: str = ""
    attachments: tuple[str, ...] = ()
    occurred_at: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """从外部导入包抽取的原始资料记录（L0 候选）。"""

    source_id: str
    source_type: str  # conversation | workspace | custom_instruction | document | tool_result | file
    source_role: str
    title: str
    content: str
    source_ref: str
    evidence_refs: tuple[str, ...] = ()
    # Legacy read aliases.  New persistence derives these from dual time.
    created_at: str = ""
    observed_at: str = ""
    import_batch_id: str = ""
    raw_format: str = ""  # markdown | json | jsonl | txt | zip | html | csv | unknown
    # Canonical dual-time fields.  ``occurred_at`` is only an external fact;
    # an absent/unknown source timestamp remains None.  ``recorded_at`` is set
    # by the persistence boundary, never by an adapter.
    occurred_at: str | None = None
    recorded_at: str = ""


@dataclass(frozen=True, slots=True)
class ImportedMemoryCandidate:
    """外部导入产生的 memory 候选（L1/L2/L3/L4）。"""

    memory_id: str
    layer: str  # L0 | L1 | L2 | L3 | L4
    type: str  # preference / fact / rule / person / project / decision / workflow / persona / capability / tag / other
    content: str
    summary: str
    confidence: float
    trust_level: str  # high | medium | low | unverified
    source_platform: str
    source_type: str
    source_role: str
    source_ref: str
    evidence_refs: tuple[str, ...]
    # Legacy read aliases.  New persistence derives these from dual time.
    created_at: str = ""
    observed_at: str = ""
    conflict_refs: tuple[str, ...] = ()
    status: str = "candidate"  # candidate | confirmed | rejected | needs_review | archived
    import_batch_id: str = ""
    privacy_level: str = "private"
    provider_boundary: str = "local_only"
    occurred_at: str | None = None
    recorded_at: str = ""


@dataclass(frozen=True, slots=True)
class ParsedBundle:
    """Adapter 解析阶段的中间结果。"""

    format: str  # markdown | json | jsonl | txt | zip | html | csv | unknown
    platform: str  # generic | chatgpt | claude | cursor | unknown
    raw_messages: tuple[ConversationMessage, ...] = ()
    raw_documents: tuple[SourceRecord, ...] = ()
    raw_custom_instructions: tuple[str, ...] = ()
    raw_metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedBundle:
    """normalize 阶段的输出，统一结构。"""

    platform: str
    conversations: tuple[ConversationMessage, ...]
    documents: tuple[SourceRecord, ...]
    custom_instructions: tuple[str, ...]
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ExternalImportResult:
    """外部导入最终结果。"""

    import_batch_id: str
    platform: str
    sources: tuple[SourceRecord, ...]
    candidates: tuple[ImportedMemoryCandidate, ...]
    role_stats: Mapping[str, int]  # user / assistant / system / tool / attachment / unknown
    trust_stats: Mapping[str, int]  # high / medium / low / unverified
    conflict_count: int
    needs_review_count: int
    high_trust_count: int
    low_trust_count: int
    summary: str
    error: str = ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """导入结果校验。"""

    is_valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


# ── Adapter 接口 ──


@runtime_checkable
class ExternalImportAdapter(Protocol):
    """外部导入 Adapter 协议。"""

    name: str

    def detect(self, source_bundle: str | bytes | Path) -> bool:  # noqa: D401
        """检测是否能处理此 source_bundle。"""
        ...

    def parse(self, source_bundle: str | bytes | Path) -> ParsedBundle:
        """解析原始包。"""
        ...

    def normalize(self, parsed: ParsedBundle) -> NormalizedBundle:
        """把解析结果归一化。"""
        ...

    def extract_sources(self, normalized: NormalizedBundle) -> tuple[SourceRecord, ...]:
        """从归一化结果抽取 L0 Source 记录。"""
        ...

    def to_memory_candidates(
        self, normalized: NormalizedBundle, *, import_batch_id: str
    ) -> tuple[ImportedMemoryCandidate, ...]:
        """从归一化结果生成 memory 候选。"""
        ...

    def validate_import_result(self, result: ExternalImportResult) -> ValidationResult:
        """校验导入结果。"""
        ...


# ── 通用工具 ──


def _read_text(source: str | bytes | Path) -> str:
    """从 str / bytes / Path 读取文本。"""
    if isinstance(source, bytes):
        return source.decode("utf-8", errors="replace")
    if isinstance(source, Path):
        return _read_text_from_path(source)
    if isinstance(source, str):
        # 短字符串当作内容本身；长字符串可能是文件路径
        if len(source) < 256 and (Path(source).exists() if source else False):
            return _read_text_from_path(Path(source))
        return source
    return ""


def _read_text_from_path(path: Path) -> str:
    """根据扩展名读取文本文件；PDF 走 pypdf 抽取。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def extract_pdf_text(source: Path | bytes) -> str:
    """从 PDF 文件 / bytes 抽取纯文本。

    使用 pypdf（纯 Python，无系统依赖）。若 pypdf 未安装或抽取失败，返回空字符串
    而不是抛错——调用方应保留原文件为 L0，由后续 OCR / Provider 链路处理。
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""

    try:
        if isinstance(source, bytes):
            reader = PdfReader(io.BytesIO(source))
        else:
            reader = PdfReader(str(source))
    except Exception:  # noqa: BLE001
        return ""

    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def extract_docx_text(source: Path | bytes) -> str:
    """从 DOCX 文件 / bytes 抽取纯文本。

    使用 python-docx（纯 Python，无系统依赖）。若未安装或抽取失败，返回空字符串。
    """
    try:
        from docx import Document
    except ImportError:
        return ""

    try:
        if isinstance(source, bytes):
            doc = Document(io.BytesIO(source))
        else:
            doc = Document(str(source))
    except Exception:  # noqa: BLE001
        return ""

    parts: list[str] = []
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


# ── 文件类型识别（与前端 detectFileCategory 对齐） ──

_TEXTUAL_EXTS = frozenset({".md", ".txt", ".json", ".jsonl", ".csv", ".html", ".htm"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm"})
_ARCHIVE_EXTS = frozenset({".zip"})


def detect_file_category(file_name: str) -> str:
    """根据文件名扩展名推断类别。

    与前端 ``knowledgeImportApi.js::detectFileCategory`` 保持一致，返回值：
    text | image | audio | video | archive | other
    """
    lower = (file_name or "").lower()
    dot = lower.rfind(".")
    if dot < 0:
        return "other"
    ext = lower[dot:]
    if ext in _TEXTUAL_EXTS:
        return "text"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _ARCHIVE_EXTS:
        return "archive"
    return "other"


def _normalize_role(role: str) -> str:
    """归一化角色名到标准枚举。"""
    if not role:
        return "unknown"
    r = role.strip().lower()
    if r in ("user", "human", "me"):
        return "user"
    if r in ("assistant", "ai", "bot", "model", "gpt", "claude"):
        return "assistant"
    if r in ("system", "developer", "instruction"):
        return "system"
    if r in ("tool", "function", "tool_result", "tool_call"):
        return "tool"
    if r in ("attachment", "file", "image", "document"):
        return "attachment"
    return "unknown"


def _compute_trust_level_for(role: str, source_type: str) -> str:
    """与 memory_quality_gate.compute_trust_level 对齐的简化版。"""
    if role == "user" and source_type in ("file", "document", "workspace"):
        return "high"
    if role == "user" and source_type == "custom_instruction":
        return "high"
    if role == "user" and source_type == "conversation":
        return "medium"
    if role == "system":
        return "medium"
    if role == "tool":
        return "medium"
    if role == "attachment":
        return "medium"
    if role == "assistant":
        return "low"
    return "unverified"


# ── Adapter 1: Generic Markdown / TXT ──


class GenericMarkdownTxtAdapter:
    """通用 Markdown / TXT 适配器。

    把整篇文档视为一条 user 来源的 file。
    从中抽取标题（# 开头）、明确偏好（我喜欢/我偏好）、项目事实作为候选。
    """

    name = "generic_markdown_txt"

    def detect(self, source_bundle: str | bytes | Path) -> bool:
        text = _read_text(source_bundle)
        if not text:
            return False
        # 启发式：包含 markdown 标记或纯文本
        return bool(re.search(r"^#+\s|^\*\s|^\-\s|^\d+\.\s", text, re.MULTILINE)) or ".md" in str(source_bundle).lower() or ".txt" in str(source_bundle).lower()

    def parse(self, source_bundle: str | bytes | Path) -> ParsedBundle:
        text = _read_text(source_bundle)
        # 抽取标题作为 metadata
        titles = re.findall(r"^#+\s+(.+)$", text, re.MULTILINE)
        return ParsedBundle(
            format="markdown",
            platform="generic",
            raw_documents=(
                SourceRecord(
                    source_id="doc-md-1",
                    source_type="document",
                    source_role="user",
                    title=titles[0] if titles else "Markdown 文档",
                    content=text,
                    source_ref="generic://markdown/doc-md-1",
                    evidence_refs=("generic://markdown/doc-md-1",),
                    raw_format="markdown",
                ),
            ),
            raw_metadata={"titles": tuple(titles)},
        )

    def normalize(self, parsed: ParsedBundle) -> NormalizedBundle:
        return NormalizedBundle(
            platform=parsed.platform,
            conversations=(),
            documents=parsed.raw_documents,
            custom_instructions=(),
            metadata=parsed.raw_metadata,
        )

    def extract_sources(self, normalized: NormalizedBundle) -> tuple[SourceRecord, ...]:
        return normalized.documents

    def to_memory_candidates(
        self, normalized: NormalizedBundle, *, import_batch_id: str
    ) -> tuple[ImportedMemoryCandidate, ...]:
        candidates: list[ImportedMemoryCandidate] = []
        for doc in normalized.documents:
            # 从文档中抽取偏好信号
            pref_matches = re.findall(
                r"(我喜欢|我偏好|我不喜欢|请用|请保持|不要用)([^\n。；,，]{4,60})",
                doc.content,
            )
            for idx, (marker, value) in enumerate(pref_matches):
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"{doc.source_id}-pref-{idx}",
                    layer="L4",
                    type="preference",
                    content=f"{marker}{value}",
                    summary=f"从文档抽取的偏好信号：{marker}{value}",
                    confidence=0.78,
                    trust_level="high",  # user + document
                    source_platform="generic",
                    source_type="document",
                    source_role="user",
                    source_ref=doc.source_ref,
                    evidence_refs=doc.evidence_refs,
                    occurred_at=_source_occurred_at(doc),
                    import_batch_id=import_batch_id,
                ))
            # 抽取项目事实（含「项目」「目标」「决策」关键词的段落，关键词可在开头）
            fact_matches = re.findall(
                r"([^\n。；]{0,80}(?:项目|目标|决策|里程碑|发布)[^\n。；]{0,80})",
                doc.content,
            )
            # 过滤过短的事实片段
            fact_matches = [f for f in fact_matches if len(f.strip()) >= 6]
            for idx, fact in enumerate(fact_matches[:10]):  # 限制每文档最多 10 条事实
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"{doc.source_id}-fact-{idx}",
                    layer="L1",
                    type="fact",
                    content=fact,
                    summary=f"从文档抽取的项目事实：{fact[:40]}",
                    confidence=0.82,
                    trust_level="high",
                    source_platform="generic",
                    source_type="document",
                    source_role="user",
                    source_ref=doc.source_ref,
                    evidence_refs=doc.evidence_refs,
                    occurred_at=_source_occurred_at(doc),
                    import_batch_id=import_batch_id,
                ))
        return tuple(candidates)

    def validate_import_result(self, result: ExternalImportResult) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if not result.sources:
            warnings.append("Markdown 导入未抽取到任何 Source")
        for c in result.candidates:
            if not c.evidence_refs:
                errors.append(f"候选 {c.memory_id} 缺少 evidence_refs")
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


# ── Adapter 2: Generic JSON / JSONL Conversation ──


class GenericJsonConversationAdapter:
    """通用 JSON / JSONL 对话适配器。

    接受两种格式：
    - JSON: {"messages": [{"role": "user", "content": "..."}, ...]}
    - JSONL: 每行一条 {"role": "...", "content": "..."}

    也接受 {"conversations": [{"from": "human", "value": "..."}, ...]} 格式。
    """

    name = "generic_json_conversation"

    def detect(self, source_bundle: str | bytes | Path) -> bool:
        text = _read_text(source_bundle)
        if not text:
            return False
        stripped = text.lstrip()
        if not stripped.startswith(("{", "[")):
            return False
        # 启发式：包含 role / from / messages / conversations 字段
        return bool(re.search(r'"(?:role|from|messages|conversations)"\s*:', text))

    def parse(self, source_bundle: str | bytes | Path) -> ParsedBundle:
        text = _read_text(source_bundle)
        messages: list[ConversationMessage] = []
        try:
            # 尝试 JSONL
            if "\n{" in text and not text.lstrip().startswith("["):
                for line in text.strip().split("\n"):
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    obj = json.loads(line)
                    self._append_message_from_obj(obj, messages)
            else:
                # 整体 JSON
                data = json.loads(text)
                if isinstance(data, list):
                    for obj in data:
                        self._append_message_from_obj(obj, messages)
                elif isinstance(data, dict):
                    raw_msgs = data.get("messages") or data.get("conversations") or []
                    for obj in raw_msgs:
                        self._append_message_from_obj(obj, messages)
        except json.JSONDecodeError as exc:
            raise ExternalImportError(f"JSON 解析失败：{exc}") from exc

        return ParsedBundle(
            format="json",
            platform="generic",
            raw_messages=tuple(messages),
        )

    def _append_message_from_obj(self, obj: Mapping[str, Any], messages: list[ConversationMessage]) -> None:
        role = obj.get("role") or obj.get("from") or obj.get("sender") or "unknown"
        content = obj.get("content") or obj.get("value") or obj.get("text") or ""
        timestamp = obj.get("timestamp") or obj.get("created_at") or ""
        message_id = obj.get("id") or obj.get("message_id") or ""
        thread_id = obj.get("thread_id") or obj.get("conversation_id") or ""
        project_id = obj.get("project_id") or obj.get("workspace_id") or ""
        attachments = obj.get("attachments") or ()
        if isinstance(attachments, list):
            attachments = tuple(str(a) for a in attachments)
        else:
            attachments = ()
        messages.append(ConversationMessage(
            role=_normalize_role(role),
            content=str(content),
            timestamp=str(timestamp),
            occurred_at=_external_occurred_at(timestamp),
            message_id=str(message_id),
            thread_id=str(thread_id),
            project_id=str(project_id),
            attachments=attachments,
        ))

    def normalize(self, parsed: ParsedBundle) -> NormalizedBundle:
        return NormalizedBundle(
            platform=parsed.platform,
            conversations=parsed.raw_messages,
            documents=(),
            custom_instructions=(),
            metadata=parsed.raw_metadata,
        )

    def extract_sources(self, normalized: NormalizedBundle) -> tuple[SourceRecord, ...]:
        sources: list[SourceRecord] = []
        for idx, msg in enumerate(normalized.conversations):
            sources.append(SourceRecord(
                source_id=f"conv-msg-{idx}",
                source_type="conversation",
                source_role=msg.role,
                title=f"对话消息 {idx}",
                content=msg.content,
                source_ref=f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                evidence_refs=(f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                occurred_at=msg.occurred_at,
                raw_format="json",
            ))
        return tuple(sources)

    def to_memory_candidates(
        self, normalized: NormalizedBundle, *, import_batch_id: str
    ) -> tuple[ImportedMemoryCandidate, ...]:
        candidates: list[ImportedMemoryCandidate] = []
        for idx, msg in enumerate(normalized.conversations):
            # user 消息：抽取为 L1 fact 候选（如果内容足够长）
            if msg.role == "user" and len(msg.content.strip()) >= 8:
                trust = _compute_trust_level_for("user", "conversation")
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"conv-cand-{idx}",
                    layer="L1",
                    type="fact",
                    content=msg.content[:200],
                    summary=f"用户消息候选：{msg.content[:40]}",
                    confidence=0.72,
                    trust_level=trust,
                    source_platform="generic",
                    source_type="conversation",
                    source_role="user",
                    source_ref=f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                    evidence_refs=(f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                    occurred_at=msg.occurred_at,
                    import_batch_id=import_batch_id,
                ))
            # assistant 消息：只作为候选，低可信
            elif msg.role == "assistant" and len(msg.content.strip()) >= 10:
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"conv-cand-{idx}",
                    layer="L0",  # assistant 默认不进入 L1
                    type="other",
                    content=msg.content[:200],
                    summary=f"assistant 回复候选：{msg.content[:40]}",
                    confidence=0.45,
                    trust_level="low",
                    source_platform="generic",
                    source_type="conversation",
                    source_role="assistant",
                    source_ref=f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                    evidence_refs=(f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                    occurred_at=msg.occurred_at,
                    status="needs_review",
                    import_batch_id=import_batch_id,
                ))
            # system 消息：只能作为待确认 L4 Persona 候选
            elif msg.role == "system" and len(msg.content.strip()) >= 8:
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"conv-cand-{idx}",
                    layer="L4",
                    type="persona",
                    content=msg.content[:200],
                    summary=f"系统指令候选：{msg.content[:40]}",
                    confidence=0.65,
                    trust_level="medium",
                    source_platform="generic",
                    source_type="custom_instruction",
                    source_role="system",
                    source_ref=f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                    evidence_refs=(f"generic://conversation/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                    occurred_at=msg.occurred_at,
                    status="needs_review",
                    import_batch_id=import_batch_id,
                ))
        return tuple(candidates)

    def validate_import_result(self, result: ExternalImportResult) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if not result.sources:
            warnings.append("JSON 对话导入未抽取到任何消息")
        # 检查 assistant 候选是否都被标记为 needs_review
        for c in result.candidates:
            if c.source_role == "assistant" and c.status == "confirmed":
                errors.append(f"assistant 候选 {c.memory_id} 不应自动确认")
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


# ── Adapter 3: Generic ZIP Knowledge Bundle ──


class GenericZipBundleAdapter:
    """通用 ZIP 知识包适配器。

    把 ZIP 内每个 .md / .txt / .json / .jsonl / .csv / .html 视为一份文档。
    若包含 manifest.json，读取其 metadata。
    """

    name = "generic_zip_bundle"
    MAX_ENTRIES = 4096
    MAX_ENTRY_BYTES = 32 * 1024 * 1024
    MAX_TOTAL_BYTES = 256 * 1024 * 1024

    def detect(self, source_bundle: str | bytes | Path) -> bool:
        if isinstance(source_bundle, bytes):
            return source_bundle[:4] in (
                b"PK\x03\x04",  # local file header
                b"PK\x05\x06",  # empty archive end-of-central-directory
                b"PK\x07\x08",  # spanned archive data descriptor
            )
        s = str(source_bundle).lower()
        return s.endswith(".zip") or (isinstance(source_bundle, Path) and source_bundle.suffix.lower() == ".zip")

    def parse(self, source_bundle: str | bytes | Path) -> ParsedBundle:
        try:
            if isinstance(source_bundle, bytes):
                zf = zipfile.ZipFile(io.BytesIO(source_bundle))
            elif isinstance(source_bundle, Path):
                zf = zipfile.ZipFile(source_bundle)
            elif isinstance(source_bundle, str) and Path(source_bundle).exists():
                zf = zipfile.ZipFile(source_bundle)
            else:
                raise ExternalImportError("ZIP bundle 需要有效的 bytes / Path / 文件路径")
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            raise ExternalImportError("ZIP 包损坏或无法读取") from exc

        documents: list[SourceRecord] = []
        custom_instructions: list[str] = []
        metadata: dict[str, object] = {}

        with zf:
            entries = [info for info in zf.infolist() if not info.is_dir()]
            if len(entries) > self.MAX_ENTRIES:
                raise ExternalImportError(
                    f"ZIP 包文件数超过上限 {self.MAX_ENTRIES}"
                )
            names = [info.filename for info in entries]
            if len(names) != len(set(names)):
                raise ExternalImportError("ZIP 包包含重复文件名")
            if any(info.flag_bits & 0x1 for info in entries):
                raise ExternalImportError("ZIP 包包含加密文件，当前无法导入")
            if any(info.file_size > self.MAX_ENTRY_BYTES for info in entries):
                raise ExternalImportError(
                    f"ZIP 包单个文件解压后超过 {self.MAX_ENTRY_BYTES} 字节"
                )
            expanded_size = sum(info.file_size for info in entries)
            if expanded_size > self.MAX_TOTAL_BYTES:
                raise ExternalImportError(
                    f"ZIP 包解压后总大小超过 {self.MAX_TOTAL_BYTES} 字节"
                )

            for info in entries:
                name = info.filename
                lower = name.lower()

                # PDF 是二进制，单独走 pypdf 抽取
                if lower.endswith(".pdf"):
                    try:
                        raw_bytes = zf.read(info)
                    except Exception:  # noqa: BLE001
                        continue
                    pdf_text = extract_pdf_text(raw_bytes)
                    if pdf_text:
                        documents.append(SourceRecord(
                            source_id=f"zip-doc-{name}",
                            source_type="document",
                            source_role="user",
                            title=name,
                            content=pdf_text,
                            source_ref=f"generic://zip/{name}",
                            evidence_refs=(f"generic://zip/{name}",),
                            raw_format="pdf",
                        ))
                    continue

                try:
                    content = zf.read(info).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue

                if lower == "manifest.json":
                    try:
                        metadata = json.loads(content)
                    except json.JSONDecodeError:
                        pass
                    continue
                # custom_instructions 必须在通用 .md 检查之前匹配
                if lower in ("custom_instructions.md", "instructions.md", "custom_instruction.md"):
                    custom_instructions.append(content)
                elif lower.endswith(".md") or lower.endswith(".txt"):
                    documents.append(SourceRecord(
                        source_id=f"zip-doc-{name}",
                        source_type="document",
                        source_role="user",
                        title=name,
                        content=content,
                        source_ref=f"generic://zip/{name}",
                        evidence_refs=(f"generic://zip/{name}",),
                        raw_format="markdown",
                    ))
                elif lower.endswith((".json", ".jsonl", ".csv", ".html", ".htm")):
                    raw_format = Path(lower).suffix.lstrip(".")
                    documents.append(SourceRecord(
                        source_id=f"zip-doc-{name}",
                        source_type="document",
                        source_role="user",
                        title=name,
                        content=content,
                        source_ref=f"generic://zip/{name}",
                        evidence_refs=(f"generic://zip/{name}",),
                        raw_format=raw_format,
                    ))
                elif lower.endswith(".docx"):
                    # DOCX 二进制，走 python-docx 抽取
                    try:
                        raw_bytes = zf.read(info)
                    except Exception:  # noqa: BLE001
                        continue
                    docx_text = extract_docx_text(raw_bytes)
                    if docx_text:
                        documents.append(SourceRecord(
                            source_id=f"zip-doc-{name}",
                            source_type="document",
                            source_role="user",
                            title=name,
                            content=docx_text,
                            source_ref=f"generic://zip/{name}",
                            evidence_refs=(f"generic://zip/{name}",),
                            raw_format="docx",
                        ))

        return ParsedBundle(
            format="zip",
            platform="generic",
            raw_documents=tuple(documents),
            raw_custom_instructions=tuple(custom_instructions),
            raw_metadata=metadata,
        )

    def normalize(self, parsed: ParsedBundle) -> NormalizedBundle:
        return NormalizedBundle(
            platform=parsed.platform,
            conversations=(),
            documents=parsed.raw_documents,
            custom_instructions=parsed.raw_custom_instructions,
            metadata=parsed.raw_metadata,
        )

    def extract_sources(self, normalized: NormalizedBundle) -> tuple[SourceRecord, ...]:
        instruction_sources = tuple(
            SourceRecord(
                source_id=f"zip-custom-instruction-{idx}",
                source_type="custom_instruction",
                source_role="user",
                title=f"custom_instructions/{idx}",
                content=content,
                source_ref=f"generic://zip/custom_instructions/{idx}",
                evidence_refs=(f"generic://zip/custom_instructions/{idx}",),
                raw_format="markdown",
            )
            for idx, content in enumerate(normalized.custom_instructions)
        )
        return (*normalized.documents, *instruction_sources)

    def to_memory_candidates(
        self, normalized: NormalizedBundle, *, import_batch_id: str
    ) -> tuple[ImportedMemoryCandidate, ...]:
        candidates: list[ImportedMemoryCandidate] = []
        # custom_instructions → 待确认 L4 Persona 候选
        for idx, ci in enumerate(normalized.custom_instructions):
            candidates.append(ImportedMemoryCandidate(
                memory_id=f"zip-ci-{idx}",
                layer="L4",
                type="persona",
                content=ci[:300],
                summary=f"自定义指令候选：{ci[:40]}",
                confidence=0.85,
                trust_level="high",
                source_platform="generic",
                source_type="custom_instruction",
                source_role="user",
                source_ref=f"generic://zip/custom_instructions/{idx}",
                evidence_refs=(f"generic://zip/custom_instructions/{idx}",),
                import_batch_id=import_batch_id,
                status="needs_review",  # L4 Persona 候选必须待确认
            ))
        # 文档作为 L1 fact 候选
        for doc in normalized.documents:
            candidates.append(ImportedMemoryCandidate(
                memory_id=f"{doc.source_id}-fact-0",
                layer="L1",
                type="fact",
                content=doc.content[:200],
                summary=f"从 ZIP 文档抽取：{doc.title}",
                confidence=0.78,
                trust_level="high",
                source_platform="generic",
                source_type="document",
                source_role="user",
                source_ref=doc.source_ref,
                evidence_refs=doc.evidence_refs,
                import_batch_id=import_batch_id,
            ))
        return tuple(candidates)

    def validate_import_result(self, result: ExternalImportResult) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if not result.sources:
            warnings.append("ZIP 包内未找到可解析文档")
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


# ── Adapter 4: Generic LLM Conversation (OpenAI/ChatGPT/Claude export) ──


class GenericLLMConversationAdapter:
    """通用 LLM 对话导出适配器。

    支持多种 LLM 平台的对话导出格式，包括：
    - ChatGPT conversations.json: [{"title": ..., "mapping": {...}}]
    - Claude chat history: {"messages": [...]}
    - 通用 {role, content} 数组

    关键规则：assistant 回复默认 low trust，不直接进入长期事实。
    """

    name = "generic_llm_conversation"

    def detect(self, source_bundle: str | bytes | Path) -> bool:
        text = _read_text(source_bundle)
        if not text or not text.lstrip().startswith(("{", "[")):
            return False
        # 启发式：包含 mapping / title / model 等 LLM 平台字段
        return bool(re.search(r'"(?:mapping|title|model|conversation_id|chat_id)"\s*:', text))

    def parse(self, source_bundle: str | bytes | Path) -> ParsedBundle:
        text = _read_text(source_bundle)
        messages: list[ConversationMessage] = []
        platform = "generic"

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExternalImportError(f"LLM 对话 JSON 解析失败：{exc}") from exc

        if isinstance(data, list):
            # ChatGPT conversations.json 格式
            platform = "chatgpt_export"
            for conv in data:
                if not isinstance(conv, dict):
                    continue
                title = conv.get("title", "未命名对话")
                conv_id = conv.get("id") or conv.get("conversation_id") or ""
                mapping = conv.get("mapping", {})
                if isinstance(mapping, dict):
                    for node_id, node in mapping.items():
                        if not isinstance(node, dict):
                            continue
                        msg = node.get("message")
                        if not isinstance(msg, dict):
                            continue
                        author = msg.get("author", {})
                        role = author.get("role", "unknown") if isinstance(author, dict) else "unknown"
                        content_obj = msg.get("content", {})
                        content = ""
                        if isinstance(content_obj, dict):
                            parts = content_obj.get("parts", [])
                            if isinstance(parts, list):
                                content = "\n".join(str(p) for p in parts if isinstance(p, (str, int, float)))
                            elif isinstance(content_obj.get("text"), str):
                                content = content_obj["text"]
                        create_time = msg.get("create_time")
                        timestamp = str(create_time) if create_time else ""
                        if content:
                            messages.append(ConversationMessage(
                                role=_normalize_role(role),
                                content=content,
                                timestamp=timestamp,
                                occurred_at=_external_occurred_at(timestamp),
                                message_id=node_id,
                                thread_id=str(conv_id),
                                attachments=(),
                            ))
        elif isinstance(data, dict):
            # Claude 或通用 {messages: [...]} 格式
            platform = data.get("platform", "generic")
            raw_msgs = data.get("messages") or data.get("conversations") or data.get("chat_history") or []
            for obj in raw_msgs:
                if not isinstance(obj, dict):
                    continue
                role = obj.get("role") or obj.get("from") or obj.get("sender") or "unknown"
                content = obj.get("content") or obj.get("value") or obj.get("text") or ""
                if isinstance(content, list):
                    content = "\n".join(str(p) for p in content)
                timestamp = obj.get("timestamp") or obj.get("created_at") or ""
                messages.append(ConversationMessage(
                    role=_normalize_role(role),
                    content=str(content),
                    timestamp=str(timestamp),
                    occurred_at=_external_occurred_at(timestamp),
                    message_id=str(obj.get("id", "")),
                    thread_id=str(obj.get("thread_id") or obj.get("conversation_id") or ""),
                    attachments=(),
                ))

        return ParsedBundle(
            format="json",
            platform=platform,
            raw_messages=tuple(messages),
        )

    def normalize(self, parsed: ParsedBundle) -> NormalizedBundle:
        return NormalizedBundle(
            platform=parsed.platform,
            conversations=parsed.raw_messages,
            documents=(),
            custom_instructions=(),
            metadata=parsed.raw_metadata,
        )

    def extract_sources(self, normalized: NormalizedBundle) -> tuple[SourceRecord, ...]:
        sources: list[SourceRecord] = []
        for idx, msg in enumerate(normalized.conversations):
            sources.append(SourceRecord(
                source_id=f"llm-msg-{idx}",
                source_type="conversation",
                source_role=msg.role,
                title=f"LLM 对话 {idx}",
                content=msg.content,
                source_ref=f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                evidence_refs=(f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                occurred_at=msg.occurred_at,
                raw_format="json",
            ))
        return tuple(sources)

    def to_memory_candidates(
        self, normalized: NormalizedBundle, *, import_batch_id: str
    ) -> tuple[ImportedMemoryCandidate, ...]:
        candidates: list[ImportedMemoryCandidate] = []
        # 统计 user / assistant / system
        for idx, msg in enumerate(normalized.conversations):
            trust = _compute_trust_level_for(msg.role, "conversation")
            # user 消息：L1 fact 候选
            if msg.role == "user" and len(msg.content.strip()) >= 8:
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"llm-cand-{idx}",
                    layer="L1",
                    type="fact",
                    content=msg.content[:200],
                    summary=f"用户消息：{msg.content[:40]}",
                    confidence=0.72,
                    trust_level=trust,
                    source_platform=normalized.platform,
                    source_type="conversation",
                    source_role="user",
                    source_ref=f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                    evidence_refs=(f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                    occurred_at=msg.occurred_at,
                    import_batch_id=import_batch_id,
                ))
            # assistant 消息：L0 only，low trust，needs_review
            elif msg.role == "assistant" and len(msg.content.strip()) >= 10:
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"llm-cand-{idx}",
                    layer="L0",  # 不进入 L1
                    type="other",
                    content=msg.content[:200],
                    summary=f"assistant 回复（仅作参考）：{msg.content[:40]}",
                    confidence=0.40,
                    trust_level="low",
                    source_platform=normalized.platform,
                    source_type="conversation",
                    source_role="assistant",
                    source_ref=f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                    evidence_refs=(f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                    occurred_at=msg.occurred_at,
                    status="needs_review",
                    import_batch_id=import_batch_id,
                ))
            # system 消息：待确认 L4 Persona 候选
            elif msg.role == "system" and len(msg.content.strip()) >= 8:
                candidates.append(ImportedMemoryCandidate(
                    memory_id=f"llm-cand-{idx}",
                    layer="L4",
                    type="persona",
                    content=msg.content[:200],
                    summary=f"系统指令候选：{msg.content[:40]}",
                    confidence=0.65,
                    trust_level="medium",
                    source_platform=normalized.platform,
                    source_type="custom_instruction",
                    source_role="system",
                    source_ref=f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",
                    evidence_refs=(f"llm://{normalized.platform}/{msg.thread_id or 'default'}/{msg.message_id or idx}",),
                    occurred_at=msg.occurred_at,
                    status="needs_review",
                    import_batch_id=import_batch_id,
                ))
        return tuple(candidates)

    def validate_import_result(self, result: ExternalImportResult) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        # 强制：所有 assistant 候选必须 needs_review
        for c in result.candidates:
            if c.source_role == "assistant":
                if c.status != "needs_review":
                    errors.append(f"assistant 候选 {c.memory_id} 必须为 needs_review，实际 {c.status}")
                if c.layer not in ("L0",):
                    errors.append(f"assistant 候选 {c.memory_id} 不应进入 {c.layer}，默认只保留 L0")
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


# ── Adapter Registry ──


_DEFAULT_ADAPTERS: tuple[ExternalImportAdapter, ...] = (
    GenericZipBundleAdapter(),
    GenericLLMConversationAdapter(),
    GenericJsonConversationAdapter(),
    GenericMarkdownTxtAdapter(),
)


def get_default_adapters() -> tuple[ExternalImportAdapter, ...]:
    """返回默认 adapter 列表（按优先级排序）。"""
    return _DEFAULT_ADAPTERS


def detect_adapter(
    source_bundle: str | bytes | Path,
    *,
    adapters: Sequence[ExternalImportAdapter] | None = None,
) -> ExternalImportAdapter | None:
    """自动检测适配的 adapter。"""
    pool = tuple(adapters) if adapters else _DEFAULT_ADAPTERS
    for adapter in pool:
        try:
            if adapter.detect(source_bundle):
                return adapter
        except Exception:  # noqa: BLE001 - detect 失败跳过
            continue
    return None


# ── 主入口 ──


def import_external_bundle(
    source_bundle: str | bytes | Path,
    *,
    import_batch_id: str,
    adapters: Sequence[ExternalImportAdapter] | None = None,
) -> ExternalImportResult:
    """导入外部 LLM / 知识包。

    流程：detect → parse → normalize → extract_sources → to_memory_candidates → validate
    """
    adapter = detect_adapter(source_bundle, adapters=adapters)
    if adapter is None:
        return ExternalImportResult(
            import_batch_id=import_batch_id,
            platform="unknown",
            sources=(),
            candidates=(),
            role_stats={},
            trust_stats={},
            conflict_count=0,
            needs_review_count=0,
            high_trust_count=0,
            low_trust_count=0,
            summary="未识别到匹配的导入 adapter",
            error="no_matching_adapter",
        )

    try:
        parsed = adapter.parse(source_bundle)
        normalized = adapter.normalize(parsed)
        sources = adapter.extract_sources(normalized)
        candidates = adapter.to_memory_candidates(normalized, import_batch_id=import_batch_id)
        if not sources and not candidates:
            raise ExternalImportError("导入包未包含可解析内容")
    except ExternalImportError as exc:
        return ExternalImportResult(
            import_batch_id=import_batch_id,
            platform=adapter.name,
            sources=(),
            candidates=(),
            role_stats={},
            trust_stats={},
            conflict_count=0,
            needs_review_count=0,
            high_trust_count=0,
            low_trust_count=0,
            summary=f"导入失败：{exc}",
            error=str(exc),
        )

    # 统计
    role_stats: dict[str, int] = {}
    trust_stats: dict[str, int] = {}
    needs_review_count = 0
    high_trust_count = 0
    low_trust_count = 0
    for c in candidates:
        role_stats[c.source_role] = role_stats.get(c.source_role, 0) + 1
        trust_stats[c.trust_level] = trust_stats.get(c.trust_level, 0) + 1
        if c.status == "needs_review":
            needs_review_count += 1
        if c.trust_level == "high":
            high_trust_count += 1
        if c.trust_level == "low":
            low_trust_count += 1

    result = ExternalImportResult(
        import_batch_id=import_batch_id,
        platform=normalized.platform,
        sources=sources,
        candidates=candidates,
        role_stats=dict(role_stats),
        trust_stats=dict(trust_stats),
        conflict_count=0,  # 冲突检测由 quality gate 后续处理
        needs_review_count=needs_review_count,
        high_trust_count=high_trust_count,
        low_trust_count=low_trust_count,
        summary=(
            f"从 {normalized.platform} 导入 {len(sources)} 条 Source、"
            f"{len(candidates)} 条候选；"
            f"user {role_stats.get('user', 0)}，"
            f"assistant {role_stats.get('assistant', 0)}，"
            f"system {role_stats.get('system', 0)}；"
            f"高可信 {high_trust_count}，低可信 {low_trust_count}，"
            f"待确认 {needs_review_count}"
        ),
    )

    return result


def validate_import_result(result: ExternalImportResult) -> ValidationResult:
    """用候选的 adapter 校验导入结果。"""
    # 通用校验：所有候选必须有 evidence_refs
    errors: list[str] = []
    warnings: list[str] = []
    for c in result.candidates:
        if not c.evidence_refs:
            errors.append(f"候选 {c.memory_id} 缺少 evidence_refs")
    if result.error:
        warnings.append(f"导入过程中出现错误：{result.error}")
    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


# ── 序列化 ──


def _external_occurred_at(value: object) -> str | None:
    """Return an external fact time and preserve an unknown value as None."""
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def _source_occurred_at(record: SourceRecord) -> str | None:
    """Read dual time first, then the historical created_at export alias."""
    return _external_occurred_at(record.occurred_at or record.created_at)


def _recorded_at(record: SourceRecord | ImportedMemoryCandidate) -> str:
    """Read dual time first, then the historical observed_at export alias."""
    return str(record.recorded_at or record.observed_at or "")


def serialize_source_record(record: SourceRecord) -> dict[str, object]:
    occurred_at = _source_occurred_at(record)
    recorded_at = _recorded_at(record)
    return {
        "source_id": record.source_id,
        "source_type": record.source_type,
        "source_role": record.source_role,
        "title": record.title,
        "content": record.content,
        "source_ref": record.source_ref,
        "evidence_refs": list(record.evidence_refs),
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "created_at": occurred_at or recorded_at,
        "observed_at": recorded_at,
        "import_batch_id": record.import_batch_id,
        "raw_format": record.raw_format,
    }


def serialize_imported_memory_candidate(candidate: ImportedMemoryCandidate) -> dict[str, object]:
    occurred_at = _external_occurred_at(candidate.occurred_at or candidate.created_at)
    recorded_at = _recorded_at(candidate)
    return {
        "memory_id": candidate.memory_id,
        "layer": candidate.layer,
        "type": candidate.type,
        "content": candidate.content,
        "summary": candidate.summary,
        "confidence": candidate.confidence,
        "trust_level": candidate.trust_level,
        "source_platform": candidate.source_platform,
        "source_type": candidate.source_type,
        "source_role": candidate.source_role,
        "source_ref": candidate.source_ref,
        "evidence_refs": list(candidate.evidence_refs),
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "created_at": occurred_at or recorded_at,
        "observed_at": recorded_at,
        "conflict_refs": list(candidate.conflict_refs),
        "status": candidate.status,
        "import_batch_id": candidate.import_batch_id,
        "privacy_level": candidate.privacy_level,
        "provider_boundary": candidate.provider_boundary,
    }


def serialize_external_import_result(result: ExternalImportResult) -> dict[str, object]:
    return {
        "import_batch_id": result.import_batch_id,
        "platform": result.platform,
        "sources": [serialize_source_record(s) for s in result.sources],
        "candidates": [serialize_imported_memory_candidate(c) for c in result.candidates],
        "role_stats": dict(result.role_stats),
        "trust_stats": dict(result.trust_stats),
        "conflict_count": result.conflict_count,
        "needs_review_count": result.needs_review_count,
        "high_trust_count": result.high_trust_count,
        "low_trust_count": result.low_trust_count,
        "summary": result.summary,
        "error": result.error,
    }


def serialize_validation_result(result: ValidationResult) -> dict[str, object]:
    return {
        "is_valid": result.is_valid,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
    }
