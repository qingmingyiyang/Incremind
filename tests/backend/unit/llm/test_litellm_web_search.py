from __future__ import annotations

import json
import unittest

from tests import _path_setup  # noqa: F401

from backend.video_summary.infrastructure.litellm_web_search import LiteLLMNativeWebSearchGateway
from backend.video_summary.infrastructure.litellm_web_search import VIDEO_WEB_SEARCH_PROMPT_VERSION


class LiteLLMNativeWebSearchGatewayTests(unittest.TestCase):
    def test_search_passes_native_web_search_options_and_extracts_url_citations(self) -> None:
        completion = FakeSearchCompletion()
        gateway = LiteLLMNativeWebSearchGateway(
            provider="openai",
            model="gpt-5-search-api",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            search_context_size="medium",
            completion_fn=completion,
            egress_guard=allow_egress,
        )

        results = gateway.search("查一下 LLMOps 最新情况", max_results=2, timeout_seconds=7)

        self.assertEqual(completion.last_request["web_search_options"], {"search_context_size": "medium"})
        self.assertEqual(completion.last_request["timeout"], 7)
        self.assertEqual(
            [message["role"] for message in completion.last_request["messages"]],
            ["system", "user"],
        )
        payload = json.loads(completion.last_request["messages"][1]["content"])
        self.assertEqual(payload["prompt_version"], VIDEO_WEB_SEARCH_PROMPT_VERSION)
        self.assertEqual(results, [
            {
                "title": "LLMOps Article",
                "url": "https://example.com/llmops",
                "text": "LLMOps 最新情况",
                "snippet": "LLMOps 最新情况",
            }
        ])

    def test_search_fails_when_provider_returns_no_citable_sources(self) -> None:
        gateway = LiteLLMNativeWebSearchGateway(
            provider="openai",
            model="gpt-5-search-api",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            search_context_size="medium",
            completion_fn=FakeNoCitationCompletion(),
            egress_guard=allow_egress,
        )

        with self.assertRaisesRegex(RuntimeError, "未返回可引用来源"):
            gateway.search("查一下", max_results=2, timeout_seconds=7)

    def test_search_refuses_before_provider_call_when_policy_is_missing(self) -> None:
        completion = FakeSearchCompletion()
        gateway = LiteLLMNativeWebSearchGateway(
            provider="openai",
            model="gpt-5-search-api",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            search_context_size="medium",
            completion_fn=completion,
        )

        with self.assertRaisesRegex(RuntimeError, "外发政策未配置"):
            gateway.search("查一下", max_results=2, timeout_seconds=7)

        self.assertEqual(completion.last_request, {})

    def test_search_clamps_timeout_and_rejects_non_positive_result_limit(self) -> None:
        completion = FakeSearchCompletion()
        gateway = LiteLLMNativeWebSearchGateway(
            provider="openai",
            model="gpt-5-search-api",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            search_context_size="medium",
            completion_fn=completion,
            egress_guard=allow_egress,
        )

        gateway.search("查一下", max_results=1, timeout_seconds=999)
        self.assertEqual(completion.last_request["timeout"], 120)
        with self.assertRaisesRegex(ValueError, "max_results"):
            gateway.search("查一下", max_results=0, timeout_seconds=7)


class FakeEgressLease:
    def finish(self, status: str, *, error_code: str | None = None) -> None:
        del status, error_code


def allow_egress(purpose: str, categories: tuple[str, ...], payload_bytes: int) -> FakeEgressLease:
    assert purpose == "web_search"
    assert categories == ("instructions", "source_excerpt")
    assert payload_bytes > 0
    return FakeEgressLease()


class FakeSearchCompletion:
    def __init__(self) -> None:
        self.last_request: dict[str, object] = {}

    def __call__(self, **kwargs):
        self.last_request = dict(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": "LLMOps 最新情况：供应商正在加强评测。",
                        "annotations": [
                            {
                                "url_citation": {
                                    "url": "https://example.com/llmops",
                                    "title": "LLMOps Article",
                                    "start_index": 0,
                                    "end_index": 11,
                                }
                            }
                        ],
                    }
                }
            ]
        }


class FakeNoCitationCompletion:
    def __call__(self, **kwargs):
        del kwargs
        return {
            "choices": [
                {
                    "message": {
                        "content": "没有来源",
                        "annotations": [],
                    }
                }
            ]
        }


if __name__ == "__main__":
    unittest.main()
