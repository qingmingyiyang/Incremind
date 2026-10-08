from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from core.memory_core import (
    MemoryCandidateRepositoryError,
    ObjectStoreMemoryCandidateRepository,
    memory_candidate_id,
)
from core.product_core.team_memory_source_authority_saga import (
    ObjectStoreTeamSourceAuthority,
)
from core.product_core.team_memory_source_staging import (
    TeamMemorySourceStagingRepository,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
_BULLET = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+")
_TABLE_SEPARATOR = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)
_DECISION_MARKERS = ("决定", "确定", "采用", "选择", "必须", "规则", "约束", "不得")
_ACTION_MARKERS = ("待办", "行动", "下一步", "需要", "计划", "将要", "请", "负责")
_MAX_ATOM_ITEMS = 80
_MAX_SCENARIO_ITEMS = 24
_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("记忆", ("记忆", "memory", "atom", "scenario", "系列", "来源")),
    ("项目", ("项目", "任务", "交付", "验收", "里程碑")),
    ("决策", ("决定", "采用", "选择", "必须", "规则", "约束")),
    ("行动", ("待办", "行动", "下一步", "需要", "计划", "负责")),
    ("用户", ("用户", "偏好", "需求", "体验")),
    ("证据", ("证据", "来源", "引用", "原文", "链接")),
)


class TeamMemorySourceExtractionError(ValueError):
    """Raised when a Team Source cannot be extracted safely."""


class TeamMemorySourceExtractionConflict(TeamMemorySourceExtractionError):
    """Raised when extraction input or a deterministic identity drifted."""


@dataclass(frozen=True, slots=True)
class TeamMemorySourceExtractionItem:
    item_id: str
    target_layer: str
    candidate_type: str
    title: str
    proposed_content: str
    source_locator: str
    source_quote: str
    start_char: int
    end_char: int
    paragraph_ids: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TeamMemorySourceExtractionPreview:
    preview_id: str
    staging_id: str
    staging_revision: int
    source_id: str
    source_uri: str
    source_revision: int
    source_content_sha256: str
    project_id: str
    items: tuple[TeamMemorySourceExtractionItem, ...]
    safety: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TeamMemorySourceExtractionResult:
    candidate_id: str
    item_id: str
    status: str
    target_layer: str
    replayed: bool
    candidate: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Paragraph:
    paragraph_id: str
    text: str
    start_char: int
    end_char: int


class PrepareTeamMemorySourceExtraction:
    """Preview deterministic L1/L2 items and stage one selected review candidate."""

    def __init__(
        self,
        *,
        staging: TeamMemorySourceStagingRepository,
        sources: ObjectStoreTeamSourceAuthority,
        candidates: ObjectStoreMemoryCandidateRepository,
    ) -> None:
        self._staging = staging
        self._sources = sources
        self._candidates = candidates

    def preview(self, staging_id: str) -> TeamMemorySourceExtractionPreview:
        (
            clean_staging_id,
            record,
            source,
            source_revision,
            source_hash,
            source_uri,
            content,
        ) = self._verified_source(staging_id)
        staging_revision = self._staging.revision(clean_staging_id)
        paragraphs = _paragraphs(content)
        items = (
            *_atom_items(
                paragraphs,
                staging_id=clean_staging_id,
                source_id=str(source["id"]),
                source_revision=source_revision,
                source_hash=source_hash,
                source_uri=source_uri,
            ),
            *_scenario_items(
                content,
                paragraphs,
                staging_id=clean_staging_id,
                source_id=str(source["id"]),
                source_revision=source_revision,
                source_hash=source_hash,
                source_uri=source_uri,
            ),
        )
        if not items:
            raise TeamMemorySourceExtractionError(
                "team Source has no extractable L1 or L2 content"
            )
        preview_id = _stable_id(
            "team-source-extraction-preview",
            clean_staging_id,
            str(staging_revision),
            str(source["id"]),
            str(source_revision),
            source_hash,
            *[item.item_id for item in items],
        )
        return TeamMemorySourceExtractionPreview(
            preview_id=preview_id,
            staging_id=clean_staging_id,
            staging_revision=staging_revision,
            source_id=str(source["id"]),
            source_uri=source_uri,
            source_revision=source_revision,
            source_content_sha256=source_hash,
            project_id=_identifier(record.get("project_id"), "project_id"),
            items=items,
            safety={
                "requires_user_confirmation": True,
                "one_item_per_request": True,
                "deterministic_local_extraction": True,
                "provider_called": False,
                "candidate_only": True,
                "publication_created": False,
                "automatic_recall_enabled": False,
            },
        )

    def stage(
        self,
        staging_id: str,
        *,
        preview_id: str,
        item_id: str,
        target_layer: str,
        expected_staging_revision: int,
        expected_source_revision: int,
        confirmed: bool,
        created_at: str,
    ) -> TeamMemorySourceExtractionResult:
        if confirmed is not True:
            raise TeamMemorySourceExtractionError(
                "team Source extraction requires explicit confirmation"
            )
        preview = self.preview(staging_id)
        if preview.preview_id != preview_id:
            raise TeamMemorySourceExtractionConflict(
                "team Source extraction preview identity drifted"
            )
        if preview.staging_revision != _positive_int(
            expected_staging_revision, "expected_staging_revision"
        ):
            raise TeamMemorySourceExtractionConflict(
                "team Source staging revision drifted"
            )
        if preview.source_revision != _positive_int(
            expected_source_revision, "expected_source_revision"
        ):
            raise TeamMemorySourceExtractionConflict(
                "team Source authority revision drifted"
            )
        clean_item_id = _identifier(item_id, "item_id")
        clean_target = target_layer.strip() if isinstance(target_layer, str) else ""
        item = next((value for value in preview.items if value.item_id == clean_item_id), None)
        if item is None:
            raise TeamMemorySourceExtractionConflict(
                "team Source extraction item drifted"
            )
        if clean_target != item.target_layer:
            raise TeamMemorySourceExtractionConflict(
                "team Source extraction target layer drifted"
            )
        timestamp = _timestamp(created_at, "created_at")
        candidate_id = memory_candidate_id(
            "team-source-extraction",
            preview.staging_id,
            str(preview.staging_revision),
            preview.source_id,
            str(preview.source_revision),
            preview.source_content_sha256,
            item.item_id,
            item.target_layer,
            item.candidate_type,
        )
        candidate = {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": preview.project_id,
            "target_layer": item.target_layer,
            "candidate_type": item.candidate_type,
            "status": "pending_review",
            "proposed_content": item.proposed_content,
            "source_refs": [
                {
                    "source_id": preview.source_id,
                    "locator": item.source_locator,
                    "quote": item.source_quote,
                }
            ],
            "provenance": {
                "model_result_id": None,
                "model_request_id": None,
                "recall_result_id": None,
                "document_id": None,
                "document_revision": None,
                "source_id": preview.source_id,
                "source_revision": preview.source_revision,
                "source_content_sha256": preview.source_content_sha256,
                "input_refs": [
                    {
                        "kind": "source",
                        "object_id": preview.source_id,
                        "uri": item.source_locator,
                    }
                ],
            },
            "extraction": {
                "schema_version": "1.0.0",
                "method": "deterministic_local_v1",
                "preview_id": preview.preview_id,
                "item_id": item.item_id,
                "title": item.title,
                "start_char": item.start_char,
                "end_char": item.end_char,
                "paragraph_ids": list(item.paragraph_ids),
                "tags": list(item.tags),
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": (
                    "本地确定性提取仅形成候选；用户确认后仍须经过既有"
                    " Memory publication 二次确认。"
                ),
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        existing = self._candidates.get(candidate_id)
        if existing is not None:
            if _candidate_intent(existing) != _candidate_intent(candidate):
                raise TeamMemorySourceExtractionConflict(
                    "team Source extraction candidate identity conflict"
                )
            saved = existing
            replayed = True
        else:
            try:
                saved = self._candidates.save(candidate)
            except MemoryCandidateRepositoryError as error:
                raise TeamMemorySourceExtractionConflict(
                    "team Source extraction candidate write conflict"
                ) from error
            replayed = False
        return TeamMemorySourceExtractionResult(
            candidate_id=candidate_id,
            item_id=item.item_id,
            status=str(saved["status"]),
            target_layer=str(saved["target_layer"]),
            replayed=replayed,
            candidate=dict(saved),
        )

    def _verified_source(
        self, staging_id: str
    ) -> tuple[str, Mapping[str, object], Mapping[str, object], int, str, str, str]:
        clean_staging_id = _identifier(staging_id, "staging_id")
        record = self._staging.get(clean_staging_id)
        if record is None:
            raise TeamMemorySourceExtractionError("team source staging was not found")
        if record.get("status") != "completed":
            raise TeamMemorySourceExtractionConflict(
                "team Source extraction requires completed staging"
            )
        receipt = _mapping(record, "receipt")
        proposal = _mapping(record, "proposed_source")
        source_id = _identifier(proposal.get("id"), "source_id")
        source = self._sources.get(source_id)
        if source is None:
            raise TeamMemorySourceExtractionConflict("completed team Source is missing")
        source_revision = self._sources.revision(source_id)
        expected_revision = _positive_int(receipt.get("source_revision"), "source_revision")
        source_hash = _sha256(source.get("content_hash"), "source content_hash")
        expected_hash = _sha256(proposal.get("content_hash"), "proposed content_hash")
        source_uri = str(source.get("storage_uri") or "")
        if (
            source_revision != expected_revision
            or source_hash != expected_hash
            or source_uri != receipt.get("source_uri")
            or source_uri != proposal.get("storage_uri")
        ):
            raise TeamMemorySourceExtractionConflict(
                "completed team Source authority drifted"
            )
        metadata = _mapping(source, "metadata")
        content = _content(metadata.get("content"))
        encoded = content.encode("utf-8")
        if (
            hashlib.sha256(encoded).hexdigest() != source_hash
            or len(encoded) != source.get("size_bytes")
        ):
            raise TeamMemorySourceExtractionConflict(
                "completed team Source content drifted"
            )
        return (
            clean_staging_id,
            record,
            source,
            source_revision,
            source_hash,
            source_uri,
            content,
        )


def _paragraphs(content: str) -> tuple[_Paragraph, ...]:
    matches = list(re.finditer(r"\S(?:.*?\S)?(?=\r?\n[ \t]*\r?\n|\Z)", content, re.DOTALL))
    if not matches:
        stripped = content.strip()
        start = content.find(stripped)
        matches = [re.match(r"[\s\S]+", content[start : start + len(stripped)])] if stripped else []
        if matches and matches[0] is not None:
            match = matches[0]
            return (_Paragraph("p001", stripped, start, start + len(stripped)),)
    result: list[_Paragraph] = []
    for match in matches:
        text = match.group(0)
        leading = len(text) - len(text.lstrip())
        trailing = len(text) - len(text.rstrip())
        start = match.start() + leading
        end = match.end() - trailing
        clean = content[start:end]
        if clean:
            result.append(_Paragraph(f"p{len(result) + 1:03d}", clean, start, end))
    return tuple(result)


def _atom_items(
    paragraphs: tuple[_Paragraph, ...],
    *,
    staging_id: str,
    source_id: str,
    source_revision: int,
    source_hash: str,
    source_uri: str,
) -> tuple[TeamMemorySourceExtractionItem, ...]:
    items: list[TeamMemorySourceExtractionItem] = []
    for paragraph in paragraphs:
        lines = paragraph.text.splitlines()
        if lines and all(_is_table_line(line) for line in lines if line.strip()):
            continue
        clean = " ".join(line.strip() for line in lines if line.strip())
        clean = _BULLET.sub("", clean, count=1).strip()
        if not clean or _HEADING.fullmatch(clean) or len(clean) > 1200:
            continue
        candidate_type = _atom_type(clean)
        tags = _tags(clean)
        item_id = _stable_id(
            "team-source-extraction-item",
            staging_id,
            source_id,
            str(source_revision),
            source_hash,
            "atom",
            paragraph.paragraph_id,
            hashlib.sha256(clean.encode("utf-8")).hexdigest(),
        )
        items.append(
            TeamMemorySourceExtractionItem(
                item_id=item_id,
                target_layer="atom",
                candidate_type=candidate_type,
                title=f"L1 Atom · {paragraph.paragraph_id}",
                proposed_content=clean,
                source_locator=(
                    f"{source_uri}#char={paragraph.start_char}-{paragraph.end_char}"
                ),
                source_quote=paragraph.text,
                start_char=paragraph.start_char,
                end_char=paragraph.end_char,
                paragraph_ids=(paragraph.paragraph_id,),
                tags=tags,
            )
        )
        if len(items) >= _MAX_ATOM_ITEMS:
            break
    return tuple(items)


def _scenario_items(
    content: str,
    paragraphs: tuple[_Paragraph, ...],
    *,
    staging_id: str,
    source_id: str,
    source_revision: int,
    source_hash: str,
    source_uri: str,
) -> tuple[TeamMemorySourceExtractionItem, ...]:
    if not paragraphs:
        return ()
    sections: list[tuple[str, list[_Paragraph]]] = []
    current_title = "完整资料"
    current: list[_Paragraph] = []
    for paragraph in paragraphs:
        heading = _heading_title(paragraph.text)
        if heading is not None:
            if current:
                sections.append((current_title, current))
            current_title = heading
            current = [paragraph]
        else:
            current.append(paragraph)
    if current:
        sections.append((current_title, current))
    if len(sections) > 1 and sections[0][0] == "完整资料" and len(sections[0][1]) == 1:
        first = sections[0][1][0]
        if _heading_title(first.text) is None and len(first.text) <= 120:
            sections[1] = (sections[1][0], [first, *sections[1][1]])
            sections.pop(0)
    result: list[TeamMemorySourceExtractionItem] = []
    for index, (title, section) in enumerate(sections, start=1):
        start = section[0].start_char
        end = section[-1].end_char
        quote = content[start:end]
        paragraph_ids = tuple(item.paragraph_id for item in section)
        tags = _tags(quote)
        structured = _structured_scenario(title, section, tags)
        item_id = _stable_id(
            "team-source-extraction-item",
            staging_id,
            source_id,
            str(source_revision),
            source_hash,
            "scenario",
            str(index),
            str(start),
            str(end),
            hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        )
        result.append(
            TeamMemorySourceExtractionItem(
                item_id=item_id,
                target_layer="scenario",
                candidate_type="document_takeaway",
                title=f"L2 Scenario · {title}",
                proposed_content=structured,
                source_locator=f"{source_uri}#char={start}-{end}",
                source_quote=quote,
                start_char=start,
                end_char=end,
                paragraph_ids=paragraph_ids,
                tags=tags,
            )
        )
        if len(result) >= _MAX_SCENARIO_ITEMS:
            break
    return tuple(result)


def _structured_scenario(
    title: str, paragraphs: list[_Paragraph], tags: tuple[str, ...]
) -> str:
    body_parts: list[str] = []
    for paragraph in paragraphs:
        lines = paragraph.text.splitlines()
        if lines and _heading_title(paragraph.text) is not None:
            continue
        if lines and any(_is_table_line(line) for line in lines):
            kind = "表格"
        elif lines and all(_BULLET.match(line) for line in lines if line.strip()):
            kind = "要点"
        elif any(line.strip().lower().startswith(("q:", "问：", "问题：")) for line in lines):
            kind = "问答"
        else:
            kind = "段落"
        body_parts.append(f"### {kind} {paragraph.paragraph_id}\n{paragraph.text}")
    tag_text = "、".join(tags) if tags else "未分类"
    body = "\n\n".join(body_parts) if body_parts else paragraphs[0].text
    return f"# {title}\n\n标签：{tag_text}\n\n{body}"


def _heading_title(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = _HEADING.fullmatch(lines[0])
    return match.group(2).strip() if match else None


def _is_table_line(line: str) -> bool:
    clean = line.strip()
    return bool(clean and ("|" in clean or _TABLE_SEPARATOR.fullmatch(clean)))


def _atom_type(text: str) -> str:
    if any(marker in text for marker in _DECISION_MARKERS):
        return "answer_decision"
    if any(marker in text for marker in _ACTION_MARKERS):
        return "answer_action"
    return "answer_fact"


def _tags(text: str) -> tuple[str, ...]:
    lower = text.lower()
    values = [
        tag
        for tag, markers in _TAG_RULES
        if any(marker.lower() in lower for marker in markers)
    ]
    return tuple(values[:6] or ["知识"])


def _candidate_intent(candidate: Mapping[str, object]) -> tuple[object, ...]:
    provenance = _mapping(candidate, "provenance")
    extraction = _mapping(candidate, "extraction")
    return (
        candidate.get("project_id"),
        candidate.get("target_layer"),
        candidate.get("candidate_type"),
        candidate.get("proposed_content"),
        tuple(
            sorted(
                (
                    str(item.get("source_id")),
                    str(item.get("locator")),
                    str(item.get("quote")),
                )
                for item in candidate.get("source_refs", ())
                if isinstance(item, Mapping)
            )
        ),
        provenance.get("source_id"),
        provenance.get("source_revision"),
        provenance.get("source_content_sha256"),
        extraction.get("preview_id"),
        extraction.get("item_id"),
    )


def _mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise TeamMemorySourceExtractionError(f"{field} is invalid")
    return nested


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise TeamMemorySourceExtractionError(f"{field} is invalid")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TeamMemorySourceExtractionError(f"{field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TeamMemorySourceExtractionError(f"{field} is invalid")
    return value


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceExtractionError("team Source content is unavailable")
    return value


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamMemorySourceExtractionError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TeamMemorySourceExtractionError(f"{field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TeamMemorySourceExtractionError(f"{field} is invalid")
    return value


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"
