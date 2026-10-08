"""Run the existing application on loopback, with optional server static UI."""
import argparse
from functools import partial
from pathlib import Path

from backend.shared.deployment import DeploymentLayout, resolve_deployment


_CODE_ROOT = Path(__file__).resolve().parents[3]


def _frontend_root(code_root):
    root = Path(code_root) / 'src' / 'frontend' / 'dist'
    if not (root / 'index.html').is_file():
        raise ValueError('server_frontend_missing')
    return root


def mount_server_frontend(application, code_root):
    dist = _frontend_root(code_root).resolve()
    from backend.api.static_assets import mount_frontend_dist, _resolve_dist_path
    application.state.server_static_paths = frozenset('/' + path.relative_to(dist).as_posix()
        for path in dist.rglob('*') if path.is_file()
        and _resolve_dist_path(dist, path.relative_to(dist).as_posix()) == path.resolve())
    mount_frontend_dist(application, Path(code_root))


def create_application(*, frontend_root=None, port=None):
    layout = resolve_deployment(_CODE_ROOT / 'runtime')
    if layout.mode == 'desktop':
        from backend.api.runtime_root_config import resolve_application_runtime_root
        layout = DeploymentLayout('desktop', resolve_application_runtime_root(_CODE_ROOT / 'runtime'))
    code_root = _CODE_ROOT if frontend_root is None else Path(frontend_root)
    if layout.mode == 'server':
        _frontend_root(code_root)
        from .app import create_server_app
        app = create_server_app(layout=layout, port=8001 if port is None else port)
        mount_server_frontend(app, code_root)
        app.state.server_frontend_installed = True
        return app
    # Validate before importing either existing global application. Both read
    # the shared root resolver and therefore initialize only the selected user.
    from .app import app
    if app.state.recognition_runtime_root != layout.user_root:
        raise ValueError('deployment_runtime_conflict')
    app.state.server_device_auth.configure_port(8001 if port is None else port)
    app.state.deployment = layout
    return app


def _port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be between 1 and 65535')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=_port, default=8001)
    args = parser.parse_args(argv)
    layout = resolve_deployment(_CODE_ROOT / 'runtime')
    import uvicorn
    factory = (partial(create_application, port=args.port) if layout.mode == 'server'
        else 'backend.memory_app.serve:create_application')
    uvicorn.run(factory, factory=True,
                host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
