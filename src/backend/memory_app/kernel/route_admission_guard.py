from contextlib import contextmanager
from copy import deepcopy

from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import validate_bundle
from ..route_correction_scopes import correction_parent_id, planned_child_scope
from ..transaction_records import TransactionRecords
from ..workbench_identity_binding import KIND


def correction_admission_guard(records, request, identity, bundle):
    parent = identity.get('parent') if isinstance(identity, dict) else None
    parent_id = parent.get('id') if isinstance(parent, dict) else None
    if not correction_parent_id(parent_id):
        if isinstance(identity, dict) and 'planned_scope' in identity:
            raise RecognitionConflict('route_correction_native_unverifiable')
        return None
    frozen_request, frozen_identity, frozen_bundle = deepcopy((request, identity, bundle))

    @contextmanager
    def guard():
        # 真实接纳线程与关闭旁路持有同一用户事务锁，不能只在异步准备阶段检查。
        if request != frozen_request or identity != frozen_identity or bundle != frozen_bundle:
            raise RecognitionConflict('route_correction_native_unverifiable')
        validate_bundle(request, bundle)
        entries = [item for item in bundle.items if item.kind == KIND]
        if (len(entries) != 1 or entries[0].payload != {**identity, 'turn_id': request['turn_id']}
                or request['desired_outcome'] != 'project.answer' or identity.get('origin_kind') != 'personal'):
            raise RecognitionConflict('route_correction_native_unverifiable')
        with records.begin() as tx:
            current = TransactionRecords(tx)
            proof = planned_child_scope(records, parent_id, request['turn_id'], reader=current)
            operation = tx.read('v2_route_correction_operations', proof['operation_id'])
            if (operation.payload['source_origin']['kind'] != 'personal'
                    or identity['planned_scope'] != proof or identity['target_turn_id'] != request['turn_id']
                    or identity['public_turn_id'] != parent_id or identity['part'] != proof['part_index']
                    or proof['target'] != {'project_id': request['scope']['project_id'], 'scene': identity['scene'],
                        'namespace': 'personal/' + identity['owner_user_id'] + '/' + identity['namespace_id'],
                        'thread_id': identity['thread_id']}):
                raise RecognitionConflict('route_correction_native_unverifiable')
            yield
            tx.commit()

    return guard
