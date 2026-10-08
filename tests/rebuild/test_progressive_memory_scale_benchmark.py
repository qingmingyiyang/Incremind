from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.product_core.progressive_memory_scale_benchmark import (
    ProgressiveMemoryScaleBenchmarkError,
    _summary,
    run_progressive_memory_scale_benchmark,
)
from core.storage_provider import JsonObjectStore


def _benchmark_store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(
        root / ".rebuild-data",
        legacy_root=root / "library",
    )


def test_scale_benchmark_is_content_free_and_uses_temporary_vault(
    tmp_path,
) -> None:
    result = run_progressive_memory_scale_benchmark(
        vault_root=tmp_path / "benchmark-vault",
        label="test",
        item_count=100,
        repetitions=2,
        store_factory=_benchmark_store,
    )

    assert result["profile"]["authority_item_count"] == 100
    assert result["profile"]["repetitions"] == 2
    assert result["outcome"]["projection_status"] == "ready"
    assert result["outcome"]["projection_cache_hit_rate"] == 1.0
    assert result["outcome"]["route_success_rate"] == 1.0
    assert result["outcome"]["deep_item_count"] == 2
    assert result["outcome"]["context_budget_within_limit"] is True
    assert result["metrics"]["projection_build_ms"]["sample_count"] == 2
    assert result["metrics"]["production_generation_read_ms"]["sample_count"] == 2
    assert result["metrics"]["projection_cache_read_ms"]["sample_count"] == 2
    assert result["metrics"]["projection_build_peak_memory_mib"] >= 0
    assert result["privacy"] == {
        "synthetic_data_only": True,
        "temporary_vault_only": True,
        "query_included": False,
        "content_included": False,
        "object_ids_included": False,
        "source_refs_included": False,
        "paths_included": False,
        "provider_called": False,
        "network_used": False,
    }
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "topic0000",
        "确定性合成",
        "source-",
        "section:benchmark",
        str(tmp_path),
    ):
        assert forbidden not in serialized
    assert (tmp_path / "benchmark-vault" / ".rebuild-data").is_dir()
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "core-contracts"
            / "rebuild"
            / "progressive_memory_scale_benchmark.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert Draft202012Validator(schema).is_valid(result)


def test_scale_benchmark_summary_uses_nearest_rank_percentiles() -> None:
    assert _summary([1.0, 2.0, 3.0, 4.0]) == {
        "sample_count": 4,
        "p50": 2.0,
        "p95": 4.0,
        "max": 4.0,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"label": "", "item_count": 10, "repetitions": 1},
        {"label": "bad path", "item_count": 10, "repetitions": 1},
        {"label": "test", "item_count": 0, "repetitions": 1},
        {"label": "test", "item_count": 100_001, "repetitions": 1},
        {"label": "test", "item_count": 10, "repetitions": 0},
        {"label": "test", "item_count": 10, "repetitions": 21},
    ],
)
def test_scale_benchmark_rejects_unsafe_inputs(tmp_path, kwargs) -> None:
    with pytest.raises(ProgressiveMemoryScaleBenchmarkError):
        run_progressive_memory_scale_benchmark(
            vault_root=tmp_path / "benchmark-vault",
            store_factory=_benchmark_store,
            **kwargs,
        )
