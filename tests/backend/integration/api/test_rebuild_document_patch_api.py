from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from fastapi.testclient import TestClient

from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.storage_provider import JsonObjectStore


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _create_document(store: JsonObjectStore, *, markdown: str = "AI 草稿正文。") -> dict[str, object]:
    documents = ObjectStoreDocumentRepository(store)
    document = documents.create(
        DocumentDraft(
            title="AI patch 测试文档",
            document_type="summary",
            markdown=markdown,
            source_refs=(
                {
                    "source_id": "source-1",
                    "locator": "char:0-10",
                    "quote": "AI 草稿",
                },
            ),
        )
    )
    return dict(document)


def _user_block(document: Mapping[str, object]) -> dict[str, object]:
    blocks = document.get("blocks")
    assert isinstance(blocks, list) and blocks
    return dict(blocks[0])


def test_document_archive_restore_routes_preserve_history_and_library_visibility(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document = _create_document(store)
    document_id = str(document["id"])

    with _client(tmp_path) as client:
        before = client.get("/api/rebuild/library/overview")
        archived = client.post(
            f"/api/rebuild/documents/{document_id}/archive",
            json={"expected_revision": 1},
        )
        hidden = client.get("/api/rebuild/library/overview")
        archive_list = client.get("/api/rebuild/documents-archived")
        stale = client.post(
            f"/api/rebuild/documents/{document_id}/restore",
            json={"expected_revision": 1},
        )
        restored = client.post(
            f"/api/rebuild/documents/{document_id}/restore",
            json={"expected_revision": 2},
        )
        visible = client.get("/api/rebuild/library/overview")
        revisions = client.get(f"/api/rebuild/documents/{document_id}/revisions")

    assert before.status_code == 200
    assert any(item["item_id"] == document_id for item in before.json()["items"])
    assert archived.status_code == 200
    assert archived.json()["document_status"] == "archived"
    assert archived.json()["revision"] == 2
    assert all(item["item_id"] != document_id for item in hidden.json()["items"])
    assert [item["document_id"] for item in archive_list.json()["items"]] == [document_id]
    assert stale.status_code == 409
    assert restored.status_code == 200
    assert restored.json()["document_status"] == "draft"
    assert restored.json()["revision"] == 3
    assert any(item["item_id"] == document_id for item in visible.json()["items"])
    assert [item["operation"] for item in revisions.json()["revisions"]] == [
        "create",
        "archive",
        "restore",
    ]
    assert ObjectStoreDocumentRepository(store).markdown(document_id, revision=1) == "AI 草稿正文。"
    assert ObjectStoreDocumentRepository(store).markdown(document_id, revision=3) == "AI 草稿正文。"


def test_patch_document_applies_ai_patch_when_no_user_edit(tmp_path: Path) -> None:
    """无用户手改 block 时，AI patch 直接应用，revision +1，status 仍为 draft。"""
    store = _store(tmp_path)
    document = _create_document(store)
    document_id = str(document["id"])
    # 取一个 AI block 来 patch（AI 草稿块不是 user-protected）
    ai_block = dict(document["blocks"][0])
    new_block = {
        "id": ai_block["id"],
        "block_type": ai_block["block_type"],
        "origin": "ai",
        "content": "AI 改进后的正文。",
        "source_refs": [{"source_id": "source-1", "locator": "char:0-12"}],
        "edited_by_user": False,
        "lock_policy": "source_required",
    }

    with _client(tmp_path) as client:
        response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={
                "expected_revision": 1,
                "blocks": [new_block],
                "reason": "AI 改进第一段",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ai_patch_applied"
    assert body["document_status"] == "draft"
    assert body["revision"] == 2
    assert body["conflict"]["status"] == "none"
    assert body["conflict"]["conflict_blocks"] == []
    # markdown 已更新为 AI 改进版本
    assert "AI 改进后的正文。" in body["markdown"]
    # 旧内容已被覆盖（无用户保护）
    assert "AI 草稿正文。" not in body["markdown"]


def test_patch_document_reports_conflict_without_overwriting_user_edit(tmp_path: Path) -> None:
    """用户手改 block 后，AI patch 触碰同一 block 时：保留用户内容，写入 conflicted revision。"""
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = _create_document(store)
    document_id = str(document["id"])
    # 用户手改：save_user_edit 把所有 block 标记为 edited_by_user=True + user_edit_protected
    edited = documents.save_user_edit(
        document_id,
        markdown="用户改写：保留这段人工判断。",
        expected_revision=1,
    )
    user_block = dict(edited["blocks"][0])
    assert user_block["edited_by_user"] is True
    assert user_block["lock_policy"] == "user_edit_protected"
    ai_patch_block = {
        "id": user_block["id"],
        "block_type": user_block["block_type"],
        "origin": "ai",
        "content": "AI 尝试覆盖用户人工判断。",
        "source_refs": [{"source_id": "source-1", "locator": "char:0-20"}],
        "edited_by_user": False,
        "lock_policy": "source_required",
    }

    with _client(tmp_path) as client:
        response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={
                "expected_revision": 2,
                "blocks": [ai_patch_block],
                "reason": "AI 尝试覆盖",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ai_patch_conflict"
    assert body["document_status"] == "conflicted"
    assert body["revision"] == 3
    assert body["conflict"]["status"] == "detected"
    assert body["conflict"]["conflict_blocks"] == [user_block["id"]]
    # 关键：用户内容被保留，AI 内容没有覆盖
    assert "用户改写：保留这段人工判断。" in body["markdown"]
    assert "AI 尝试覆盖用户人工判断。" not in body["markdown"]
    # changed_blocks 记录了 AI 想做的改动（仅作记录，未应用）
    assert isinstance(body["changed_blocks"], list)
    assert len(body["changed_blocks"]) == 1
    assert body["changed_blocks"][0]["operation"] == "update"


def test_patch_document_rejects_missing_expected_revision(tmp_path: Path) -> None:
    """缺少 expected_revision 返回 400。"""
    store = _store(tmp_path)
    document = _create_document(store)
    document_id = str(document["id"])

    with _client(tmp_path) as client:
        response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={"blocks": [{"id": "block-001", "block_type": "paragraph", "content": "x"}]},
        )

    assert response.status_code == 400
    body = response.json()
    assert "expected_revision" in body["reason"]


def test_patch_document_rejects_empty_blocks(tmp_path: Path) -> None:
    """空 blocks 列表返回 400。"""
    store = _store(tmp_path)
    document = _create_document(store)
    document_id = str(document["id"])

    with _client(tmp_path) as client:
        response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={"expected_revision": 1, "blocks": []},
        )

    assert response.status_code == 400
    body = response.json()
    assert "blocks" in body["reason"]


def test_patch_document_returns_409_on_stale_revision(tmp_path: Path) -> None:
    """过期 expected_revision 返回 409。"""
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = _create_document(store)
    document_id = str(document["id"])
    # 用户已保存到 revision 2，AI 用 revision 1 patch 会冲突
    documents.save_user_edit(
        document_id,
        markdown="用户已更新到 revision 2。",
        expected_revision=1,
    )
    ai_block = {
        "id": dict(document["blocks"][0])["id"],
        "block_type": "paragraph",
        "origin": "ai",
        "content": "AI 用旧 revision patch。",
        "source_refs": [],
        "edited_by_user": False,
        "lock_policy": "none",
    }

    with _client(tmp_path) as client:
        response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={"expected_revision": 1, "blocks": [ai_block]},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "document revision conflict"


def test_get_document_revisions_lists_full_history(tmp_path: Path) -> None:
    """GET /revisions 返回完整 revision 历史，含 operation / author / conflict 摘要。"""
    store = _store(tmp_path)
    documents = ObjectStoreDocumentRepository(store)
    document = _create_document(store)
    document_id = str(document["id"])
    # 制造 revision 2（user_edit）和 revision 3（ai_patch conflict）
    documents.save_user_edit(
        document_id,
        markdown="用户改写内容。",
        expected_revision=1,
    )
    user_block = dict(documents.read(document_id)["blocks"][0])
    documents.apply_ai_patch(
        document_id,
        blocks=[
            {
                "id": user_block["id"],
                "block_type": user_block["block_type"],
                "origin": "ai",
                "content": "AI 尝试覆盖。",
                "source_refs": [],
                "edited_by_user": False,
                "lock_policy": "source_required",
            }
        ],
        expected_revision=2,
        reason="测试冲突",
    )

    with _client(tmp_path) as client:
        response = client.get(f"/api/rebuild/documents/{document_id}/revisions")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == document_id
    assert body["current_revision"] == 3
    assert body["document_status"] == "conflicted"
    revisions = body["revisions"]
    assert [item["revision"] for item in revisions] == [1, 2, 3]
    assert revisions[0]["operation"] == "create"
    assert revisions[1]["operation"] == "user_edit"
    assert revisions[1]["author"] == "user"
    assert revisions[2]["operation"] == "ai_patch"
    assert revisions[2]["author"] == "system"
    assert revisions[2]["conflict"]["status"] == "detected"
    assert revisions[2]["conflict"]["conflict_blocks"] == [user_block["id"]]
    assert revisions[2]["reason"] == "测试冲突"


def test_get_document_revisions_returns_404_for_unknown_document(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/rebuild/documents/unknown_document/revisions")

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "document not found"


def test_patch_and_revisions_response_excludes_secrets(tmp_path: Path) -> None:
    """PATCH 与 revisions 响应不含 secret-like 值。"""
    store = _store(tmp_path)
    document = _create_document(store)
    document_id = str(document["id"])
    ai_block = {
        "id": dict(document["blocks"][0])["id"],
        "block_type": "paragraph",
        "origin": "ai",
        "content": "正常 AI 改动。",
        "source_refs": [],
        "edited_by_user": False,
        "lock_policy": "none",
    }

    with _client(tmp_path) as client:
        patch_response = client.patch(
            f"/api/rebuild/documents/{document_id}",
            json={"expected_revision": 1, "blocks": [ai_block]},
        )
        revisions_response = client.get(f"/api/rebuild/documents/{document_id}/revisions")

    assert patch_response.status_code == 200
    assert revisions_response.status_code == 200
    patch_text = patch_response.json()["markdown"].lower()
    revisions_text = revisions_response.json()["document_id"].lower()
    full_text = patch_text + revisions_text + str(patch_response.json()).lower() + str(revisions_response.json()).lower()
    assert "sk-" not in full_text
    assert "bearer " not in full_text
    assert "authorization" not in full_text
    assert "cookie" not in full_text
    assert "password" not in full_text
    assert "token" not in full_text


# typing import at bottom to avoid circular confusion in test helper
from typing import Mapping  # noqa: E402
