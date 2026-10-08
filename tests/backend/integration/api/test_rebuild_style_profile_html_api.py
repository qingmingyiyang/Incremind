from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

from backend.api import ai_runtime
from backend.api.app import create_app
from backend.api.routes.product import source_content as product_source_content
from core.document_engine import ObjectStoreDocumentRepository
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.model_gateway import ModelResult
from core.product_core import (
    ObjectStorePersonaRepository,
    PersonaExtractor,
    ReadSourceTextContent,
)
from core.storage_provider import JsonObjectStore


# ── 测试夹具 ──


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _confirmed_atom(
    *,
    atom_id: str = "atom-style-001",
    project_id: str = "project-alpha",
    language_style: str = "克制、书面、避免命令式",
    format_preferences: tuple[str, ...] = ("Markdown", "短段落"),
    avoidances: tuple[str, ...] = ("不要使用 emoji",),
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": atom_id,
        "layer": "atom",
        "project_id": project_id,
        "content": "已确认的 Atom 内容，用于提取 Persona 风格。",
        "source_refs": [{"source_id": "source-style", "locator": "char:0-80"}],
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    if language_style:
        payload["language_style"] = language_style
    if format_preferences:
        payload["format_preferences"] = list(format_preferences)
    if avoidances:
        payload["avoidances"] = list(avoidances)
    return payload


def _publish_global_persona(store: JsonObjectStore) -> None:
    """通过 PersonaExtractor + Repository 写入并确认一条全局 Persona 记录。"""
    extractor = PersonaExtractor()
    record = extractor.extract(
        scope="global",
        confirmed_entries=[_confirmed_atom()],
    )
    repo = ObjectStorePersonaRepository(store)
    repo.save(record)  # pending draft
    repo.update_confirmation("global", status="confirmed", reason="测试夹具确认 Persona")


def _intake_link_source(client, *, title: str, url: str, monkeypatch) -> str:
    """通过 link-source-intake 接口创建 Source，返回 source_id。"""
    monkeypatch.setattr(
        product_source_content,
        "_fetch_url_text",
        lambda _: (
            "<html><body><h1>统一输出范式测试</h1>"
            "<p>结构化整理需要形成摘要、关键点和统一输出模板。</p>"
            "<p>项目总结需要保留待确认事项和来源引用。</p></body></html>"
        ),
    )
    intake = client.post(
        "/api/rebuild/workbench/link-source-intake",
        json={"title": title, "url": url},
    )
    return intake.json()["source_id"]


def _prepare_source_for_template(client, source_id: str) -> None:
    """完成 Source 的 web-content + structure + series-assignment 三步。"""
    assert client.post(f"/api/rebuild/sources/{source_id}/web-content", json={}).status_code == 200
    assert client.post(f"/api/rebuild/sources/{source_id}/structure-content", json={}).status_code == 200
    assert client.post(
        f"/api/rebuild/sources/{source_id}/series-assignment",
        json={"confirm": True},
    ).status_code == 200


# ── 1. style_prefix 注入：Source 模板生成 ──


def test_source_template_document_injects_style_prefix_from_persona(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    _publish_global_persona(store)
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="统一输出范式测试资料",
            url="https://example.com/style-test",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        response = client.post(
            f"/api/rebuild/sources/{source_id}/template-document",
            json={"template_type": "review"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    # Persona 的风格字段应注入到 template_prompt 前缀
    assert "统一输出范式（用户个人风格，必须遵循）：" in body["template_prompt"]
    assert "克制、书面、避免命令式" in body["template_prompt"]
    assert "不要使用 emoji" in body["template_prompt"]
    # 不泄露敏感字段
    assert "sk-" not in str(body).lower()
    assert "cookie" not in str(body).lower()


def test_source_template_document_without_persona_returns_empty_prefix(tmp_path, monkeypatch) -> None:
    """无 Persona 时 template_prompt 不含风格前缀，但仍正常生成。"""
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="无 Persona 测试",
            url="https://example.com/no-persona",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        response = client.post(
            f"/api/rebuild/sources/{source_id}/template-document",
            json={"template_type": "review"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    # 无 Persona 时不应包含风格前缀
    assert "统一输出范式" not in body["template_prompt"]
    # 但模板生成应正常进行
    assert "不得编造不存在的事实" in body["template_prompt"]


# ── 2. style_prefix 注入：Provider 增强模板生成 ──


class _FakeTemplateProvider:
    provider_name = "deepseek-template-style-test"

    def complete_json(self, *, system_prompt, user_payload):
        assert "source_refs" in user_payload
        assert "api_key" not in str(user_payload).lower()
        assert "cookie" not in str(user_payload).lower()
        # Provider 收到的 system_prompt 应包含 style_prefix
        assert "统一输出范式（用户个人风格，必须遵循）：" in system_prompt
        return {
            "title": "AI 增强复盘（带风格）",
            "markdown": "## 背景\n资料已按统一风格整理。\n\n## 行动\n接入候选审核。",
        }


def test_provider_source_template_document_injects_style_prefix(tmp_path, monkeypatch) -> None:
    class Gateway:
        def invoke(self, request):
            messages = request.parameters["messages"]
            assert "统一输出范式（用户个人风格，必须遵循）：" in messages[0]["content"]
            return ModelResult(
                {
                    "title": "AI 增强复盘（带风格）",
                    "markdown": "## 背景\n资料已按统一风格整理。\n\n## 行动\n接入候选审核。",
                },
                "deepseek-template-test",
                "deepseek-chat",
                {},
            )

    store = _store(tmp_path)
    _publish_global_persona(store)
    monkeypatch.setattr(
        ai_runtime,
        "resolve_model_gateway_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=Gateway(), egress_consented=True),
    )
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="Provider 风格注入测试",
            url="https://example.com/provider-style",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        response = client.post(
            f"/api/rebuild/sources/{source_id}/provider-template-document",
            json={"template_type": "review"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    assert body["provider_enhanced"] is True
    # template_prompt 应包含风格前缀
    assert "统一输出范式（用户个人风格，必须遵循）：" in body["template_prompt"]
    assert "克制、书面、避免命令式" in body["template_prompt"]
    assert "sk-" not in str(body).lower()
    assert "api_key" not in str(body).lower()


# ── 3. style_prefix 注入：Media 输出模板生成 ──


def _setup_media_output(store: JsonObjectStore, *, source_id: str) -> str:
    """写入 media_processing_jobs + media_processing_outputs，返回 output_id。"""
    output_id = f"media-output-summary-{source_id}"
    job_id = f"media-job-summary-{source_id}"
    store.write(
        "media_processing_jobs",
        job_id,
        {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "source_type": "video",
            "required_capability": "transcript_summary",
            "status": "completed",
            "disabled_reason": None,
            "input_refs": [],
            "expected_output_refs": [],
            "adapter_contract": {},
            "error": None,
            "activity_refs": [],
            "output_refs": [f"crp://default/media-processing-outputs/{output_id}.json"],
            "created_at": "2026-07-02T19:20:00+08:00",
            "updated_at": "2026-07-02T19:20:00+08:00",
        },
        expected_revision=None,
    )
    store.write(
        "media_processing_outputs",
        output_id,
        {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source_id,
            "source_type": "video",
            "output_kind": "summary",
            "status": "completed",
            "provider": "local-command-summary",
            "title": "媒体输出风格测试",
            "preview": "总结输出：统一输出范式注入测试。",
            "text": "## 摘要\n统一输出范式注入测试。",
            "markdown": "## 摘要\n统一输出范式注入测试。",
            "summary_data": {
                "title": "媒体输出风格测试",
                "thirty_second_summary": "媒体输出模板应注入风格前缀。",
                "chapters": [{"title": "风格注入", "summary": "媒体输出接入统一风格。"}],
            },
            "metadata": {"memory_publication": "not_started"},
            "memory_publication": "not_started",
            "created_at": "2026-07-02T19:20:00+08:00",
            "ref": f"crp://default/media-processing-outputs/{output_id}.json",
        },
        expected_revision=None,
    )
    return output_id


def test_media_output_template_document_injects_style_prefix(tmp_path) -> None:
    store = _store(tmp_path)
    _publish_global_persona(store)
    source = ObjectStoreSourceRegistrar(store).register(
        SourceSubmission(
            kind="video",
            title="媒体输出风格测试",
            display_name="style-test.mp4",
            media_type="video/mp4",
            size_bytes=4096,
            video_reference="bilibili/BV1style/p1",
            duration_ms=8000,
        )
    )
    output_id = _setup_media_output(store, source_id=str(source["id"]))
    with _client(tmp_path) as client:
        response = client.post(
            f"/api/rebuild/media-processing-outputs/{output_id}/template-document",
            json={"template_type": "project_summary"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "document_created"
    assert body["template_type"] == "project_summary"
    # template_prompt 应包含风格前缀
    assert "统一输出范式（用户个人风格，必须遵循）：" in body["template_prompt"]
    assert "克制、书面、避免命令式" in body["template_prompt"]
    assert "sk-" not in str(body).lower()


# ── 4. GET /documents/{id}/html —— HTML 预览端点 ──


def _create_document_via_source_template(client, source_id: str) -> str:
    """通过 Source template-document 创建一个 Document，返回 document_id。"""
    response = client.post(
        f"/api/rebuild/sources/{source_id}/template-document",
        json={"template_type": "review"},
    )
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def test_get_document_html_returns_html_response(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="HTML 预览测试",
            url="https://example.com/html-preview",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        response = client.get(f"/api/rebuild/documents/{document_id}/html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<!DOCTYPE html>" in body
    assert "cr-doc" in body
    assert "--cr-bg" in body  # Porcelain CSS
    assert "@media (prefers-color-scheme: dark)" in body
    assert "Porcelain Editorial OS" in body  # footer


def test_get_document_html_returns_404_for_missing_document(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/documents/nonexistent-doc/html")

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "document not found"
    assert body["document_id"] == "nonexistent-doc"


def test_get_document_html_renders_markdown_content(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="HTML 内容渲染测试",
            url="https://example.com/html-content",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        response = client.get(f"/api/rebuild/documents/{document_id}/html")

    assert response.status_code == 200
    body = response.text
    # 模板生成的 Markdown 应被渲染为 HTML 元素
    assert "<h1" in body or "<h2" in body  # 标题
    assert "<p" in body  # 段落
    # 不泄露敏感字段
    assert "sk-" not in body.lower()
    assert "cookie" not in body.lower()


# ── 5. POST /documents/{id}/html-export —— HTML 导出端点 ──


def test_post_document_html_export_writes_file_and_returns_json(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="HTML 导出测试",
            url="https://example.com/html-export",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        response = client.post(f"/api/rebuild/documents/{document_id}/html-export")
        body = response.json()
        artifact = client.get(
            f"/api/rebuild/document-deliveries/{body['delivery_id']}/artifacts/html"
        )

    assert response.status_code == 200
    assert body["status"] == "exported"
    assert body["document_id"] == document_id
    assert body["file_name"].endswith(".html")
    assert body["file_name"].startswith(document_id)
    assert body["output_ref"].startswith("crp://default/document-deliveries/")
    assert body["receipt_ref"].startswith("crp://default/document-delivery-receipts/")
    assert "file_path" not in body
    assert artifact.status_code == 200
    file_content = artifact.text
    assert "<!DOCTYPE html>" in file_content
    assert "cr-doc" in file_content
    # 旧 ObjectStore export collection 不再是生产写入权威。
    store = _store(tmp_path)
    record_id = f"doc-html-{document_id}-r{body['revision']}"
    assert store.read("document_html_exports", record_id) is None
    # 不泄露敏感字段
    assert "sk-" not in str(body).lower()
    assert "cookie" not in str(body).lower()


def test_post_document_html_export_returns_404_for_missing_document(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/api/rebuild/documents/nonexistent-doc/html-export")

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "document not found"


def test_post_document_html_export_is_idempotent(tmp_path, monkeypatch) -> None:
    """同一 document 多次导出应覆盖文件，不报错。"""
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="HTML 幂等导出测试",
            url="https://example.com/html-idempotent",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        response1 = client.post(f"/api/rebuild/documents/{document_id}/html-export")
        response2 = client.post(f"/api/rebuild/documents/{document_id}/html-export")
        body2 = response2.json()
        artifact = client.get(
            f"/api/rebuild/document-deliveries/{body2['delivery_id']}/artifacts/html"
        )

    assert response1.status_code == 200
    assert response2.status_code == 200
    body1 = response1.json()
    # 同 revision 复用同一 Delivery identity 与 artifact。
    assert body1["delivery_id"] == body2["delivery_id"]
    assert body1["file_name"] == body2["file_name"]
    assert body2["replayed"] is True
    assert artifact.status_code == 200


def test_document_delivery_freezes_revision_and_serves_verified_artifacts(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="Document Delivery",
            url="https://example.com/document-delivery",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        detail = client.get(f"/api/rebuild/documents/{document_id}").json()

        response = client.post(
            "/api/rebuild/document-deliveries",
            json={
                "document_id": document_id,
                "expected_document_revision": detail["revision"],
                "formats": ["markdown", "html", "docx", "pptx"],
            },
        )
        replay = client.post(
            "/api/rebuild/document-deliveries",
            json={
                "document_id": document_id,
                "expected_document_revision": detail["revision"],
                "formats": ["pptx", "docx", "html", "markdown"],
            },
        )
        body = response.json()
        markdown = client.get(
            f"/api/rebuild/document-deliveries/{body['delivery_id']}/artifacts/markdown"
        )
        html = client.get(
            f"/api/rebuild/document-deliveries/{body['delivery_id']}/artifacts/html"
        )
        docx = client.get(
            f"/api/rebuild/document-deliveries/{body['delivery_id']}/artifacts/docx"
        )
        pptx = client.get(
            f"/api/rebuild/document-deliveries/{body['delivery_id']}/artifacts/pptx"
        )

    assert response.status_code == 200 and body["status"] == "completed"
    assert body["document_revision"] == detail["revision"]
    assert body["style_snapshot"]["profile_id"] == "builtin.porcelain-document"
    assert "file_path" not in str(body)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["delivery_id"] == body["delivery_id"]
    assert markdown.status_code == 200 and "Document Delivery" in markdown.text
    assert markdown.headers["content-disposition"].endswith('.md"')
    assert html.status_code == 200 and "<!DOCTYPE html>" in html.text
    assert html.headers["content-disposition"].endswith('.html"')
    assert docx.status_code == 200 and docx.content.startswith(b"PK")
    assert docx.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert docx.headers["content-disposition"].endswith('.docx"')
    assert pptx.status_code == 200 and pptx.content.startswith(b"PK")
    assert pptx.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert pptx.headers["content-disposition"].endswith('.pptx"')


def test_document_delivery_startup_recovers_prepared_operation(tmp_path, monkeypatch) -> None:
    import core.product_core.document_delivery as delivery_module

    real_publish = delivery_module._publish_exact
    calls = 0

    def crash_after_first_replace(path, data) -> None:
        nonlocal calls
        real_publish(path, data)
        calls += 1
        if calls == 1:
            raise BaseException("simulated process exit")

    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="Recover Delivery",
            url="https://example.com/recover-delivery",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        detail = client.get(f"/api/rebuild/documents/{document_id}").json()

    from backend.api.routes.product.document_delivery_services import _document_delivery_service

    monkeypatch.setattr(delivery_module, "_publish_exact", crash_after_first_replace)
    with pytest.raises(BaseException, match="simulated process exit"):
        _document_delivery_service(SimpleNamespace(root_dir=tmp_path)).create_or_resume(
            document_id=document_id,
            expected_document_revision=detail["revision"],
            formats=["markdown", "html"],
        )

    monkeypatch.setattr(delivery_module, "_publish_exact", real_publish)
    database = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT operation_id FROM effect WHERE kind='document_delivery' AND state='INFLIGHT'"
        ).fetchone()
        assert row is not None
        delivery_id = str(row[0])
        connection.execute(
            "UPDATE effect SET lease_expires_at=0 WHERE operation_id=?", (delivery_id,),
        )
        connection.commit()
    with _client(tmp_path) as restarted:
        effect = restarted.app.state.effect_runtime.log.get(delivery_id)
        assert effect.state.value == "SETTLED_OK"
        artifact = restarted.get(
            f"/api/rebuild/document-deliveries/{delivery_id}/artifacts/html"
        )

    assert artifact.status_code == 200 and "<!DOCTYPE html>" in artifact.text


def test_document_delivery_rejects_stale_revision_and_unsupported_format(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="Stale Delivery",
            url="https://example.com/stale-delivery",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        document_id = _create_document_via_source_template(client, source_id)
        detail = client.get(f"/api/rebuild/documents/{document_id}").json()
        save = client.put(
            f"/api/rebuild/documents/{document_id}",
            json={
                "expected_revision": detail["revision"],
                "title": "Stale Delivery revised",
                "markdown": "# revised",
            },
        )
        assert save.status_code == 200
        stale = client.post(
            "/api/rebuild/document-deliveries",
            json={
                "document_id": document_id,
                "expected_document_revision": detail["revision"],
                "formats": ["markdown", "html"],
            },
        )
        unsupported = client.post(
            "/api/rebuild/document-deliveries",
            json={
                "document_id": document_id,
                "expected_document_revision": save.json()["revision"],
                "formats": ["pdf"],
            },
        )
        unsafe_identity = client.post(
            "/api/rebuild/document-deliveries",
            json={
                "document_id": "document:unsafe",
                "expected_document_revision": 1,
                "formats": ["markdown"],
            },
        )

    assert stale.status_code == 409
    assert stale.json()["detail"] == "document delivery conflict"
    assert unsupported.status_code == 400
    assert unsafe_identity.status_code == 400
    assert "safe repository segment" in unsafe_identity.json()["reason"]
    assert "supported formats" in unsupported.json()["reason"]


# ── 6. 端到端：风格注入 + HTML 预览 + HTML 导出 ──


def test_end_to_end_style_injection_and_html_export(tmp_path, monkeypatch) -> None:
    """端到端：Persona 风格注入 → 生成 Document → HTML 预览 → HTML 导出。"""
    store = _store(tmp_path)
    _publish_global_persona(store)
    with _client(tmp_path) as client:
        source_id = _intake_link_source(
            client,
            title="端到端风格测试",
            url="https://example.com/e2e-style",
            monkeypatch=monkeypatch,
        )
        _prepare_source_for_template(client, source_id)
        # 1. 生成模板 Document（应注入风格）
        template_resp = client.post(
            f"/api/rebuild/sources/{source_id}/template-document",
            json={"template_type": "project_summary"},
        )
        assert template_resp.status_code == 200
        document_id = template_resp.json()["document_id"]
        assert "统一输出范式" in template_resp.json()["template_prompt"]

        # 2. GET HTML 预览
        html_resp = client.get(f"/api/rebuild/documents/{document_id}/html")
        assert html_resp.status_code == 200
        assert "cr-doc" in html_resp.text

        # 3. POST HTML 导出
        export_resp = client.post(f"/api/rebuild/documents/{document_id}/html-export")
        assert export_resp.status_code == 200
        export_body = export_resp.json()
        assert export_body["status"] == "exported"
        assert "file_path" not in export_body

        # 4. 导出的文件内容与预览一致
        exported_html = client.get(
            f"/api/rebuild/document-deliveries/{export_body['delivery_id']}/artifacts/html"
        ).text
        preview_html = html_resp.text
        # 两者都应是完整 HTML 文档
        assert exported_html.count("<!DOCTYPE html>") == 1
        assert preview_html.count("<!DOCTYPE html>") == 1
        # 都应包含 Porcelain 风格
        assert "cr-doc" in exported_html
        assert "cr-doc" in preview_html
