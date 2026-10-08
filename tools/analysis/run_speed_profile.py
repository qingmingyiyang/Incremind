from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from time import perf_counter
from typing import Callable


@dataclass(frozen=True)
class SpanRecord:
    name: str
    elapsed_seconds: float


class Profiler:
    def __init__(self) -> None:
        self.records: list[SpanRecord] = []

    def record(self, name: str, elapsed_seconds: float) -> None:
        self.records.append(SpanRecord(name=name, elapsed_seconds=max(0.0, elapsed_seconds)))


def _attach_service_profiling(stack: ExitStack, service, profiler: Profiler) -> None:
    _wrap_method(stack, service, "run_turn", "graph_service.run_turn", profiler)
    input_builder = service._input_builder
    context_loader = input_builder.context_loader
    session_store = input_builder.session_store
    _wrap_method(stack, context_loader, "load", "context_loader.load", profiler)
    if session_store is not None:
        _wrap_method(stack, session_store, "get_snapshot", "session_store.get_snapshot", profiler)
        _wrap_method(stack, session_store, "append_turn", "session_store.append_turn", profiler)
    _wrap_method(stack, service._graph, "invoke", "graph.invoke", profiler)


def _wrap_method(stack: ExitStack, target, method_name: str, span_name: str, profiler: Profiler) -> None:
    original = getattr(target, method_name)

    def wrapped(*args, **kwargs):
        started = perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            profiler.record(span_name, perf_counter() - started)

    setattr(target, method_name, wrapped)
    stack.callback(setattr, target, method_name, original)
