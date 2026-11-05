from pathlib import Path
import json
import os
import sys

import uvicorn

root = Path(sys.argv[1]).resolve()
os.environ['CHRIPTMAS_APP_ROOT'] = str(root)

from tests.memory_app.v2.test_consolidation_titles import build_app


environment = build_app(root)
(root / 'identities.json').write_text(json.dumps({'persona': environment.persona}), encoding='utf-8')
uvicorn.run(environment.app, host='127.0.0.1', port=int(sys.argv[2]), lifespan='off', log_level='error')
