"""Back up a server/user or restore its Settings-compatible snapshot to a new root."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backend.memory_app.backup import backup_runtime, restore_runtime, verify_runtime_backup, verification_metadata
from core.storage_provider.vault_backup_restore import VaultBackupRestoreError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=os.environ.get('CHRIPTMAS_APP_ROOT'))
    parser.add_argument('--user')
    parser.add_argument('--output', type=Path, help='backup parent directory outside source')
    parser.add_argument('--restore', type=Path, help='existing snapshot directory')
    parser.add_argument('--verify', type=Path, help='restore-check an existing snapshot in a temporary root')
    parser.add_argument('--to', type=Path, help='new restore directory; existing targets rejected')
    args = parser.parse_args(argv)
    if args.verify is not None:
        if args.restore is not None or args.to is not None or args.user is not None or args.output is not None:
            parser.error('--verify cannot use --restore, --to, --user or --output')
    elif args.restore is not None:
        if args.to is None or args.user is not None or args.output is not None:
            parser.error('--restore requires --to and cannot use --user or --output')
    elif args.root is None or args.output is None or args.to is not None:
        parser.error('backup requires --root (or CHRIPTMAS_APP_ROOT) and --output')
    try:
        if args.verify is not None:
            value = verify_runtime_backup(args.verify)
        elif args.restore is not None:
            result = restore_runtime(args.restore, args.to)
            value = {'restored': str(result.target_root), 'files': result.file_count}
        else:
            result = backup_runtime(args.root, args.output, user=args.user)
            value = {'snapshot': str(result.snapshot_root), 'files': result.file_count,
                'verified': verification_metadata(result.snapshot_root)['verified']}
    except (ValueError, OSError, VaultBackupRestoreError):
        parser.exit(1, 'backup_or_restore_failed\n')
    print(json.dumps(value))
    if args.verify is not None and value['verified'] is not True:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
