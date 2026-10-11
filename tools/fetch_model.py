"""安装可选的本机文字向量，只委托应用的唯一安装服务。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backend.shared.deployment import resolve_deployment
from backend.memory_app.local_vector_assets import install_embedding
from backend.memory_app.v2.embedding_settings import vector_policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', choices=['embedding'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--deployment', choices=['desktop', 'server'], default='desktop')
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--from', dest='source', type=Path)
    sources.add_argument('--source', dest='download_source', choices=['huggingface'], default='huggingface')
    args = parser.parse_args(argv)
    layout = resolve_deployment(args.root, environment={
        'CHRIPTMAS_DEPLOY': args.deployment, 'CHRIPTMAS_APP_ROOT': str(args.root)})
    # 保留调用方词法路径，让唯一安装服务继续检查 symlink/junction 祖先。
    root = args.root.expanduser().absolute()
    models_root = root / 'models' if layout.mode == 'server' else root / 'data' / 'models'
    install_embedding(models_root, model=vector_policy()['model'], source=args.source,
        progress=lambda value: print(f"{value['done']} / {value['total']}", flush=True))


if __name__ == '__main__':
    main()
