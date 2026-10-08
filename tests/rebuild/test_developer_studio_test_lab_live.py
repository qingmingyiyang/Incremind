"""Test Lab 端点真实 LLM 集成测试（仅当 CHRIPTMAS_TEST_DEEPSEEK_API_KEY 设置时运行）。

使用用户提供的测试 API key 进行真实调用，验证端到端流程。
默认跳过；通过设置环境变量启用：

    set CHRIPTMAS_TEST_DEEPSEEK_API_KEY=sk-xxxxxx
    python -m pytest tests/rebuild/test_developer_studio_test_lab_live.py -v
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import sys
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from backend.api.app import create_app  # noqa: E402


_LIVE_KEY = os.environ.get("CHRIPTMAS_TEST_DEEPSEEK_API_KEY", "").strip()
pytestmark = pytest.mark.skipif(
    not _LIVE_KEY,
    reason="CHRIPTMAS_TEST_DEEPSEEK_API_KEY 未设置，跳过真实 LLM 调用测试",
)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def test_live_prompt_type_returns_valid_json(tmp_path, monkeypatch) -> None:
    """使用真实 DeepSeek API key 调用 prompt 类型，应返回有效 JSON。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", _LIVE_KEY)
    client = _client(tmp_path)
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json={
            "test_type": "prompt",
            "input": "今天下午 3 点开产品评审会议，讨论 v2 版本功能优先级",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["test_type"] == "prompt"
    assert payload["valid"] is True, f"expected valid JSON, got error: {payload.get('error')}"
    assert isinstance(payload["parsed"], dict)
    assert payload["usage"]["input_tokens"] > 0
    assert payload["usage"]["output_tokens"] > 0
    assert payload["elapsed_ms"] > 0
    assert payload["credential_source"] == "environment"
    assert payload["model"] == "deepseek-chat"
    # 验证原始 content 是 JSON 字符串
    assert isinstance(payload["raw"], str)
    assert payload["raw"].strip().startswith("{")


def test_live_search_type_returns_summary_and_keywords(tmp_path, monkeypatch) -> None:
    """search 类型应返回 summary + keywords 结构。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", _LIVE_KEY)
    client = _client(tmp_path)
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json={
            "test_type": "search",
            "input": "如何区分领导力和管理的区别",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["valid"] is True, f"error: {payload.get('error')}"
    assert "summary" in payload["parsed"]
    assert "keywords" in payload["parsed"]


def test_live_pipeline_type_runs_two_steps(tmp_path, monkeypatch) -> None:
    """pipeline 类型应执行两步 LLM 调用，最终返回 structuring 结果。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", _LIVE_KEY)
    client = _client(tmp_path)
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json={
            "test_type": "pipeline",
            "input": "记一下：明天要交季度报告草稿，重点突出 Q2 用户增长数据",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["test_type"] == "pipeline"
    assert payload["valid"] is True, f"error: {payload.get('error')}"
    assert len(payload["steps"]) == 2
    # 第二步返回 title/summary/tags
    assert "title" in payload["parsed"]
    assert "summary" in payload["parsed"]


def test_live_does_not_leak_api_key_in_response(tmp_path, monkeypatch) -> None:
    """响应中不应包含 API key。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", _LIVE_KEY)
    client = _client(tmp_path)
    response = client.post(
        "/api/rebuild/developer-studio/test-lab",
        json={"test_type": "prompt", "input": "测试"},
    )
    assert response.status_code == 200
    body_text = response.text
    assert _LIVE_KEY not in body_text
    assert "Bearer " not in body_text
