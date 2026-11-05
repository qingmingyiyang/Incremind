import ast
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def test_credentials_helpers_are_a_leaf_and_share_the_single_argon_gate():
    from backend.memory_app.v2 import server_accounts, server_credentials
    source = ROOT / 'src/backend/memory_app/v2/server_credentials.py'
    imports = {node.module for node in ast.walk(ast.parse(source.read_text(encoding='utf-8')))
               if isinstance(node, ast.ImportFrom)}
    assert not {'server_accounts', 'server_users', 'devices'} & imports
    assert server_accounts._HASHER is server_credentials._HASHER
    assert server_accounts._ARGON is server_credentials._ARGON
    assert server_accounts.prepare_credentials is server_credentials.prepare_credentials
    assert server_accounts.store_credentials is server_credentials.store_credentials


def test_devices_and_users_import_without_loading_server_credentials_or_argon():
    code = (
        "import sys,json; sys.path.insert(0,sys.argv[1]); "
        "import backend.memory_app.v2.devices; import backend.memory_app.v2.server_users; "
        "print(json.dumps({name:name in sys.modules for name in "
        "['argon2','backend.memory_app.v2.server_accounts','backend.memory_app.v2.server_credentials']}))"
    )
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'src')],
                            capture_output=True, text=True, check=True)
    assert not any(json.loads(result.stdout).values())


def test_credentials_import_does_not_load_account_user_or_device_owners():
    code = (
        "import sys,json; sys.path.insert(0,sys.argv[1]); "
        "import backend.memory_app.v2.server_credentials; "
        "print(json.dumps({name:name in sys.modules for name in "
        "['backend.memory_app.v2.server_accounts','backend.memory_app.v2.server_users','backend.memory_app.v2.devices']}))"
    )
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'src')],
                            capture_output=True, text=True, check=True)
    assert not any(json.loads(result.stdout).values())
