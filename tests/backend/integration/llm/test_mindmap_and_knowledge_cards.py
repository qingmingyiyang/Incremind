from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


from backend.api.responses import VideoKnowledgeCardsResponse
from backend.video_summary.infrastructure.filesystem_video_workspace import FileSystemVideoWorkspace
from backend.video_summary.infrastructure.litellm_mindmap_generator import build_mindmap_prompt
from backend.video_summary.library.models import KnowledgeCardDTO, VideoKnowledgeCardsDTO


class MindmapPromptTests(unittest.TestCase):
    def test_prompt_allows_depth_to_follow_content_complexity(self) -> None:
        prompt = build_mindmap_prompt(
            title="测试视频",
            duration_seconds=300.0,
            summary_data={
                "title": "测试视频",
                "chapters": [
                    {
                        "id": "chapter-1",
                        "title": "章节一",
                        "summary": "章节摘要",
                        "key_points": ["要点一"],
                        "start_seconds": 0.0,
                        "end_seconds": 120.0,
                    }
                ],
            },
        )

        self.assertIn("层级深度由内容复杂度决定", prompt)
        self.assertNotIn("二三级节点用于展开要点", prompt)




class KnowledgeCardWorkspaceCompatibilityTests(unittest.TestCase):
    def test_workspace_accepts_cards_without_source_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            series_dir = root_dir / "videos" / "series-1"
            series_dir.mkdir(parents=True)
            (series_dir / "video-1.mp4").write_bytes(b"")

            output_dir = root_dir / "workspace" / "series-1" / "video-1"
            output_dir.mkdir(parents=True)
            (output_dir / "knowledge_cards.json").write_text(
                json.dumps(
                    {
                        "title": "测试视频",
                        "cards": [
                            {
                                "id": "kc-1",
                                "title": "反常识协作",
                                "kind": "insight",
                                "summary": "协作的关键不是数量，而是清晰分工。",
                                "details": "当多个 Agent 没有边界时，只会放大噪音。",
                                "tags": ["多 Agent", "协作"],
                                "keywords": ["协作", "分工"],
                                "related_card_ids": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            workspace = FileSystemVideoWorkspace(root_dir)

            cards = workspace.get_video_knowledge_cards("series-1", "video-1")

            self.assertIsNotNone(cards)
            self.assertEqual(cards.cards[0].kind, "insight")
            self.assertFalse(hasattr(cards.cards[0], "source_refs"))


class KnowledgeCardResponseTests(unittest.TestCase):
    def test_api_response_omits_source_refs(self) -> None:
        response = VideoKnowledgeCardsResponse.from_model(
            VideoKnowledgeCardsDTO(
                series_id="series-1",
                video_id="video-1",
                title="测试视频",
                cards=[
                    KnowledgeCardDTO(
                        id="kc-1",
                        title="多 Agent 协作",
                        kind="concept",
                        summary="多个智能体围绕共享目标协作。",
                        details="它要求目标一致、边界清晰、协调顺畅，否则数量只会放大噪音。",
                        tags=["多 Agent"],
                        keywords=["协作"],
                        related_card_ids=[],
                    )
                ],
            )
        )

        self.assertNotIn("source_refs", response.model_dump()["cards"][0])


if __name__ == "__main__":
    unittest.main()
