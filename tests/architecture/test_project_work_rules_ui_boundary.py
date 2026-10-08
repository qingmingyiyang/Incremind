from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_project_work_rules_ui_reuses_project_skill_api_without_new_authority() -> None:
    panel = (ROOT / "src/frontend/src/features/rebuild/ProjectWorkRulesPanel.jsx").read_text(encoding="utf-8")
    brain = (ROOT / "src/frontend/src/features/rebuild/ProjectBrainDisplay.jsx").read_text(encoding="utf-8")

    assert 'from "./projectSkillOverviewApi"' in panel
    assert "loadProjectSkill" in panel
    assert "saveProjectSkillOutline" in panel
    assert "rollbackProjectSkill" in panel
    assert "localStorage" not in panel
    assert "ProjectWorkRulesPanel" in brain
    assert "developer-mode" not in panel
    assert "DeveloperStudio" not in panel


def test_project_brain_keeps_project_rules_behind_l3_user_selection() -> None:
    brain = (ROOT / "src/frontend/src/features/rebuild/ProjectBrainDisplay.jsx").read_text(encoding="utf-8")

    entry = brain.index('aria-label="项目工作规则入口"')
    conditional_panel = brain.index("workRulesOpen ? <ProjectWorkRulesPanel")
    selected_dialog = brain.index("{selectedLayer ? (")
    assert selected_dialog < entry < conditional_panel
