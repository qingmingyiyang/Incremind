import os
from pathlib import Path
import sys

from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.memory_app.v2.server_models import ServerModels
from backend.memory_app.v2.server_model_balance import ServerModelBalances


root, user_id, attempt_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
devices = DeviceRegistry(root / 'server')
paired = devices.exchange(os.environ.pop('CHRIPTMAS_BALANCE_PAIRING'), name='恢复检查进程')
actor = devices.authenticate(paired['key'])
users = ServerUsers(root, records=devices.records)
models = ServerModels(users)
balances = ServerModelBalances(users, models)
access = models.access_for(user_id, users.root_for(user_id))
balances.reserve(actor=actor, user_id=user_id, user_root=users.root_for(user_id),
    binding=access.selection('generation')['server_model_binding'],
    attempt_id=attempt_id, model='deepseek-flash', max_output_tokens=100)
print('reserved', flush=True)
sys.stdin.readline()
