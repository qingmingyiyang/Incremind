from __future__ import annotations

import gc
import hashlib
import math
import time
import tracemalloc
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from core.product_core.memory_projection_contract import (
    serialize_memory_retrieval_projection,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)
from core.product_core.object_store_port import RevisionedProductObjectStorePort
from core.product_core.progressive_recall_drilldown import (
    AuthorityEvidenceCandidate,
    EvidenceSourceRef,
    run_progressive_recall_drilldown,
)
from core.product_core.progressive_recall_shadow import (
    route_progressive_memory_r0,
)
BENCHMARK_VERSION = "progressive-memory-scale-v2"
DEFAULT_PROFILES = (
    ("small", 1_000),
    ("medium", 10_000),
    ("large", 100_000),
)
MAX_ITEM_COUNT = 100_000
MAX_REPETITIONS = 20
GENERATED_AT = "2026-07-26T00:00:00+00:00"


class ProgressiveMemoryScaleBenchmarkError(ValueError):
    """Raised when a synthetic scale benchmark request is unsafe."""


def run_progressive_memory_scale_benchmark(
    *,
    vault_root: Path,
    label: str,
    item_count: int,
    repetitions: int = 3,
    store_factory: Callable[[Path], RevisionedProductObjectStorePort],
) -> dict[str, object]:
    clean_label = _label(label)
    count = _positive_int(item_count, "item_count", maximum=MAX_ITEM_COUNT)
    rounds = _positive_int(
        repetitions,
        "repetitions",
        maximum=MAX_REPETITIONS,
    )
    root = Path(vault_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    snapshot, topic = _synthetic_snapshot(count)

    build_samples: list[float] = []
    peak_memory_mib = 0.0
    projection = None
    for _ in range(rounds):
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        projection = snapshot.build(generated_at=GENERATED_AT)
        build_samples.append(_elapsed_ms(started))
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_memory_mib = max(peak_memory_mib, peak_bytes / (1024 * 1024))
    if projection is None:
        raise ProgressiveMemoryScaleBenchmarkError(
            "projection benchmark produced no sample"
        )

    store = store_factory(root)
    repository = ObjectStoreMemoryProjectionRepository(store)
    fingerprint = authority_snapshot_fingerprint(snapshot)
    job_id = f"benchmark-job-{fingerprint[:16]}"
    persistence_started = time.perf_counter()
    repository.begin_rebuild(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        updated_at=GENERATED_AT,
    )
    artifact_id = repository.stage_projection(projection)
    repository.activate_staged(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        artifact_id=artifact_id,
        updated_at=GENERATED_AT,
    )
    generation_token = snapshot.authority_generation_token
    if generation_token is None:
        raise ProgressiveMemoryScaleBenchmarkError(
            "synthetic authority generation token is required"
        )
    repository.bind_generation(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_generation_token=generation_token,
        authority_fingerprint=fingerprint,
    )
    persistence_ms = _elapsed_ms(persistence_started)
    generation_read_samples: list[float] = []
    cache_read_samples: list[float] = []
    route_samples: list[float] = []
    r1_samples: list[float] = []
    cache_hits = 0
    route_successes = 0
    route = None
    read_result = None
    for _ in range(rounds):
        generation_started = time.perf_counter()
        generation_current = repository.load_current_for_generation(
            project_id=snapshot.project_id,
            authority_identity=snapshot.authority_identity,
            authority_generation_token=generation_token,
        )
        generation_read_samples.append(_elapsed_ms(generation_started))
        if generation_current is None:
            raise ProgressiveMemoryScaleBenchmarkError(
                "synthetic generation binding did not resolve"
            )
        generation_fingerprint, generation_result = generation_current
        if (
            generation_fingerprint != fingerprint
            or generation_result.status != "fresh"
        ):
            raise ProgressiveMemoryScaleBenchmarkError(
                "synthetic generation binding is not fresh"
            )
        cache_started = time.perf_counter()
        read_result = repository.load_current(
            project_id=snapshot.project_id,
            authority_identity=snapshot.authority_identity,
            authority_fingerprint=fingerprint,
        )
        cache_read_samples.append(_elapsed_ms(cache_started))
        cache_hits += int(
            read_result.status == "fresh"
            and read_result.fallback_to_authority is False
        )
        route_started = time.perf_counter()
        route = route_progressive_memory_r0(
            read_result=read_result,
            project_id=snapshot.project_id,
            authority_identity=snapshot.authority_identity,
            authority_fingerprint=fingerprint,
            query=topic,
        )
        route_samples.append(_elapsed_ms(route_started))
        route_successes += int(route.status == "routed")
        r1_started = time.perf_counter()
        _read_selected_r1(read_result.projection, route)
        r1_samples.append(_elapsed_ms(r1_started))
    if (
        read_result is None
        or route is None
        or route.status != "routed"
    ):
        raise ProgressiveMemoryScaleBenchmarkError(
            "synthetic R0 route did not resolve"
        )

    reader = _SyntheticDeepReader()
    drilldown_samples: list[float] = []
    deep_result = None
    for _ in range(rounds):
        drilldown_started = time.perf_counter()
        deep_result = run_progressive_recall_drilldown(
            project_id=snapshot.project_id,
            query=f"{topic} 原文出处",
            projection_result=read_result,
            route=route,
            authority_reader=reader,
            consumed_items=1,
            consumed_chars=64,
        )
        drilldown_samples.append(_elapsed_ms(drilldown_started))
    if deep_result is None:
        raise ProgressiveMemoryScaleBenchmarkError(
            "synthetic drilldown produced no sample"
        )

    projection_payload = serialize_memory_retrieval_projection(projection)
    deep_items = deep_result.bundle.get("items")
    return {
        "schema_version": "1.0.0",
        "benchmark_version": BENCHMARK_VERSION,
        "profile": {
            "label": clean_label,
            "authority_item_count": count,
            "series_count": len(snapshot.series_memories),
            "scenario_count": len(snapshot.scenarios),
            "atom_count": len(snapshot.atoms),
            "repetitions": rounds,
        },
        "metrics": {
            "projection_build_ms": _summary(build_samples),
            "projection_persist_ms": _summary([persistence_ms]),
            "production_generation_read_ms": _summary(
                generation_read_samples
            ),
            "projection_cache_read_ms": _summary(cache_read_samples),
            "r0_route_ms": _summary(route_samples),
            "r1_read_ms": _summary(r1_samples),
            "r2_r3_drilldown_ms": _summary(drilldown_samples),
            "projection_build_peak_memory_mib": round(peak_memory_mib, 3),
        },
        "outcome": {
            "projection_status": projection_payload["status"],
            "projection_cache_hit_rate": round(cache_hits / rounds, 6),
            "route_success_rate": round(route_successes / rounds, 6),
            "r0_item_count": len(projection_payload["r0_items"]),
            "r1_item_count": len(projection_payload["r1_items"]),
            "deep_item_count": (
                len(deep_items)
                if isinstance(deep_items, list)
                else 0
            ),
            "context_budget_within_limit": (
                deep_result.bundle.get("budget", {}).get("used_items", 0)
                <= deep_result.bundle.get("budget", {}).get("max_items", 0)
                and deep_result.bundle.get("budget", {}).get("used_chars", 0)
                <= deep_result.bundle.get("budget", {}).get("max_chars", 0)
            ),
        },
        "privacy": {
            "synthetic_data_only": True,
            "temporary_vault_only": True,
            "query_included": False,
            "content_included": False,
            "object_ids_included": False,
            "source_refs_included": False,
            "paths_included": False,
            "provider_called": False,
            "network_used": False,
        },
    }


def _synthetic_snapshot(
    item_count: int,
) -> tuple[MemoryProjectionAuthoritySnapshot, str]:
    series_count = max(1, min(1_000, item_count // 100))
    scenario_count = min(series_count * 8, max(series_count, item_count // 10))
    atom_count = item_count - series_count - scenario_count
    if atom_count < series_count:
        atom_count = series_count
    atom_ids = [f"atom-{index:06d}" for index in range(atom_count)]
    scenario_ids_by_series: list[list[str]] = [
        [] for _ in range(series_count)
    ]
    scenarios: list[Mapping[str, object]] = []
    for index in range(scenario_count):
        series_index = index % series_count
        scenario_id = f"scenario-{index:06d}"
        scenario_ids_by_series[series_index].append(scenario_id)
        atom_start = (series_index * 16 + index) % atom_count
        scenarios.append(
            {
                "id": scenario_id,
                "title": f"主题 {series_index:04d} 场景",
                "summary": "确定性合成场景摘要。",
                "atom_ids": [
                    atom_ids[atom_start],
                    atom_ids[(atom_start + 1) % atom_count],
                ],
                "source_refs": [
                    {
                        "source_id": f"source-{series_index:04d}",
                        "locator": "section:benchmark",
                    }
                ],
                "tags": [f"topic{series_index:04d}", "benchmark"],
                "series_id": f"series-{series_index:04d}",
                "project_id": "benchmark-project",
                "stale": False,
                "revision": 1,
                "trust_status": "user_confirmed",
            }
        )
    series = tuple(
        {
            "id": f"series-memory-{index:04d}",
            "series_id": f"series-{index:04d}",
            "title": f"topic{index:04d}",
            "overview": f"topic{index:04d} 的确定性系列概况。",
            "scenario_ids": scenario_ids_by_series[index],
            "source_refs": [
                {
                    "source_id": f"source-{index:04d}",
                    "locator": "section:benchmark",
                }
            ],
            "project_ids": ["benchmark-project"],
            "stale": False,
            "revision": 1,
            "trust_status": "user_confirmed",
        }
        for index in range(series_count)
    )
    atoms = tuple(
        {
            "id": atom_id,
            "source_id": f"source-{index % series_count:04d}",
            "content": "确定性合成事实。",
            "atom_type": "fact",
            "tags": [f"topic{index % series_count:04d}"],
            "source_refs": [
                {
                    "source_id": f"source-{index % series_count:04d}",
                    "locator": "section:benchmark",
                }
            ],
            "revision": 1,
            "trust_status": "user_confirmed",
        }
        for index, atom_id in enumerate(atom_ids)
    )
    selected = series_count // 2
    return (
        MemoryProjectionAuthoritySnapshot(
            project_id="benchmark-project",
            authority_identity="progressive-memory-authority-v1:benchmark",
            series_memories=series,
            scenarios=tuple(scenarios),
            atoms=atoms,
            project_skills=(),
            authority_generation_token=hashlib.sha256(
                f"benchmark-generation:{item_count}".encode("utf-8")
            ).hexdigest(),
        ),
        f"topic{selected:04d}",
    )


def _read_selected_r1(
    projection: Mapping[str, object] | None,
    route: object,
) -> Mapping[str, object] | None:
    if not isinstance(projection, Mapping):
        return None
    candidates = getattr(route, "candidates", ())
    selected = {
        str(item.series_memory_id)
        for item in candidates
    }
    values = projection.get("r1_items")
    if not isinstance(values, list):
        return None
    return next(
        (
            item
            for item in values
            if isinstance(item, Mapping)
            and str(item.get("series_memory_id")) in selected
        ),
        None,
    )


class _SyntheticDeepReader:
    def read_structured(self, **kwargs: object):
        return (self._candidate("r2_structured_content", kwargs),)

    def read_source_evidence(self, **kwargs: object):
        return (self._candidate("r3_source_evidence", kwargs),)

    @staticmethod
    def _candidate(
        layer: str,
        kwargs: Mapping[str, object],
    ) -> AuthorityEvidenceCandidate:
        allowed = kwargs.get("allowed_source_refs")
        if not isinstance(allowed, tuple) or not allowed:
            raise ProgressiveMemoryScaleBenchmarkError(
                "synthetic deep reader has no authorized source"
            )
        source_id, locator = allowed[0]
        content = (
            "确定性结构化基准资料。"
            if layer == "r2_structured_content"
            else "确定性原始基准资料。"
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return AuthorityEvidenceCandidate(
            layer=layer,
            object_type="synthetic_benchmark",
            object_id=f"{layer}-item",
            revision_identity=f"{layer}:r1",
            content=content,
            content_hash=digest,
            source_refs=(
                EvidenceSourceRef(
                    str(source_id),
                    str(locator),
                    "b" * 64,
                ),
            ),
            relevance_score=1.0,
        )


def _summary(values: Sequence[float]) -> dict[str, object]:
    if not values:
        raise ProgressiveMemoryScaleBenchmarkError(
            "benchmark samples are required"
        )
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) or value < 0 for value in ordered):
        raise ProgressiveMemoryScaleBenchmarkError(
            "benchmark samples must be finite and non-negative"
        )
    return {
        "sample_count": len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "max": round(ordered[-1], 3),
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    index = max(0, math.ceil(len(values) * quantile) - 1)
    return round(values[index], 3)


def _positive_int(value: object, field: str, *, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > maximum
    ):
        raise ProgressiveMemoryScaleBenchmarkError(
            f"{field} must be between 1 and {maximum}"
        )
    return value


def _label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProgressiveMemoryScaleBenchmarkError("label is required")
    label = value.strip()
    if len(label) > 40 or not all(
        character.isalnum() or character in {"-", "_"}
        for character in label
    ):
        raise ProgressiveMemoryScaleBenchmarkError("label is invalid")
    return label


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, (time.perf_counter() - started) * 1000), 3)
