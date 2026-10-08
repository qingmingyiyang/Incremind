from pathlib import Path

from backend.api.routes.product.repositories import resolve_rebuild_repository_root


def test_resolve_rebuild_repository_root_uses_staged_sidecar_config(tmp_path: Path) -> None:
    route_file = tmp_path / "sidecar" / "backend" / "api" / "routes" / "rebuild.py"
    route_file.parent.mkdir(parents=True)
    route_file.touch()
    config = tmp_path / "sidecar" / "config" / "rebuild.toml.example"
    config.parent.mkdir()
    config.write_text("[storage]\nnamespace_id = 'default'\n", encoding="utf-8")

    assert resolve_rebuild_repository_root(route_file) == tmp_path / "sidecar"
