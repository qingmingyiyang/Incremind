from __future__ import annotations

import re
from dataclasses import dataclass

from .source_spec import SourceSpec, SourceSpecError, parse_source_spec


class InstallIntentError(ValueError):
    """Raised when natural language cannot form a safe installation proposal."""


_INTENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,159}$")
_INSTALL_PREFIX = re.compile(
    r"^(?:(?:请|麻烦)?(?:帮我)?(?:安装|添加|接入|导入)|(?:please\s+)?(?:install|add|connect|import)\b)\s*",
    re.IGNORECASE,
)
_KIND_PREFIXES = (
    (re.compile(r"^(?:(?:a|an|the|一个|这个)\s+)?(?:技能|skill)\b\s*[:：]?\s*", re.IGNORECASE), "skill"),
    (re.compile(r"^(?:(?:a|an|the|一个|这个)\s+)?(?:插件|plugin)\b\s*[:：]?\s*", re.IGNORECASE), "plugin"),
    (re.compile(r"^(?:(?:a|an|the|一个|这个)\s+)?mcp(?:\s*(?:server|服务器))?\b\s*[:：]?\s*", re.IGNORECASE), "mcp"),
    (re.compile(r"^(?:(?:a|an|the|一个|这个)\s+)?(?:仓库|repository\b|repo\b)\s*[:：]?\s*", re.IGNORECASE), "repository"),
)
_FORBIDDEN_TOKENS = ("\x00", "\r", "\n", "`", "$(`", ";", "&&", "||", "|", ">", "<")
_REFERENCE_ALLOWED_PUNCTUATION = frozenset("-._/@:+~= ")


@dataclass(frozen=True, slots=True)
class InstallIntent:
    intent_id: str
    kind_hint: str
    source_spec: SourceSpec | None
    search_term: str | None
    project_id: str | None
    disposition: str
    reason_codes: tuple[str, ...]
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not _INTENT_ID.fullmatch(self.intent_id):
            raise InstallIntentError("install intent id is invalid")
        if self.kind_hint not in {"auto", "skill", "plugin", "mcp", "repository"}:
            raise InstallIntentError("install kind hint is invalid")
        if (self.source_spec is None) == (self.search_term is None):
            raise InstallIntentError("install intent requires exactly one source or search term")
        if self.source_spec is not None and not isinstance(self.source_spec, SourceSpec):
            raise InstallIntentError("install source spec is invalid")
        if self.search_term is not None:
            if _safe_search_term(self.search_term) != self.search_term:
                raise InstallIntentError("install search term is not canonical")
        if self.project_id is not None and not _PROJECT_ID.fullmatch(self.project_id):
            raise InstallIntentError("install project id is invalid")
        if self.disposition not in {"AUTO_WITH_NOTICE", "ASK", "REJECT"}:
            raise InstallIntentError("install disposition is invalid")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if tuple(sorted(set(self.reason_codes))) != self.reason_codes:
            raise InstallIntentError("install reason codes must be sorted and unique")
        if self.schema_version != "1.0.0":
            raise InstallIntentError("install intent schema is invalid")


def parse_install_intent(
    text: str,
    *,
    intent_id: str,
    project_id: str | None = None,
    requested_ref: str | None = None,
    subpath: str | None = None,
) -> InstallIntent:
    """Parse an explicit install request without model, network, or filesystem access.

    An exact locator may proceed automatically only to source resolution and
    quarantine preview.  It never authorizes activation or execution.
    """

    if not isinstance(text, str) or not text.strip() or len(text) > 1024:
        raise InstallIntentError("installation request text is invalid")
    normalized = text.strip()
    if any(token in normalized for token in _FORBIDDEN_TOKENS):
        raise InstallIntentError("installation request contains executable syntax")
    remainder = _INSTALL_PREFIX.sub("", normalized, count=1).strip()
    if remainder == normalized:
        raise InstallIntentError("installation request must use an explicit install verb")
    kind_hint = "auto"
    for pattern, candidate_kind in _KIND_PREFIXES:
        matched = pattern.match(remainder)
        if matched:
            kind_hint = candidate_kind
            remainder = remainder[matched.end():].strip()
            break
    remainder = _trim_polite_suffix(remainder)
    if not remainder:
        raise InstallIntentError("installation request does not identify a source")
    try:
        source = parse_source_spec(
            remainder,
            request_id=intent_id,
            requested_ref=requested_ref,
            subpath=subpath,
        )
    except SourceSpecError as error:
        if _looks_like_explicit_locator(remainder):
            raise InstallIntentError("explicit installation source is unsupported or unsafe") from error
        search_term = _safe_search_term(remainder)
        return InstallIntent(
            intent_id=intent_id,
            kind_hint=kind_hint,
            source_spec=None,
            search_term=search_term,
            project_id=project_id,
            disposition="ASK",
            reason_codes=("source_resolution_required",),
        )
    _require_kind_compatibility(kind_hint, source)
    if source.kind == "mcp_endpoint":
        return InstallIntent(
            intent_id=intent_id,
            kind_hint=kind_hint,
            source_spec=source,
            search_term=None,
            project_id=project_id,
            disposition="ASK",
            reason_codes=("endpoint_network_review",),
        )
    return InstallIntent(
        intent_id=intent_id,
        kind_hint=kind_hint,
        source_spec=source,
        search_term=None,
        project_id=project_id,
        disposition="AUTO_WITH_NOTICE",
        reason_codes=("quarantine_preview_only",),
    )


def _trim_polite_suffix(value: str) -> str:
    for suffix in ("，谢谢", "谢谢", " please", " now"):
        if value.lower().endswith(suffix.lower()):
            return value[: -len(suffix)].strip()
    return value


def _safe_search_term(value: str) -> str:
    if len(value) < 2 or len(value) > 160:
        raise InstallIntentError("installation search term is invalid")
    if any(not (character.isalnum() or character in _REFERENCE_ALLOWED_PUNCTUATION) for character in value):
        raise InstallIntentError("installation search term contains unsupported characters")
    return " ".join(value.split())


def _looks_like_explicit_locator(value: str) -> bool:
    lowered = value.lower()
    return (
        "://" in value
        or lowered.startswith(("npm:", "pypi:", "marketplace:", "crp:"))
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value) is not None
    )


def _require_kind_compatibility(kind_hint: str, source: SourceSpec) -> None:
    repository_sources = {
        "github_repository", "git_repository", "https_archive", "registry_package", "marketplace_entry",
        "local_selection",
    }
    if kind_hint == "mcp" and source.kind != "mcp_endpoint":
        raise InstallIntentError("MCP installation requires an explicit MCP endpoint or a catalog search")
    if kind_hint in {"skill", "plugin", "repository"} and source.kind not in repository_sources:
        raise InstallIntentError("repository installation source kind is incompatible")
