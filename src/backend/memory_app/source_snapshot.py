"""Compare source material identity separately from mutable privacy."""
from collections.abc import Mapping
from backend.recognition.product_draft_dependencies import PRODUCT_DRAFT_FIELDS
from backend.recognition.document_filings import FILED_EDIT_FIELDS
from backend.recognition.external_input_dependencies import FIELDS as EXTERNAL_INPUT_FIELDS
from .source_graph import graph_identity


def _closure_identity(node: Mapping[str, object]) -> tuple[object, ...]:
    dependencies = node.get("dependency_revisions")
    if dependencies is not None and set(dependencies) == {
            'origin_marker_revision', 'source_project_id', 'source_experience_id', 'source_revision', 'source_snapshot'}:
        snapshot = dependencies['source_snapshot']
        frozen_dependencies = (dependencies['origin_marker_revision'], dependencies['source_project_id'],
            dependencies['source_experience_id'], dependencies['source_revision'], tuple(sorted(snapshot['scope'].items())),
            tuple(_closure_identity(parent) for parent in snapshot['nodes']))
    elif dependencies is not None and set(dependencies) in (PRODUCT_DRAFT_FIELDS, EXTERNAL_INPUT_FIELDS):
        identities = tuple(sorted((key, value) for key, value in dependencies.items()
            if key != 'current_source_graph'))
        frozen_dependencies = identities, graph_identity(dependencies['current_source_graph'])
    elif dependencies is not None and set(dependencies) == FILED_EDIT_FIELDS:
        snapshot = dependencies['source_snapshot']
        frozen_dependencies = (tuple(sorted((key, value) for key, value in dependencies.items()
            if key != 'source_snapshot')), tuple(sorted(snapshot['scope'].items())),
            tuple(_closure_identity(parent) for parent in snapshot['nodes']))
    else:
        frozen_dependencies = None if dependencies is None else tuple(sorted(dependencies.items()))
    styles = tuple((tuple(sorted(snapshot["scope"].items())),tuple(_closure_identity(n) for n in snapshot["nodes"]))
                   for snapshot in node.get("research_style_sources",[]))
    return node["type"], node["id"], node["source_revision"], frozen_dependencies, node.get("incarnation"), styles
