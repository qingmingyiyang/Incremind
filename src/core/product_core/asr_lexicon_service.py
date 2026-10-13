"""复用实时 ASR 词表规则的事务服务，供产品与兼容适配器共同调用。"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from pathlib import Path

from core.product_core.realtime_asr_lexicon import (
    MAX_SESSION_TERMS, MAX_SUPER_TERMS, SUPER_WEIGHT, RealtimeAsrLexicon, RealtimeAsrLexiconError,
)
from core.storage_provider import JsonObjectStore
from core.storage_provider.sqlite_uow import begin_read

MAX_IMPORT_BYTES = 256 * 1024
_COLLECTIONS = ("realtime_asr_lexicon_state", "realtime_asr_lexicon_terms", "realtime_asr_lexicon_candidates")
_STATE = _COLLECTIONS[0]


class AsrLexiconError(ValueError):
    def __init__(self, code="invalid_asr_lexicon_import"):
        self.code = code
        super().__init__(code)


class _Objects:
    """让原领域服务参加调用方事务，不新增数据库或第二套词表规则。"""
    def __init__(self, tx, legacy=None):
        self.tx, self.legacy = tx, legacy

    def read(self, collection, identity):
        row = self.tx.read(collection, identity)
        return dict(row.payload) if row is not None else self.legacy.read(collection, identity) if self.legacy else None

    def list(self, collection):
        if self.legacy is not None:
            return self.legacy.list(collection)
        return [dict(row.payload) for row in self.tx.list(collection)]

    def write(self, collection, identity, payload, expected_revision):
        if self.legacy is not None:
            raise AsrLexiconError("asr_lexicon_legacy_read_only")
        previous = self.tx.read(collection, identity)
        return self.tx.put(collection, identity, payload,
                           expected_revision=previous.revision if previous else 0).revision


def _term_public(term):
    return {"id": term.term_id, "term": term.term, "weight": term.weight, "is_super": term.is_super,
            "status": term.status, "origin": term.origin}


class AsrLexiconService:
    def __init__(self, records, *, runtime_root=None, legacy_store=None):
        self.records = records
        self.legacy = legacy_store if legacy_store is not None else (
            JsonObjectStore(Path(runtime_root) / ".rebuild-data", namespace_id="default") if runtime_root is not None else None)

    def _domain(self, tx):
        legacy = self.legacy if tx.read(_STATE, "default") is None else None
        return RealtimeAsrLexicon(_Objects(tx, legacy))

    def _migrate(self, tx):
        if tx.read(_STATE, "default") is not None:
            return
        if self.legacy is not None:
            # 原 JSON 保留为历史副本；新旧入口此后只使用同一 SQLite 权威。
            for collection in _COLLECTIONS:
                for payload in self.legacy.list(collection):
                    tx.put(collection, payload["id"], payload, expected_revision=0)
        if tx.read(_STATE, "default") is None:
            tx.put(_STATE, "default", {"schema_version": "1.0.0", "id": "default", "revision": 0}, expected_revision=0)

    def read(self):
        with begin_read(self.records) as tx:
            domain = self._domain(tx)
            return {"revision": domain.current_revision(), "terms": [_term_public(row) for row in domain.list_terms()],
                    "session_limit": MAX_SESSION_TERMS, "super_limit": MAX_SUPER_TERMS,
                    "import_max_bytes": MAX_IMPORT_BYTES,
                    "providers": {"local-faster-whisper": {"hotwords_supported": True, "weights": "priority_only"},
                                  "tokenhub-asr": {"hotwords_supported": False},
                                  "qwen-realtime-asr": {"hotwords_supported": True, "weights": "native"}}}

    def import_text(self, *, text, format="txt", expected_revision, preview=True):
        entries = _parse(text, format)
        if type(expected_revision) is not int or expected_revision < 0 or type(preview) is not bool:
            raise AsrLexiconError()
        with self.records.begin() as tx:
            domain = self._domain(tx)
            revision = domain.current_revision()
            if revision != expected_revision:
                raise AsrLexiconError("asr_lexicon_revision_conflict")
            existing = {term.term.casefold(): term for term in domain.list_terms()}
            counts = {"added_count": 0, "updated_count": 0, "unchanged_count": 0}
            for term, weight in entries:
                old = existing.get(term.casefold())
                key = "added_count" if old is None else "unchanged_count" if (
                    old.status == "active" and old.term == term and old.weight == weight) else "updated_count"
                counts[key] += 1
            # 用内存适配器运行同一个领域方法，预览与提交的容量/校验完全一致。
            memory = _MemoryObjects(domain._store)
            try:
                proposed = RealtimeAsrLexicon(memory).bulk_upsert(entries)
            except RealtimeAsrLexiconError as error:
                raise AsrLexiconError("asr_lexicon_capacity_exceeded" if "capacity" in str(error) else "invalid_asr_lexicon_import") from error
            if not preview:
                self._migrate(tx)
                domain = self._domain(tx)
                proposed = domain.bulk_upsert(entries)
                revision = domain.current_revision()
                tx.commit()
            return {"revision": revision, "preview": preview, "imported_count": len(entries), **counts,
                    "terms": [_term_public(row) for row in proposed]}

    def _read_domain(self, method, *args, **kwargs):
        with begin_read(self.records) as tx:
            return getattr(self._domain(tx), method)(*args, **kwargs)

    def _write_domain(self, method, *args, **kwargs):
        with self.records.begin() as tx:
            self._migrate(tx)
            result = getattr(self._domain(tx), method)(*args, **kwargs)
            tx.commit()
            return result

    def current_revision(self):
        return self._read_domain("current_revision")

    def list_terms(self):
        return self._read_domain("list_terms")

    def list_candidates(self, **kwargs):
        return self._read_domain("list_candidates", **kwargs)

    def select_for_session(self, **kwargs):
        return self._read_domain("select_for_session", **kwargs)

    def manual_upsert(self, *args, **kwargs):
        return self._write_domain("manual_upsert", *args, **kwargs)

    def retire(self, *args, **kwargs):
        return self._write_domain("retire", *args, **kwargs)

    def propose_candidate(self, *args, **kwargs):
        return self._write_domain("propose_candidate", *args, **kwargs)

    def accept_candidate(self, *args, **kwargs):
        return self._write_domain("accept_candidate", *args, **kwargs)

    def reject_candidate(self, *args, **kwargs):
        return self._write_domain("reject_candidate", *args, **kwargs)


class _MemoryObjects:
    def __init__(self, source):
        self.rows = {collection: {row["id"]: dict(row) for row in source.list(collection)} for collection in _COLLECTIONS}

    def read(self, collection, identity):
        return self.rows[collection].get(identity)

    def list(self, collection):
        return list(self.rows[collection].values())

    def write(self, collection, identity, payload, expected_revision):
        self.rows[collection][identity] = dict(payload)
        return 1


def _parse(text, format):
    if not isinstance(text, str) or not isinstance(format, str) or format not in {"txt", "csv", "json"}:
        raise AsrLexiconError()
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise AsrLexiconError() from None
    if size > MAX_IMPORT_BYTES:
        raise AsrLexiconError("asr_lexicon_import_too_large")
    try:
        if format == "json":
            rows = json.loads(text.lstrip("\ufeff"))
            if not isinstance(rows, list):
                raise ValueError()
        elif format == "csv":
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
            if (not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames))
                    or not set(reader.fieldnames) <= {"term", "weight", "is_super"} or "term" not in reader.fieldnames):
                raise ValueError()
            rows = list(reader)
        else:
            rows = []
            for line in text.lstrip("\ufeff").splitlines():
                if line.strip():
                    parts = line.split("\t")
                    if len(parts) > 2:
                        raise ValueError()
                    rows.append({"term": parts[0], "weight": parts[1] if len(parts) == 2 else 4})
        if not rows:
            raise ValueError()
        normalized = {}
        for row in rows:
            if not isinstance(row, Mapping) or not set(row) <= {"term", "weight", "is_super"} or "term" not in row:
                raise ValueError()
            weight = row.get("weight", 4)
            raw_super = row.get("is_super", False)
            if format != "json":
                weight = int(weight) if weight not in {None, ""} else 4
                if raw_super in {"true", "1"}:
                    raw_super = True
                elif raw_super in {None, "", "false", "0"}:
                    raw_super = False
            if type(raw_super) is not bool or type(weight) is not int or weight not in {1, 2, 3, 4, 5, SUPER_WEIGHT}:
                raise ValueError()
            weight = SUPER_WEIGHT if raw_super else weight
            # 原领域校验词语长度与控制字符，内存写入不产生持久化副作用。
            checked = RealtimeAsrLexicon(_MemoryObjects(_EmptyObjects())).manual_upsert(row["term"], weight=weight, is_super=weight == SUPER_WEIGHT)
            # JSON 转义可产生孤立代理项；在任何 SQLite 写入前按原无效导入契约拒绝。
            checked.term.encode("utf-8")
            key = checked.term.casefold()
            entry = (checked.term, checked.weight)
            if key in normalized and normalized[key][1] != entry[1]:
                raise ValueError()
            normalized.setdefault(key, entry)
            if len(normalized) > MAX_SESSION_TERMS:
                raise AsrLexiconError("asr_lexicon_capacity_exceeded")
        return tuple(normalized.values())
    except AsrLexiconError:
        raise
    except (ValueError, TypeError, csv.Error):
        raise AsrLexiconError() from None


class _EmptyObjects:
    def list(self, collection):
        return ()


