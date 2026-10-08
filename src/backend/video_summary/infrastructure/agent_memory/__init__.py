from __future__ import annotations

from importlib import import_module

__all__ = [
    "AgentWorkspaceIndexBuilder",
    "BGEReranker",
    "MetaStateReader",
    "SeriesRetrievalService",
    "VideoGraphPinpointService",
    "VideoWorkflowExtractor",
]

_EXPORT_MODULES = {
    "AgentWorkspaceIndexBuilder": ".index_builder",
    "BGEReranker": ".pinpoint",
    "MetaStateReader": ".retrieval",
    "SeriesRetrievalService": ".retrieval",
    "VideoGraphPinpointService": ".pinpoint",
    "VideoWorkflowExtractor": ".video_workflow",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
