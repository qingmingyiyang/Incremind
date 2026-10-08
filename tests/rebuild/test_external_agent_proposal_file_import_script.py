from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "work" / "scripts" / "import_external_agent_proposals.py"


def test_external_agent_proposal_file_imports_candidates_and_review_drafts(tmp_path) -> None:
    root_dir = tmp_path / "workspace"
    proposal_file = tmp_path / "external-agent-proposals.json"
    report_path = tmp_path / "proposal-import-report.json"
    proposal_file.write_text(
        json.dumps(
            {
                "proposals": [
                    {
                        "proposal_id": "memory-proposal",
                        "proposal_type": "memory_candidate_proposal",
                        "summary": "外部 Agent 建议把灵感沉淀为原子记忆。",
                        "source_refs": [{"locator": "crp://default/exports/memory_layers.json"}],
                        "evidence_refs": [{"locator": "crp://default/exports/tags.json"}],
                        "suggested_changes": {
                            "target_layer": "atom",
                            "candidate_type": "inspiration_atom",
                            "proposed_content": "灵感系统应参与后续项目构思和问答生成。",
                        },
                        "requires_user_review": True,
                    },
                    {
                        "proposal_id": "skill-proposal",
                        "proposal_type": "project_skill_update_proposal",
                        "summary": "外部 Agent 建议补项目 skill 默认阅读要求。",
                        "source_refs": [{"locator": "crp://default/exports/project_skills.json"}],
                        "evidence_refs": [{"locator": "crp://default/exports/series_summaries.json"}],
                        "suggested_changes": {
                            "project_skill_id": "skill-chriptmas-os",
                            "proposed_content": "回答前读取白盒导出、系列摘要和待审 proposal。",
                        },
                        "requires_user_review": True,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(proposal_file),
            "--root-dir",
            str(root_dir),
            "--project-id",
            "chriptmas-os",
            "--report",
            str(report_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    stdout = json.loads(completed.stdout)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    store = JsonObjectStore(root_dir / ".rebuild-data", legacy_root=root_dir / "library")
    assert stdout["status"] == "passed"
    assert stdout["imported_count"] == 2
    assert report["proposal_count"] == 2
    assert report["rejected_count"] == 0
    assert report["boundary"]["provider_called"] is False
    assert report["boundary"]["automatic_memory_publication"] is False
    assert store.read("external_agent_proposals", "memory-proposal") is not None
    assert store.read("external_agent_proposals", "skill-proposal") is not None
    assert len(store.list("memory_candidates")) == 1
    drafts = store.list("external_agent_review_drafts")
    assert len(drafts) == 1
    assert drafts[0]["draft_type"] == "project_skill_update"
    assert store.list("memory_atoms") == ()
    assert store.list("project_skills") == ()
    assert store.list("memory_publications") == ()


def test_external_agent_proposal_file_import_rejects_forbidden_memory_write(tmp_path) -> None:
    root_dir = tmp_path / "workspace"
    proposal_file = tmp_path / "bad-proposal.json"
    report_path = tmp_path / "proposal-import-report.json"
    proposal_file.write_text(
        json.dumps(
            {
                "proposal_id": "bad-proposal",
                "proposal_type": "memory_candidate_proposal",
                "summary": "bad",
                "source_refs": ["source:bad"],
                "evidence_refs": ["source:bad"],
                "suggested_changes": {"proposed_content": "bad"},
                "requires_user_review": True,
                "memory_publications": [{"id": "publication-direct"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(proposal_file),
            "--root-dir",
            str(root_dir),
            "--project-id",
            "chriptmas-os",
            "--report",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    store = JsonObjectStore(root_dir / ".rebuild-data", legacy_root=root_dir / "library")
    assert completed.returncode == 1
    assert report["status"] == "failed"
    assert report["imported_count"] == 0
    assert report["rejected_count"] == 1
    assert report["rejected"][0]["reason"] == "forbidden proposal field: memory_publications"
    assert store.list("external_agent_proposals") == ()
    assert store.list("memory_candidates") == ()
    assert store.list("memory_publications") == ()
