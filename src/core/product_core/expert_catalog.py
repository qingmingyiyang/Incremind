"""Expert Catalog — P5 专家层生产目录切片。

ExpertProfile 是显式配置的声明式角色与能力组合，不是 Agent、Tool、Skill、
Secret、Session 或模型路由的新权威。它只保存对 immutable Skill revision、Tool id、
Context/Model policy、OutputStyleProfile 和 quality gate 的引用。

三个部分：
- ``ExpertCatalog``：全局专家目录，immutable revision + 状态生命周期 + 目录 lint。
- ``ExpertProjectBindingStore``：项目稀疏绑定（enabled revision、可选 default、
  binding revision CAS），不复制 prompt/Grant/Secret。
- ``ExpertConfigurationResolver``：只解析显式请求或项目默认绑定，产出冻结配置
  Receipt；恢复只 replay/verify，漂移 fail-closed，不做 affinity 或自动建议。

对齐 ``docs/vNext-remediation/ai-workbench-governance-v2.md`` 的 Expert Profile 治理：
选择不得扩权（未绑定专家一律 excluded）、default 只提高优先级、lint 拒绝职责重叠
未解释、触发模糊、输出合同缺失、依赖悬空、缺禁止事项或自主上限、现实资质暗示。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from threading import Lock
from typing import Mapping

_EXPERT_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_TOOL_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SCHEMA_VERSION = "1.0.0"
_PROFILE_STATUSES = {"draft", "active", "disabled", "retired"}
_MUTATION_STATUSES = {"active", "disabled", "retired"}
_SELECTION_MODES = {"manual", "disabled"}
_LEGACY_SELECTION_MODES = {"auto", "confirm"}
_AUTONOMY_CEILINGS = {"propose_only", "bounded_actions"}
_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "cookies", "password",
    "secret", "secret_key", "token", "access_token", "refresh_token",
}
_QUALIFICATION_HINTS = (
    "医生", "诊断", "处方", "医疗建议", "律师", "法律意见", "诉讼代理", "执业",
    "持证", "投资建议", "财务顾问", "证券推荐",
    "medical advice", "legal advice", "financial advice", "licensed physician",
)
_PATH_LOCKS: dict[Path, Lock] = {}
_PATH_LOCKS_GUARD = Lock()


class ExpertCatalogError(ValueError):
    pass


class ExpertCatalogConflict(ExpertCatalogError):
    pass


class ExpertCatalogNotFound(ExpertCatalogError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _path_lock(path: Path) -> Lock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, Lock())


def _reject_sensitive_material(value: object, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_secret"):
                raise ExpertCatalogError(f"sensitive material is forbidden at {path}.{key}")
            _reject_sensitive_material(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_material(nested, path=f"{path}[{index}]")


def _clean_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


# ---------------------------------------------------------------------------
# 目录 lint
# ---------------------------------------------------------------------------


def lint_expert_profile(
    profile: Mapping[str, object],
    *,
    existing_active: tuple[Mapping[str, object], ...] = (),
) -> list[str]:
    """返回 lint 错误列表；空列表表示通过。

    拒绝：缺角色/方法/输出合同/禁止事项/自主上限、空 skills/tools、悬空或重复
    引用、敏感物质、现实资质暗示、与既有 active 专家职责重叠且无 differentiation。
    """
    errors: list[str] = []
    expert_id = _clean_str(profile.get("expert_id"))
    if not _EXPERT_ID.fullmatch(expert_id):
        errors.append("expert_id must match ^[a-z][a-z0-9_-]{2,63}$")
    for field in ("role", "method", "output_contract"):
        if not _clean_str(profile.get(field)):
            errors.append(f"{field} is required and must be non-empty")
    prohibited = _str_list(profile.get("prohibited"))
    if not prohibited:
        errors.append("prohibited must list at least one prohibition")
    applicable = _str_list(profile.get("applicable_tasks"))
    if not applicable:
        errors.append("applicable_tasks must name at least one task intent")
    if len(set(applicable)) != len(applicable):
        errors.append("applicable_tasks must not contain duplicates")
    if _clean_str(profile.get("autonomy_ceiling")) not in _AUTONOMY_CEILINGS:
        errors.append(f"autonomy_ceiling must be one of {sorted(_AUTONOMY_CEILINGS)}")

    skills = profile.get("skills")
    if not isinstance(skills, list) or not skills:
        errors.append("skills must reference at least one immutable skill revision")
    else:
        seen_skills: set[str] = set()
        for index, entry in enumerate(skills):
            if not isinstance(entry, Mapping) or not _clean_str(entry.get("skill_id")):
                errors.append(f"skills[{index}] must reference a skill_id")
                continue
            skill_id = _clean_str(entry.get("skill_id"))
            revision = entry.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                errors.append(f"skills[{index}].revision must be a positive int (immutable ref)")
            if skill_id in seen_skills:
                errors.append(f"skills must not duplicate skill_id {skill_id}")
            seen_skills.add(skill_id)

    tools = profile.get("tools")
    if not isinstance(tools, list) or not tools:
        errors.append("tools must list at least one governed tool id")
    else:
        seen_tools: set[str] = set()
        for index, tool in enumerate(tools):
            tool_id = _clean_str(tool)
            if not _TOOL_ID.fullmatch(tool_id):
                errors.append(f"tools[{index}] is not a valid tool id")
            if tool_id in seen_tools:
                errors.append(f"tools must not duplicate tool id {tool_id}")
            seen_tools.add(tool_id)

    for ref_field in ("model_policy_ref", "context_policy_ref"):
        ref = profile.get(ref_field)
        if ref is None:
            continue
        if not isinstance(ref, Mapping) or not ref:
            errors.append(f"{ref_field} must be a non-empty reference object when present")
    scorecard = profile.get("quality_gate_ref")
    if not isinstance(scorecard, Mapping) or not scorecard:
        errors.append("quality_gate_ref must reference a governed scorecard")
    else:
        if not _clean_str(scorecard.get("scorecard_id")):
            errors.append("quality_gate_ref.scorecard_id must be non-empty")
        revision = scorecard.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            errors.append("quality_gate_ref.revision must be a positive int")

    texts = " \n ".join(
        _clean_str(profile.get(field))
        for field in ("role", "method", "output_contract", "differentiation")
    ) + " \n " + " \n ".join(prohibited)
    lowered = texts.lower()
    for hint in _QUALIFICATION_HINTS:
        if hint in lowered:
            errors.append(f"profile text implies real-world qualification: {hint}")
            break

    try:
        _reject_sensitive_material(dict(profile), path="profile")
    except ExpertCatalogError as error:
        errors.append(str(error))

    if existing_active and not _clean_str(profile.get("differentiation")):
        mine = set(applicable)
        for other in existing_active:
            other_id = _clean_str(other.get("expert_id"))
            if other_id and other_id == expert_id:
                continue
            if mine & set(_str_list(other.get("applicable_tasks"))):
                errors.append(
                    f"overlapping duties with active expert {other_id} require an explicit differentiation"
                )
                break
    return errors


# ---------------------------------------------------------------------------
# 全局专家目录
# ---------------------------------------------------------------------------


class ExpertCatalog:
    """全局专家目录：immutable expert revision、状态生命周期与 history。"""

    schema_version = _SCHEMA_VERSION

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)
        self._path = self._root_dir / "library" / "global" / "expert-catalog" / "expert-catalog.json"
        self._lock = _path_lock(self._path)

    @property
    def registry_revision(self) -> int:
        with self._lock:
            return self._load()["registry_revision"]

    def get(self, expert_id: str) -> dict[str, object] | None:
        clean = _clean_str(expert_id)
        if not clean:
            return None
        with self._lock:
            experts = self._load()["experts"]
        record = experts.get(clean)
        return deepcopy(record) if isinstance(record, dict) else None

    def get_revision(self, expert_id: str, revision: int) -> dict[str, object] | None:
        clean = _clean_str(expert_id)
        with self._lock:
            catalog = self._load()
        current = catalog["experts"].get(clean)
        if isinstance(current, dict) and current.get("revision") == revision:
            return deepcopy(current)
        for entry in reversed(catalog["history"]):
            expert = entry.get("expert")
            if (
                entry.get("expert_id") == clean
                and isinstance(expert, Mapping)
                and expert.get("revision") == revision
            ):
                return deepcopy(dict(expert))
        return None

    def list_experts(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            experts = self._load()["experts"]
        return tuple(deepcopy(dict(experts[key])) for key in sorted(experts))

    def create(
        self,
        profile: Mapping[str, object],
        *,
        expected_registry_revision: int,
    ) -> dict[str, object]:
        payload = deepcopy(dict(profile))
        payload.pop("revision", None)
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        status = _clean_str(payload.get("status")) or "draft"
        if status not in {"draft", "active"}:
            raise ExpertCatalogError("a new expert must start as draft or active")
        payload["status"] = status
        expert_id = _clean_str(payload.get("expert_id"))
        with self._lock:
            catalog = self._load()
            if catalog["experts"].get(expert_id) is not None:
                raise ExpertCatalogConflict(f"expert {expert_id} already exists")
            errors = lint_expert_profile(
                payload,
                existing_active=tuple(
                    dict(item) for item in catalog["experts"].values()
                    if isinstance(item, Mapping) and item.get("status") == "active"
                ),
            )
            if errors:
                raise ExpertCatalogError("expert profile lint failed: " + "; ".join(errors))
            stamped = dict(payload)
            stamped["revision"] = 1
            stamped["created_at"] = _now()
            stamped["updated_at"] = stamped["created_at"]
            next_revision = catalog["registry_revision"] + 1
            self._expect_revision(catalog, expected_registry_revision)
            catalog["registry_revision"] = next_revision
            catalog["experts"][expert_id] = stamped
            catalog["history"].append({
                "registry_revision": next_revision,
                "expert_id": expert_id,
                "expert_revision": 1,
                "action": "created",
                "recorded_at": _now(),
                "expert": deepcopy(stamped),
            })
            self._save(catalog)
        return deepcopy(stamped)

    def upgrade(
        self,
        expert_id: str,
        profile: Mapping[str, object],
        *,
        expected_expert_revision: int,
        expected_registry_revision: int,
    ) -> dict[str, object]:
        payload = deepcopy(dict(profile))
        payload.pop("revision", None)
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        payload.pop("status", None)
        with self._lock:
            catalog = self._load()
            current = catalog["experts"].get(_clean_str(expert_id))
            if not isinstance(current, dict):
                raise ExpertCatalogNotFound(f"expert {expert_id} does not exist")
            if current.get("revision") != expected_expert_revision:
                raise ExpertCatalogConflict(
                    f"expert {expert_id} revision drift: expected {expected_expert_revision}"
                )
            if current.get("status") == "retired":
                raise ExpertCatalogError("a retired expert cannot be upgraded")
            merged = deepcopy(current)
            merged.update(payload)
            merged["expert_id"] = _clean_str(expert_id)
            errors = lint_expert_profile(
                merged,
                existing_active=tuple(
                    dict(item) for item in catalog["experts"].values()
                    if isinstance(item, Mapping) and item.get("status") == "active"
                    and item.get("expert_id") != expert_id
                ),
            )
            if errors:
                raise ExpertCatalogError("expert profile lint failed: " + "; ".join(errors))
            upgraded = dict(merged)
            upgraded["revision"] = int(current["revision"]) + 1
            upgraded["status"] = current["status"]
            upgraded["created_at"] = current["created_at"]
            upgraded["updated_at"] = _now()
            next_revision = catalog["registry_revision"] + 1
            self._expect_revision(catalog, expected_registry_revision)
            catalog["registry_revision"] = next_revision
            catalog["experts"][expert_id] = upgraded
            catalog["history"].append({
                "registry_revision": next_revision,
                "expert_id": expert_id,
                "expert_revision": upgraded["revision"],
                "action": "upgraded",
                "recorded_at": upgraded["updated_at"],
                "expert": deepcopy(upgraded),
            })
            self._save(catalog)
        return deepcopy(upgraded)

    def set_status(
        self,
        expert_id: str,
        status: str,
        *,
        reason: str,
        expected_expert_revision: int,
        expected_registry_revision: int,
    ) -> dict[str, object]:
        if status not in _MUTATION_STATUSES:
            raise ExpertCatalogError(f"status must be one of {sorted(_MUTATION_STATUSES)}")
        if not _clean_str(reason):
            raise ExpertCatalogError("a reason is required for every status change")
        with self._lock:
            catalog = self._load()
            current = catalog["experts"].get(_clean_str(expert_id))
            if not isinstance(current, dict):
                raise ExpertCatalogNotFound(f"expert {expert_id} does not exist")
            if current.get("status") == "retired":
                raise ExpertCatalogError("a retired expert is terminal")
            if current.get("revision") != expected_expert_revision:
                raise ExpertCatalogConflict(
                    f"expert {expert_id} revision drift: expected {expected_expert_revision}"
                )
            updated = deepcopy(current)
            updated["status"] = status
            updated["updated_at"] = _now()
            next_revision = catalog["registry_revision"] + 1
            self._expect_revision(catalog, expected_registry_revision)
            catalog["registry_revision"] = next_revision
            catalog["experts"][expert_id] = updated
            catalog["history"].append({
                "registry_revision": next_revision,
                "expert_id": _clean_str(expert_id),
                "expert_revision": updated["revision"],
                "action": f"status:{status}",
                "recorded_at": updated["updated_at"],
                "reason": _clean_str(reason),
                "expert": deepcopy(updated),
            })
            self._save(catalog)
        return deepcopy(updated)

    def _expect_revision(self, catalog: dict, expected_registry_revision: int) -> None:
        if catalog["registry_revision"] != expected_registry_revision:
            raise ExpertCatalogConflict(
                "registry revision drift: expected "
                f"{expected_registry_revision}, current {catalog['registry_revision']}"
            )

    def _load(self) -> dict:
        if not self._path.exists():
            return {"schema_version": _SCHEMA_VERSION, "registry_revision": 0, "experts": {}, "history": []}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("experts"), dict):
            raise ExpertCatalogError("expert catalog file is corrupted")
        raw.setdefault("history", [])
        return raw

    def _save(self, catalog: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# 项目稀疏绑定
# ---------------------------------------------------------------------------


class ExpertProjectBindingStore:
    """ProjectExpertBinding：只保存 enabled revision、default、affinity、mode 与 CAS。"""

    schema_version = _SCHEMA_VERSION

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)
        self._path = self._root_dir / "library" / "global" / "expert-bindings" / "expert-bindings.json"
        self._lock = _path_lock(self._path)

    @property
    def store_revision(self) -> int:
        with self._lock:
            return self._load()["store_revision"]

    def get(self, project_id: str, expert_id: str) -> dict[str, object] | None:
        with self._lock:
            bindings = self._load()["bindings"]
        project_bindings = bindings.get(_clean_str(project_id))
        if not isinstance(project_bindings, Mapping):
            return None
        record = project_bindings.get(_clean_str(expert_id))
        return deepcopy(dict(record)) if isinstance(record, Mapping) else None

    def list_for_project(self, project_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            bindings = self._load()["bindings"]
        project_bindings = bindings.get(_clean_str(project_id))
        if not isinstance(project_bindings, Mapping):
            return ()
        return tuple(
            deepcopy(dict(project_bindings[key])) for key in sorted(project_bindings)
        )

    def bind(
        self,
        project_id: str,
        expert_id: str,
        *,
        catalog: ExpertCatalog,
        enabled_expert_revision: int,
        intent_affinity: list[str] | None = None,
        selection_mode: str = "manual",
        default: bool = False,
        reason: str,
        expected_store_revision: int,
    ) -> dict[str, object]:
        clean_project = _clean_str(project_id)
        clean_expert = _clean_str(expert_id)
        if not _PROJECT_ID.fullmatch(clean_project):
            raise ExpertCatalogError("project_id is not valid")
        del intent_affinity
        if selection_mode in _LEGACY_SELECTION_MODES:
            selection_mode = "manual"
        if selection_mode not in _SELECTION_MODES:
            raise ExpertCatalogError(f"selection_mode must be one of {sorted(_SELECTION_MODES)}")
        expert = catalog.get(clean_expert)
        if expert is None:
            raise ExpertCatalogNotFound(f"expert {clean_expert} does not exist")
        if expert.get("status") != "active":
            raise ExpertCatalogError("only an active expert can be bound to a project")
        if expert.get("revision") != enabled_expert_revision:
            raise ExpertCatalogConflict(
                f"expert {clean_expert} revision drift: catalog has {expert.get('revision')}"
            )
        with self._lock:
            store = self._load()
            project_bindings = store["bindings"].setdefault(clean_project, {})
            if clean_expert in project_bindings:
                raise ExpertCatalogConflict(
                    f"expert {clean_expert} is already bound to project {clean_project}"
                )
            if default and any(
                isinstance(item, Mapping) and item.get("default") is True
                for item in project_bindings.values()
            ):
                raise ExpertCatalogConflict(
                    f"project {clean_project} already has a default expert"
                )
            binding = {
                "schema_version": _SCHEMA_VERSION,
                "project_id": clean_project,
                "expert_id": clean_expert,
                "enabled_expert_revision": enabled_expert_revision,
                "selection_mode": selection_mode,
                "default": bool(default),
                "binding_revision": 1,
                "reason": _clean_str(reason),
                "created_at": _now(),
                "updated_at": _now(),
            }
            next_revision = store["store_revision"] + 1
            self._expect_revision(store, expected_store_revision)
            store["store_revision"] = next_revision
            project_bindings[clean_expert] = binding
            store["history"].append({
                "store_revision": next_revision,
                "project_id": clean_project,
                "expert_id": clean_expert,
                "action": "bound",
                "recorded_at": binding["updated_at"],
                "binding": deepcopy(binding),
            })
            self._save(store)
        return deepcopy(binding)

    def update(
        self,
        project_id: str,
        expert_id: str,
        *,
        expected_binding_revision: int,
        expected_store_revision: int,
        enabled_expert_revision: int | None = None,
        intent_affinity: list[str] | None = None,
        selection_mode: str | None = None,
        default: bool | None = None,
        reason: str,
    ) -> dict[str, object]:
        del intent_affinity
        if not _clean_str(reason):
            raise ExpertCatalogError("a reason is required for every binding update")
        with self._lock:
            store = self._load()
            binding = self._find(store, project_id, expert_id)
            updated = deepcopy(binding)
            if enabled_expert_revision is not None:
                if not isinstance(enabled_expert_revision, int) or enabled_expert_revision < 1:
                    raise ExpertCatalogError("enabled_expert_revision must be a positive int")
                updated["enabled_expert_revision"] = enabled_expert_revision
            if selection_mode is not None:
                if selection_mode in _LEGACY_SELECTION_MODES:
                    selection_mode = "manual"
                if selection_mode not in _SELECTION_MODES:
                    raise ExpertCatalogError(
                        f"selection_mode must be one of {sorted(_SELECTION_MODES)}"
                    )
                updated["selection_mode"] = selection_mode
            if default is not None:
                if default is True:
                    project_bindings = store["bindings"][_clean_str(project_id)]
                    for other_id, other in project_bindings.items():
                        if other_id != _clean_str(expert_id) and isinstance(other, Mapping) and other.get("default") is True:
                            raise ExpertCatalogConflict(
                                f"project {project_id} already has a default expert ({other_id})"
                            )
                updated["default"] = bool(default)
            updated["binding_revision"] = int(binding["binding_revision"]) + 1
            updated["updated_at"] = _now()
            updated["reason"] = _clean_str(reason)
            next_revision = store["store_revision"] + 1
            self._expect_revision(store, expected_store_revision)
            if binding["binding_revision"] != expected_binding_revision:
                raise ExpertCatalogConflict(
                    f"binding revision drift: expected {expected_binding_revision}"
                )
            store["store_revision"] = next_revision
            store["bindings"][_clean_str(project_id)][_clean_str(expert_id)] = updated
            store["history"].append({
                "store_revision": next_revision,
                "project_id": _clean_str(project_id),
                "expert_id": _clean_str(expert_id),
                "action": "updated",
                "recorded_at": updated["updated_at"],
                "binding": deepcopy(updated),
            })
            self._save(store)
        return deepcopy(updated)

    def unbind(
        self,
        project_id: str,
        expert_id: str,
        *,
        reason: str,
        expected_binding_revision: int,
        expected_store_revision: int,
    ) -> dict[str, object]:
        if not _clean_str(reason):
            raise ExpertCatalogError("a reason is required for unbind")
        with self._lock:
            store = self._load()
            binding = self._find(store, project_id, expert_id)
            if binding["binding_revision"] != expected_binding_revision:
                raise ExpertCatalogConflict(
                    f"binding revision drift: expected {expected_binding_revision}"
                )
            removed = deepcopy(binding)
            removed["updated_at"] = _now()
            removed["reason"] = _clean_str(reason)
            next_revision = store["store_revision"] + 1
            self._expect_revision(store, expected_store_revision)
            store["store_revision"] = next_revision
            del store["bindings"][_clean_str(project_id)][_clean_str(expert_id)]
            if not store["bindings"][_clean_str(project_id)]:
                del store["bindings"][_clean_str(project_id)]
            store["history"].append({
                "store_revision": next_revision,
                "project_id": _clean_str(project_id),
                "expert_id": _clean_str(expert_id),
                "action": "unbound",
                "recorded_at": removed["updated_at"],
                "binding": removed,
            })
            self._save(store)
        return removed

    def _find(self, store: dict, project_id: str, expert_id: str) -> dict[str, object]:
        project_bindings = store["bindings"].get(_clean_str(project_id))
        record = project_bindings.get(_clean_str(expert_id)) if isinstance(project_bindings, Mapping) else None
        if not isinstance(record, Mapping):
            raise ExpertCatalogNotFound(
                f"expert {expert_id} is not bound to project {project_id}"
            )
        return dict(record)

    def _expect_revision(self, store: dict, expected_store_revision: int) -> None:
        if store["store_revision"] != expected_store_revision:
            raise ExpertCatalogConflict(
                "binding store revision drift: expected "
                f"{expected_store_revision}, current {store['store_revision']}"
            )

    def _load(self) -> dict:
        if not self._path.exists():
            return {"schema_version": _SCHEMA_VERSION, "store_revision": 0, "bindings": {}, "history": []}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("bindings"), dict):
            raise ExpertCatalogError("expert binding store file is corrupted")
        raw.setdefault("history", [])
        return raw

    def _save(self, store: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# 选择与 Receipt
# ---------------------------------------------------------------------------


_RECEIPT_SCHEMA_VERSION = "1.0.0"


class ExpertConfigurationResolver:
    """Resolve only an explicit expert or the single configured project default."""

    def __init__(self, catalog: ExpertCatalog, bindings: ExpertProjectBindingStore) -> None:
        self._catalog = catalog
        self._bindings = bindings

    def select(
        self,
        project_id: str,
        task_intents: list[str],
        requested_expert_id: str | None = None,
        *,
        budget: str | None = None,
    ) -> dict[str, object]:
        clean_project = _clean_str(project_id)
        intents = [item for item in _str_list(task_intents)]
        requested = _clean_str(requested_expert_id) or None
        if requested is not None:
            receipt = self._select_explicit(clean_project, intents, requested)
        else:
            receipt = self._select_project_default(clean_project)
        receipt["schema_version"] = _RECEIPT_SCHEMA_VERSION
        receipt["project_id"] = clean_project
        receipt["task_intents"] = intents
        receipt["requested_expert_id"] = requested
        receipt["budget"] = _clean_str(budget) or "default"
        receipt["fallback"] = "generic_agent"
        receipt["decided_at"] = _now()
        return receipt

    def _select_explicit(
        self, project_id: str, intents: list[str], expert_id: str
    ) -> dict[str, object]:
        receipt: dict[str, object] = {
            "selection_mode": "explicit",
            "selected": None,
            "candidates": [],
            "frozen_refs": {},
            "autonomy_ceiling": None,
        }
        expert = self._catalog.get(expert_id)
        if expert is None:
            receipt["candidates"].append({
                "expert_id": expert_id, "expert_revision": None, "binding_revision": None,
                "status": "excluded", "reason": "unknown_expert",
            })
            return receipt
        if expert.get("status") != "active":
            receipt["candidates"].append({
                "expert_id": expert_id, "expert_revision": expert.get("revision"),
                "binding_revision": None, "status": "excluded", "reason": "expert_not_active",
            })
            return receipt
        binding = self._bindings.get(project_id, expert_id)
        if binding is None:
            receipt["candidates"].append({
                "expert_id": expert_id, "expert_revision": expert.get("revision"),
                "binding_revision": None, "status": "excluded",
                "reason": "expert_not_bound_to_project",
            })
            return receipt
        if binding.get("selection_mode") == "disabled":
            receipt["candidates"].append({
                "expert_id": expert_id, "expert_revision": expert.get("revision"),
                "binding_revision": binding.get("binding_revision"),
                "status": "excluded", "reason": "binding_disabled",
            })
            return receipt
        if binding.get("enabled_expert_revision") != expert.get("revision"):
            receipt["candidates"].append({
                "expert_id": expert_id, "expert_revision": expert.get("revision"),
                "binding_revision": binding.get("binding_revision"),
                "status": "excluded", "reason": "binding_revision_drift",
            })
            return receipt
        receipt["selected"] = {
            "expert_id": expert_id,
            "expert_revision": expert.get("revision"),
            "binding_revision": binding.get("binding_revision"),
            "requires_confirmation": False,
            "reason": "explicit_request",
        }
        receipt["candidates"].append({
            "expert_id": expert_id, "expert_revision": expert.get("revision"),
            "binding_revision": binding.get("binding_revision"),
            "status": "selected", "reason": "explicit_request",
        })
        receipt["frozen_refs"] = self._frozen_refs(expert)
        receipt["autonomy_ceiling"] = expert.get("autonomy_ceiling")
        return receipt

    def _select_project_default(self, project_id: str) -> dict[str, object]:
        receipt: dict[str, object] = {
            "selection_mode": "none",
            "selected": None,
            "candidates": [],
            "frozen_refs": {},
            "autonomy_ceiling": None,
        }
        for binding in self._bindings.list_for_project(project_id):
            expert_id = _clean_str(binding.get("expert_id"))
            candidate: dict[str, object] = {
                "expert_id": expert_id,
                "expert_revision": binding.get("enabled_expert_revision"),
                "binding_revision": binding.get("binding_revision"),
                "status": "excluded",
                "reason": "",
            }
            receipt["candidates"].append(candidate)
            expert = self._catalog.get(expert_id)
            if expert is None:
                candidate["reason"] = "unknown_expert"
                continue
            candidate["expert_revision"] = expert.get("revision")
            if expert.get("status") != "active":
                candidate["reason"] = "expert_not_active"
                continue
            if binding.get("enabled_expert_revision") != expert.get("revision"):
                candidate["reason"] = "binding_revision_drift"
                continue
            mode = _clean_str(binding.get("selection_mode"))
            if mode == "disabled":
                candidate["reason"] = "binding_disabled"
                continue
            if binding.get("default") is not True:
                candidate["reason"] = "not_project_default"
                continue
            candidate["reason"] = "project_default_binding"
            candidate["status"] = "selected"
            receipt["selection_mode"] = "project_default"
            receipt["selected"] = {
                "expert_id": expert_id,
                "expert_revision": expert.get("revision"),
                "binding_revision": binding.get("binding_revision"),
                "requires_confirmation": False,
                "reason": "project_default_binding",
            }
            receipt["frozen_refs"] = self._frozen_refs(expert)
            receipt["autonomy_ceiling"] = expert.get("autonomy_ceiling")
            break
        return receipt

    def _frozen_refs(self, expert: Mapping[str, object]) -> dict[str, object]:
        return {
            "skills": deepcopy(expert.get("skills")),
            "tools": deepcopy(expert.get("tools")),
            "model_policy_ref": deepcopy(expert.get("model_policy_ref")),
            "context_policy_ref": deepcopy(expert.get("context_policy_ref")),
            "quality_gate_ref": deepcopy(expert.get("quality_gate_ref")),
        }

    def verify_receipt(self, receipt: Mapping[str, object]) -> dict[str, object]:
        """恢复路径专用：只校验冻结快照，不重新选择。漂移返回 drifted。"""
        selected = receipt.get("selected")
        if not isinstance(selected, Mapping):
            return {"status": "ok", "reasons": []}
        reasons: list[str] = []
        expert_id = _clean_str(selected.get("expert_id"))
        expert = self._catalog.get(expert_id)
        if expert is None:
            reasons.append("expert_missing_from_catalog")
        else:
            if expert.get("revision") != selected.get("expert_revision"):
                reasons.append("expert_revision_drift")
            if expert.get("status") != "active":
                reasons.append("expert_not_active")
            frozen = receipt.get("frozen_refs")
            if isinstance(frozen, Mapping) and frozen.get("skills") != self._frozen_refs(expert)["skills"]:
                reasons.append("frozen_skill_refs_drift")
        binding = self._bindings.get(_clean_str(receipt.get("project_id")), expert_id)
        if binding is None:
            reasons.append("binding_missing")
        else:
            if binding.get("binding_revision") != selected.get("binding_revision"):
                reasons.append("binding_revision_drift")
            if binding.get("selection_mode") == "disabled":
                reasons.append("binding_disabled")
        if reasons:
            return {"status": "drifted", "reasons": reasons}
        return {"status": "ok", "reasons": []}


def default_video_research_expert_profile() -> dict[str, object]:
    """P5 计划的试点专家：引用 analyze_source 与媒体理解/知识入库 Skill。"""
    return {
        "schema_version": _SCHEMA_VERSION,
        "expert_id": "video-research-expert",
        "role": "视频内容研究与证据整理",
        "method": "字幕优先、ASR 回退、观点与证据对应、时间戳追溯",
        "skills": [
            {"skill_id": "media-comprehension", "revision": 1},
            {"skill_id": "knowledge-intake", "revision": 1},
        ],
        "tools": ["analyze_source", "memory.recall", "document.draft.propose"],
        "applicable_tasks": ["media_analysis", "research", "knowledge_intake"],
        "output_contract": "输出必须包含结论、逐条证据引用和时间戳定位；无证据的判断标记为推断。",
        "prohibited": [
            "不直接修改长期记忆，只产生候选提案",
            "不引用未授权来源或跨项目内容",
            "不外发未授权正文",
        ],
        "autonomy_ceiling": "propose_only",
        "quality_gate_ref": {
            "scorecard_id": "evidence-grounded-research",
            "revision": 1,
        },
        "status": "draft",
    }
