from __future__ import annotations

from collections.abc import Mapping

from core.search_and_recall import RecallPort, RecallQuery


class MemoryRecallCapability:
    def __init__(self, recalls: RecallPort) -> None:
        self._recalls = recalls

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        scope = request.get("scope") if isinstance(request.get("scope"), Mapping) else {}
        arguments = request.get("arguments") if isinstance(request.get("arguments"), Mapping) else {}
        text = str(arguments.get("query", "")).strip()
        if not text:
            raise ValueError("memory.recall requires query")
        layers = tuple(str(item) for item in arguments.get("layers", ("atom", "scenario", "series_memory", "persona")))
        trust = tuple(str(item) for item in arguments.get("allowed_trust_statuses", ("verified", "published")))
        hits = self._recalls.recall(RecallQuery(text, _optional_text(scope.get("project_id")), layers, trust, int(arguments.get("limit", 12))))
        return {
            "summary": f"recalled {len(hits)} evidence items",
            "payload_ref": None,
            "receipt_ref": None,
            "evidence_refs": [f"crp://default/memory/{hit.object_id}" for hit in hits],
            "result": [
                {"object_id": hit.object_id, "layer": hit.layer, "content": hit.content, "source_refs": list(hit.source_refs), "trust_status": hit.trust_status, "score": hit.score}
                for hit in hits
            ],
        }


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
