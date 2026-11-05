from pathlib import Path
from dataclasses import replace
import json
import os
import sys

from backend.memory_app.runtime_recovery import load_runtime_recovery_operation
from core.storage_provider.vault_operational_recovery import _OperationLease, _write_operation


def main():
    operations, operation_id, ready = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    operation = load_runtime_recovery_operation(operations_root=operations, operation_id=operation_id)
    unrecorded = len(sys.argv) == 5
    if unrecorded:
        operation_id = sys.argv[4]
        operation = replace(operation, operation_id=operation_id, state='preparing', intent='none',
            staging_root=str(operations.parent / 'restored' / operation_id),
            rollback_root=str(operations.parent / 'rollback' / operation_id),
            target_entries=(), original_entries=(), last_error=None, activation_state='pending', server_identity=None)
    with _OperationLease(operation, operations, lock_root=Path(operation.active_root)):
        if not unrecorded:
            _write_operation(operations, operation)
        ready.write_text(json.dumps({'pid': os.getpid(), 'parent_pid': os.getppid()}), encoding='utf8')
        sys.stdin.buffer.read(1)


if __name__ == '__main__':
    main()
