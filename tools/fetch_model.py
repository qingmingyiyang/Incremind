"""安装可选的本机文字向量，只委托应用的唯一安装服务。"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backend.memory_app.local_vector_assets import install_embedding
from backend.memory_app.v2.embedding_settings import vector_policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', choices=['embedding'])
    parser.add_argument('--root', type=Path, required=True)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--from', dest='source', type=Path)
    sources.add_argument('--source', dest='download_source', choices=['huggingface'], default='huggingface')
    args = parser.parse_args(argv)
    install_embedding(args.root / 'data' / 'models', model=vector_policy()['model'], source=args.source,
        progress=lambda value: print(f"{value['done']} / {value['total']}", flush=True))


if __name__ == '__main__':
    main()
