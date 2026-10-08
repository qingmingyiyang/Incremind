from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from backend.shared.llm import RequestScopedWireAttemptRecorder
from backend.video_summary.generation.ports import GenerationArtifactStore, MindmapGenerator


class GenerateMindmap:
    def __init__(self, generator: MindmapGenerator, artifact_store: GenerationArtifactStore) -> None:
        self._generator = generator
        self._artifact_store = artifact_store

    async def run(
        self,
        *,
        title: str,
        duration_seconds: float,
        summary_data: dict[str, object],
        output_dir: Path,
    ) -> dict[str, object]:
        recorder = RequestScopedWireAttemptRecorder(
            request_id=f"video-mindmap:{uuid4().hex[:12]}",
            stage="mindmap",
            model_identity=_cache_identity(self._generator),
        )
        try:
            mindmap = await self._generator.generate(
                title=title,
                duration_seconds=duration_seconds,
                summary_data=summary_data,
                wire_attempt_sink=recorder,
            )
            await self._artifact_store.save_mindmap(mindmap=mindmap, output_dir=output_dir)
            await self._artifact_store.save_wire_attempts(records=recorder.records, output_dir=output_dir)
            return mindmap
        except BaseException:
            if recorder.records:
                try:
                    await self._artifact_store.save_wire_attempts(records=recorder.records, output_dir=output_dir)
                except Exception:
                    # Attempt accounting must never change the generation outcome.
                    pass
            raise


def _cache_identity(component: object) -> str:
    explicit_identity = getattr(component, "cache_identity", None)
    if isinstance(explicit_identity, str) and explicit_identity.strip():
        return explicit_identity.strip()
    component_type = type(component)
    return f"{component_type.__module__}.{component_type.__qualname__}"
