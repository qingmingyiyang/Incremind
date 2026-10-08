"""Project registration and scene sidecars, without changing source revisions."""
from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..workspace_contracts import _json, _project
from .privacy import is_private_project, set_private_project_in_transaction


_PROJECTS = "v2_projects"
_BUILTINS = {"inbox": "收件箱", "me": "我"}
DEFAULT_NAME = "日常"
LEGACY_DEFAULT_NAME = "默认"
_OBJECT_TYPES = frozenset({"item", "document", "candidate", "recognition"})


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "invalid_" + label)
    return value.strip()


def display_name(project_id, name):
    """default 项目的旧占位名"默认"显示为"日常"；用户自己改过的名字原样显示，正式数据不改。"""
    return DEFAULT_NAME if project_id == "default" and name == LEGACY_DEFAULT_NAME else name


def project_by_tag(names, tag):
    """names 是 {项目id: 显示名}；旧写法 #默认 在没有别的项目叫"默认"时仍指 default。"""
    if tag == LEGACY_DEFAULT_NAME and LEGACY_DEFAULT_NAME not in names.values():
        return "default"
    return None


def _view(reader, row):
    return {"id": row.object_id, "name": display_name(row.object_id, row.payload["name"]),
            "scenes": list(row.payload["scenes"]),
            "private": is_private_project(reader, row.object_id),
            "builtin": row.payload.get("builtin"), "revision": row.revision}


def _register_projects(tx):
    identities = set(_BUILTINS)
    for collection in ("workspace_items", "documents", "recognitions"):
        for row in tx.list(collection):
            project_id = row.payload.get("project_id")
            if project_id is None:
                scope = row.payload.get("scope")
                project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
            if project_id is not None:
                identities.add(_project(project_id))
    for project_id in sorted(identities):
        if tx.read(_PROJECTS, project_id) is None:
            tx.put(_PROJECTS, project_id, {
                "name": _BUILTINS.get(project_id, DEFAULT_NAME if project_id == "default" else project_id),
                "scenes": [], "private": is_private_project(tx, project_id),
                "builtin": project_id if project_id in _BUILTINS else None,
            }, expected_revision=0)


def create_named_project(records, name):
    """Register one new ordinary project; an existing ordinary project with the same name is reused."""
    name = _name(name, "name")
    with records.begin() as tx:
        for row in tx.list(_PROJECTS):
            if row.payload["name"] == name and row.object_id not in _BUILTINS and row.object_id != "default":
                return row.object_id
        project_id = "project-" + uuid4().hex
        tx.put(_PROJECTS, project_id, {"name": name, "scenes": [], "private": False, "builtin": None},
               expected_revision=0)
        tx.commit()
    return project_id


def install_project_routes(application, *, records):
    router = APIRouter(prefix="/api/v2/projects")

    @router.get("")
    def list_projects():
        with records.begin() as tx:
            _register_projects(tx)
            result = {"items": [_view(tx, row) for row in tx.list(_PROJECTS)]}
            tx.commit()
        return result

    @router.post("")
    async def create_project(request: Request):
        body = await _json(request)
        if set(body) != {"name"}:
            raise HTTPException(400, "invalid_project_fields")
        name = _name(body["name"], "name")
        project_id = "project-" + uuid4().hex
        with records.begin() as tx:
            row = tx.put(_PROJECTS, project_id,
                         {"name": name, "scenes": [], "private": False, "builtin": None},
                         expected_revision=0)
            result = _view(tx, row)
            tx.commit()
        return result

    @router.patch("/{project_id}")
    async def patch_project(project_id: str, request: Request):
        project_id = _project(project_id)
        body = await _json(request)
        if set(body).difference({"name", "scenes", "private", "expected_revision"}):
            raise HTTPException(400, "invalid_project_fields")
        revision = body.get("expected_revision")
        if type(revision) is not int or revision < 1:
            raise HTTPException(400, "invalid_expected_revision")
        changes = {}
        if "name" in body:
            changes["name"] = _name(body["name"], "name")
        if "scenes" in body:
            if not isinstance(body["scenes"], list):
                raise HTTPException(400, "invalid_scenes")
            changes["scenes"] = [_name(scene, "scene") for scene in body["scenes"]]
        if "private" in body:
            if type(body["private"]) is not bool:
                raise HTTPException(400, "invalid_private")
            changes["private"] = body["private"]
        with records.begin() as tx:
            current = tx.read(_PROJECTS, project_id)
            if current is None:
                raise HTTPException(404, "project_not_found")
            if current.revision != revision:
                return JSONResponse(status_code=409, content={
                    "detail": "project_revision_conflict", "current": _view(tx, current)})
            if "private" in changes:
                private_row = tx.read("v2_private_scopes", project_id)
                set_private_project_in_transaction(
                    tx, project_id, changes["private"], private_row.revision if private_row else 0)
            row = tx.put(_PROJECTS, project_id,
                         {**current.payload, "private": is_private_project(tx, project_id), **changes},
                         expected_revision=revision)
            result = _view(tx, row)
            tx.commit()
        return result

    application.include_router(router)


def _scene_collection(object_type):
    if object_type not in _OBJECT_TYPES:
        raise HTTPException(400, "invalid_object_type")
    return "v2_scene_assignments_" + object_type


def assign_scene(records, object_type, object_id, project_id, scene, *, if_absent=False):
    collection = _scene_collection(object_type)
    object_id = _project(object_id)
    payload = {"project_id": _project(project_id), "scene": _name(scene, "scene")}
    with records.begin() as tx:
        current = tx.read(collection, object_id)
        if if_absent and current is not None:
            return current
        saved = tx.put(collection, object_id, payload,
                       expected_revision=current.revision if current else 0)
        tx.commit()
    return saved


def scene_of(reader, object_type, object_id):
    row = reader.read(_scene_collection(object_type), _project(object_id))
    return dict(row.payload) if row is not None else None
