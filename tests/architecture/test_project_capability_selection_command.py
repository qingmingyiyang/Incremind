from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_capability_selection_command_cannot_enable_tool_containers() -> None:
    text = (
        ROOT / "src/backend/security/project_capability_selection_command.py"
    ).read_text(encoding="utf-8")
    assert "capability-exclude" in text
    assert "exclude_tool" in text
    assert "ToolSelectionBinding" in text
    for forbidden in (
        "enabled_plugin_ids=", "enabled_mcp_server_ids=", "enabled_sources=",
        ".dispatch(", ".invoke(", "api_key", "request_body",
    ):
        assert forbidden not in text


def test_capability_selection_api_reads_existing_snapshot_without_runtime_build() -> None:
    text = (ROOT / "src/backend/api/routes/ai.py").read_text(encoding="utf-8")
    start = text.index("async def mutate_project_capability(")
    end = text.index(
        "\n\n@router.get(\"/projects/{project_id}/capability-selection-commands",
        start,
    )
    endpoint = text[start:end]
    assert "capability_registry_snapshot" in endpoint
    assert "confirmation.consume_exact" in endpoint
    assert 'action not in {"exclude", "select", "reset_exclusion"}' in endpoint
    assert "get_or_build_ai_runtime" not in endpoint
