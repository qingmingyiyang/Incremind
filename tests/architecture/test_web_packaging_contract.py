from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Keep these two shared web and packaging contracts.

def test_frontend_uses_one_desktop_backend_origin_for_api_and_media() -> None:
    api = (ROOT / "src" / "frontend" / "src" / "features" / "intake" / "intakeApi.js").read_text(
        encoding="utf-8"
    )
    page = (ROOT / "src" / "frontend" / "src" / "features" / "intake" / "IntelligenceApp.jsx").read_text(
        encoding="utf-8"
    )

    assert 'function apiUrl(path)' in api
    assert 'return `${DESKTOP_BACKEND}${path}`' in api
    assert 'const DESKTOP_BACKEND = ""' in api
    assert "fetch(apiUrl(path)" in api
    assert "src={`/api" not in page
    assert "href={`/api" not in page


def test_release_packaging_uses_current_product_identity() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "tools" / "build_release.ps1",
            ROOT / "tools" / "release_packaging.py",
            ROOT / "tools" / "package" / "environment.cpu.yml",
            ROOT / "tools" / "package" / "environment.gpu.yml",
        )
    )

    assert "vsummary" not in sources.lower()
    assert "Chriptmas_Replay-cpu" in sources
    assert "chriptmas-replay-pack-cpu" in sources
