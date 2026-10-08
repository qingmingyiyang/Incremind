from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_ordinary_project_surfaces_use_project_work_rules_language() -> None:
    brain = (ROOT / "src/frontend/src/features/rebuild/ProjectBrainDisplay.jsx").read_text(encoding="utf-8")
    panel = (ROOT / "src/frontend/src/features/rebuild/ProjectWorkRulesPanel.jsx").read_text(encoding="utf-8")
    editor = (ROOT / "src/frontend/src/features/rebuild/ProjectSkillOutlineEditor.jsx").read_text(encoding="utf-8")

    assert 'project_skill: "项目工作规则"' in brain
    assert 'title="项目工作规则"' in brain
    assert 'variant="project"' in panel
    assert 'const ordinary = variant === "project"' in editor
    assert "章节标识" in editor
    assert "项目工作规则已被其他操作更新" in editor


def test_single_spotlight_onboarding_explains_each_capability_boundary() -> None:
    onboarding = (ROOT / "src/frontend/src/features/rebuild/FirstRunOnboarding.jsx").read_text(encoding="utf-8")

    for term in ("Project Skill", "Application Skill", "Processing Recipe"):
        assert term in onboarding
    assert "它不是通用 Agent 插件" in onboarding
    assert "普通使用不需要进入开发者模式逐项配置" in onboarding
    assert "打开模型高级设置" not in onboarding
    assert "切换开发者模式" not in onboarding
    assert "data-onboarding-target" in onboarding
    assert "chriptmas-os-onboarding-v2-complete" in onboarding
    assert "first-run-onboarding-spotlight" in onboarding
