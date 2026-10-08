"""专家层幂等冒烟：目录→绑定→选择→冻结快照→记忆提案 全链路一次性走通。

只使用临时目录与真实领域代码，不触碰真实 Vault/数据库/Provider/网络。
默认在系统临时目录新建隔离 root；传入 ``--root`` 时要求目录不存在或为空。
通过打印各环节产物 ID 并以 exit 0 结束；任何一步失败即非零退出。

用法::

    runtime/python.exe tools/smoke_expert_layer.py
    runtime/python.exe tools/smoke_expert_layer.py --root D:\\tmp\\expert-smoke --keep
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebuild.product_core.expert_binding_snapshot import (  # noqa: E402
    freeze_expert_binding_snapshot,
    verify_snapshot_replay,
)
from rebuild.product_core.expert_catalog import (  # noqa: E402
    ExpertCatalog,
    ExpertProjectBindingStore,
    ExpertSelectionService,
    default_video_research_expert_profile,
)
from rebuild.product_core.expert_memory_proposal import (  # noqa: E402
    submit_expert_memory_proposal,
)
from rebuild.product_core.external_agent_proposal_import import (  # noqa: E402
    ImportExternalAgentProposal,
)
from rebuild.storage_provider import JsonObjectStore  # noqa: E402

CONTEXT_MANIFEST_ID = "smoke-context-manifest-00000001"
MODEL_ROUTE_REVISION = "smoke-model-route-" + "0" * 48
TOOL_REVISIONS = {
    "analyze_source": 1,
    "library_search": 1,
    "document_generate": 1,
}


def _step(index: int, name: str, detail: str) -> None:
    print(f"[{index:02d}] {name}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="隔离运行根目录（默认新建临时目录）")
    parser.add_argument("--keep", action="store_true", help="保留运行目录便于检查")
    parser.add_argument("--project-id", default="smoke-project-a")
    args = parser.parse_args()

    root = args.root or Path(tempfile.mkdtemp(prefix="expert-layer-smoke-"))
    if root.exists() and any(root.iterdir()):
        print(f"REFUSE: --root {root} 已存在且非空，冒烟要求隔离目录")
        return 2
    root.mkdir(parents=True, exist_ok=True)

    try:
        catalog = ExpertCatalog(root)
        bindings = ExpertProjectBindingStore(root)

        created = catalog.create(
            {**default_video_research_expert_profile(), "status": "active"},
            expected_registry_revision=0,
        )
        _step(1, "create expert", f"{created['expert_id']} r{created['revision']}")

        binding = bindings.bind(
            args.project_id,
            created["expert_id"],
            catalog=catalog,
            enabled_expert_revision=created["revision"],
            intent_affinity=["media_analysis", "research"],
            selection_mode="auto",
            reason="冒烟绑定",
            expected_store_revision=0,
        )
        _step(2, "bind project", f"binding r{binding['binding_revision']} mode={binding['selection_mode']}")

        selection = ExpertSelectionService(catalog, bindings).select(
            args.project_id, ["media_analysis"]
        )
        selected = selection.get("selected")
        if not selected:
            print("FAIL: selection missed expert", selection)
            return 1
        _step(3, "select", f"mode={selection['selection_mode']} expert={selected['expert_id']}")

        snapshot = freeze_expert_binding_snapshot(
            selection,
            catalog=catalog,
            bindings=bindings,
            context_manifest_revision=CONTEXT_MANIFEST_ID,
            boundary_revision=1,
            model_route_revision=MODEL_ROUTE_REVISION,
            tool_capability_revisions=dict(TOOL_REVISIONS),
        )
        _step(4, "freeze snapshot", f"{snapshot['snapshot_id']} frozen_at={snapshot['frozen_at']}")

        replay = verify_snapshot_replay(
            snapshot,
            catalog=catalog,
            bindings=bindings,
            context_manifest_revision=CONTEXT_MANIFEST_ID,
            boundary_revision=1,
            model_route_revision=MODEL_ROUTE_REVISION,
            tool_capability_revisions=dict(TOOL_REVISIONS),
        )
        if replay["status"] != "ok":
            print("FAIL: snapshot replay drifted", replay)
            return 1
        _step(5, "verify snapshot replay", "ok")

        store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")
        receipt = {
            "snapshot_id": snapshot["snapshot_id"],
            "expert_id": snapshot["expert_id"],
            "expert_revision": snapshot["expert_revision"],
            "summary": "冒烟：完成一次视频观点与证据对应整理。",
            "stages": [{"stage": "resolve", "status": "completed"}],
            "tool_invocation_refs": ["tool-invocation:smoke-001"],
            "evidence_refs": [{"locator": f"source:smoke-src-1#t=42"}],
            "output_refs": [],
            "decided_at": snapshot["frozen_at"],
        }
        result = submit_expert_memory_proposal(
            ImportExternalAgentProposal(store),
            snapshot=snapshot,
            catalog=catalog,
            bindings=bindings,
            context_manifest_revision=CONTEXT_MANIFEST_ID,
            boundary_revision=1,
            model_route_revision=MODEL_ROUTE_REVISION,
            tool_capability_revisions=dict(TOOL_REVISIONS),
            expert_receipt=receipt,
            suggested_changes={
                "target_layer": "atom",
                "candidate_type": "preference",
                "content": "冒烟结论：字幕优先于 ASR，时间戳逐条对应。",
            },
        )
        if result.status != "pending_review" or not result.memory_candidate_id:
            print("FAIL: proposal not pending_review", result)
            return 1
        _step(6, "memory proposal", f"proposal={result.proposal_id} candidate={result.memory_candidate_id}")
        _step(7, "human review", "(smoke stops here: candidate awaits human review, no auto publication)")

        print(f"SMOKE PASS root={root}")
        return 0
    except Exception as error:  # noqa: BLE001 - 冒烟需要把任何失败转成非零退出
        print(f"FAIL: {error}")
        return 1
    finally:
        if not args.keep and args.root is None:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
