import sqlite3
import sys


def sqlite_version_supported(version: tuple[int, ...]) -> bool:
    # 3.50 回补分支与 3.51.3 后续版本分别判断，防止放行 3.51.0–3.51.2。
    return version >= (3, 51, 3) or version[:2] == (3, 50) and version >= (3, 50, 7)


def main() -> int:
    print(sqlite3.sqlite_version)
    return 0 if sqlite_version_supported(sqlite3.sqlite_version_info) else 1


if __name__ == '__main__':
    sys.exit(main())
