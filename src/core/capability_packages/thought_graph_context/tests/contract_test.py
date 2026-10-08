from core.capability_packages.thought_graph_context import ThoughtDAGImporter


def test_thought_graph_importer_contract_is_available() -> None:
    assert ThoughtDAGImporter is not None
