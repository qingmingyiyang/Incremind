from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path

from backend.providers import ProviderRegistry


ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "work" / "scripts" / "run_four_layer_provider_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("four_layer_provider_smoke", SMOKE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_four_layer_provider_smoke_fake_runs_review_publication_and_rollback(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--output-dir", str(tmp_path / "fake")],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    report = json.loads(Path(output["report"]).read_text(encoding="utf-8"))

    assert report["status"] == "passed"
    assert report["provider"] == "fake"
    assert report["content_read_status"] == "completed"
    assert report["provider_status"] == "provider_candidates_imported"
    assert report["candidate_status_after_provider"] == "pending_review"
    assert report["review_status"] == "promoted"
    assert report["publication_status"] == "published"
    assert report["rollback_status"] == "rolled_back"
    assert report["memory_publication_final_status"] == "rolled_back"
    assert report["published_memory_remaining"] == 0
    assert report["request_payload_persisted"] is False
    assert report["key_material_returned"] is False
    assert report["privacy_boundary"]["api_key_committed"] is False
    assert report["privacy_boundary"]["cookie_committed"] is False
    assert report["privacy_boundary"]["provider_request_payload_committed"] is False
    assert report["privacy_boundary"]["long_term_memory_left_published"] is False
    assert "sk-" not in json.dumps(report, ensure_ascii=False)


def test_four_layer_provider_smoke_real_provider_requires_explicit_network(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--output-dir",
            str(tmp_path / "deepseek-no-network"),
            "--provider",
            "deepseek",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    report = json.loads(Path(output["report"]).read_text(encoding="utf-8"))

    assert output["status"] == "skipped"
    assert report["status"] == "skipped"
    assert report["reason"] == "real provider smoke requires --allow-network"
    assert report["key_material_returned"] is False
    assert report["api_key_committed"] is False
    assert report["cookie_committed"] is False
    assert report["provider_request_payload_committed"] is False
    assert "sk-" not in json.dumps(report, ensure_ascii=False)


def test_four_layer_provider_smoke_real_provider_requires_explicit_egress_confirmation(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--output-dir",
            str(tmp_path / "deepseek-no-egress-confirmation"),
            "--provider",
            "deepseek",
            "--allow-network",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(Path(json.loads(completed.stdout)["report"]).read_text(encoding="utf-8"))

    assert report["status"] == "skipped"
    assert report["reason"] == "real provider smoke requires --confirm-egress"
    assert report["key_material_returned"] is False


def test_four_layer_provider_smoke_uses_provider_registry_for_non_default_deepseek(
    tmp_path: Path,
) -> None:
    fallback = {
        "name": "默认供应商",
        "llm_provider": "openai",
        "base_url": "",
        "api_path": "/chat/completions",
        "model": "",
        "models": [],
        "enabled": True,
    }
    registry = ProviderRegistry(tmp_path)
    registry.create(
        {
            "provider_id": "personal-deepseek",
            "name": "个人 DeepSeek",
            "llm_provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "api_path": "/chat/completions",
            "model": "deepseek-chat",
            "models": ["deepseek-chat"],
            "enabled": True,
        },
        fallback=fallback,
    )
    registry.activate("personal-deepseek", fallback=fallback)
    smoke = _load_smoke_module()

    record = smoke._resolve_deepseek_provider(tmp_path)

    assert record["provider_id"] == "personal-deepseek"
    assert smoke._provider_endpoint_url(record) == "https://api.deepseek.com/chat/completions"
