from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_direct_edit_and_imported_proposal_use_distinct_audit_transitions() -> None:
    routes = _read("src/backend/api/routes/product/project_skills.py")
    external = _read("src/backend/api/external_project_skill_apply_saga.py")
    publication = _read("src/core/project_skill_core/publication_composite_uow.py")

    outline = routes[
        routes.index("async def project_skill_outline_update") :
        routes.index('@router.post("/api/rebuild/projects/{project_id}/skill/rollback")')
    ]
    rollback = routes[
        routes.index("async def project_skill_direct_rollback") :
        len(routes)
    ]
    assert 'transition_kind="user_edit"' in outline
    assert 'confirmation_kind="direct_user_save"' in outline
    assert "skills.rollback(" in rollback
    assert 'transition_kind="external_proposal_apply"' in external
    assert 'transition_kind="ai_publication"' in publication
    assert 'transition_kind="ai_publication_rollback"' in publication


def test_project_skill_revision_contract_is_append_only_and_has_no_frontend_dependency() -> None:
    runtime = _read("src/core/project_skill_core/runtime.py")
    sqlite = _read("src/core/project_skill_core/sqlite_runtime.py")

    assert '"project_skill_revisions"' in runtime
    assert 'transition_kind="user_rollback"' in runtime
    assert "target revision must precede current revision" in runtime
    assert "with self.records.begin() as uow:" in sqlite
    for forbidden in ("frontend", "DeveloperStudio", "ProcessingRecipe", "model_route"):
        assert forbidden not in runtime
