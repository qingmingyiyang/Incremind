"""旧实时入口和上传流程解析同一用户词表权威。"""
from pathlib import Path

from backend.security.audited_records import audit_records
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.product_core.asr_lexicon_service import AsrLexiconService
from core.storage_provider import JsonObjectStore


def user_asr_lexicon(runtime_root):
    root = Path(runtime_root)
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library", namespace_id="default")
    factory = AggregateRepositoryFactory(runtime_root=root, namespace_id="default", json_store=store)
    records, namespace = factory.recognition_record_store_resolution()
    records = audit_records(records, root, namespace)
    return AsrLexiconService(records, runtime_root=root)
