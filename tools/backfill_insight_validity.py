"""Explicit-copy validity backfill; no default runtime path and no model access."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from backend.memory_app.v2.insight_validity import backfill
from core.storage_provider import SQLiteStructuredRecordStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True, help='Selected temporary copy SQLite database')
    args = parser.parse_args(argv)
    if not args.database.is_file():
        parser.error('selected database does not exist')
    result = backfill(SQLiteStructuredRecordStore(args.database))
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
