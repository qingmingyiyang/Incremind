"""Issue an installation/recovery pairing URL for the server administrator."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backend.shared.deployment import resolve_deployment
from backend.memory_app.v2.devices import DeviceRegistry, DeviceError


def _origin(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
                or url.path not in {'', '/'} or url.query or url.fragment or url.port == 0):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError('url must be the server HTTP(S) origin') from None
    return value.rstrip('/')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['pair'])
    parser.add_argument('--root', type=Path, default=os.environ.get('CHRIPTMAS_APP_ROOT'))
    parser.add_argument('--url', type=_origin, required=True)
    args = parser.parse_args(argv)
    if args.root is None:
        parser.error('--root (or CHRIPTMAS_APP_ROOT) is required')
    try:
        layout = resolve_deployment(args.root, environment={'CHRIPTMAS_DEPLOY': 'server',
            'CHRIPTMAS_APP_ROOT': str(args.root)})
        pairing = DeviceRegistry(layout.server_root / 'server').issue_pairing(user_id='local-user', actor='install')
    except (DeviceError, ValueError, OSError):
        parser.exit(1, 'server_admin_failed\n')
    # This is the sole deliberate disclosure, to the local installer stdout.
    # Never log this URL or put its fragment credential in an HTTP query.
    print(json.dumps({'url': args.url + '/pair#code=' + pairing['code'], 'expires_at': pairing['expires_at']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
