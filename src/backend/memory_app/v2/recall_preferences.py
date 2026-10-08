"""Recall state mutation with existing product activity and usage sidecars."""
from backend.recognition import RecognitionConflict, RecognitionError
from datetime import datetime, timezone
from ..recall_state import COLLECTION
from core.document_engine import SQLiteDocumentRepository
from backend.shared.document_visibility import LegacyDocumentVisibility


class DocumentRecallUnavailable(RecognitionError):
    """整理稿不属于当前可见范围。"""


def restore_document_preference(records, documents, project, document_id, *,
                                document_revision, preference_revision):
    """只恢复降权旁路，不修改正文、来源、归档或核对事实。"""
    if (type(document_revision) is not int or document_revision < 1
            or type(preference_revision) is not int or preference_revision < 1):
        raise RecognitionError('document recall revision is invalid')
    if (not isinstance(documents, SQLiteDocumentRepository) or documents.records is not records
            or documents.namespace_id != 'default'):
        raise RecognitionConflict('document recall owner is unavailable')
    with records.begin() as tx:
        document = tx.read('documents', document_id)
        if document is None or document.payload.get('project_id') != project:
            raise DocumentRecallUnavailable('document is unavailable')
        # 原可见性读取使用同一库；写事务在读取期间阻止来源资格被并发改写。
        visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project,
                                                               document_ids={document_id})
        if not visibility.allows(document.payload):
            raise DocumentRecallUnavailable('document is unavailable')
        if (document.payload.get('revision') != document_revision
                or document.payload.get('status') == 'archived'):
            raise RecognitionConflict('document revision changed')
        previous = tx.read('v2_document_recall', document_id)
        if (previous is None or previous.revision != preference_revision
                or previous.payload.get('state') != 'cooled'):
            raise RecognitionConflict('document recall changed')
        updated = tx.put('v2_document_recall', document_id, {
            'state': 'normal', 'by': 'user', 'changed_at': datetime.now(timezone.utc).isoformat(),
        }, expected_revision=preference_revision)
        tx.commit()
    from .usage import safe_record_usage
    safe_record_usage(records, 'document', document_id, project, 1.0, reset=True)
    return {'document_id': document_id, 'recall_state': 'normal',
            'recall_preference_revision': updated.revision}

def set_preference(records, scope, recognition_id, *, recognition_revision, preference_revision, state):
    if not isinstance(state, str) or state not in {"normal", "cooled", "forgotten"}:
        raise RecognitionError("recall state is invalid")
    for revision in (recognition_revision, preference_revision):
        if type(revision) is not int or revision < 0:
            raise RecognitionError("recall revision is invalid")
    with records.begin() as tx:
        current = tx.read("recognitions", recognition_id)
        if (current is None or current.payload.get("scope") != {"user_id": scope.user_id, "project_id": scope.project_id}
                or current.payload.get("state") != "active" or current.revision != recognition_revision):
            raise RecognitionConflict("recognition is unavailable or changed")
        previous = tx.read(COLLECTION, recognition_id)
        updated = tx.put(COLLECTION, recognition_id, {"id": recognition_id, "user_id": scope.user_id,
            "project_id": scope.project_id, "state": state, "by": "user",
            "changed_at": datetime.now(timezone.utc).isoformat()}, expected_revision=preference_revision)
        tx.commit()
    if state == 'forgotten' and (previous is None or previous.payload.get('state') != 'forgotten'):
        from .stats import record_activity
        record_activity(records, 'forget', scope.project_id, recognition_id,
                        event_id='forget-' + recognition_id + '-' + str(updated.revision))
    if state == "normal" and previous is not None and previous.payload.get("state") in {"forgotten", "cooled"}:
        from .usage import safe_record_usage
        safe_record_usage(records, "insight", recognition_id, scope.project_id, 1.0, reset=True)
    return {"recall_state": state, "recall_preference_revision": updated.revision}
