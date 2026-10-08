from __future__ import annotations

from pydantic import BaseModel, Field


class ToolAvailability(BaseModel):
    available: bool = False
    generated: bool = False
    status: str = "idle"


class AgentSeriesScopeAuthority(BaseModel):
    """Revision-bound Series membership frozen before a legacy Agent turn.

    Written only by the pre-accept authority adapter; a client can never
    supply it.  Recovery replays this snapshot instead of resolving current
    membership again.
    """

    kind: str = "project_series_scope_v1"
    project_id: str
    series_id: str
    object_id: str
    payload_revision: int
    storage_revision: int
    authority_identity: str
    authority_ref: str


class AgentContext(BaseModel):
    session_id: str
    workspace_title: str = "Video Include"
    scope_type: str = "series"
    series_id: str | None = None
    series_title: str | None = None
    video_id: str | None = None
    video_title: str | None = None
    selected_tool: str | None = None
    series_authority: AgentSeriesScopeAuthority | None = None
    overview: ToolAvailability = Field(default_factory=ToolAvailability)
    mindmap: ToolAvailability = Field(default_factory=ToolAvailability)
    knowledge_cards: ToolAvailability = Field(default_factory=ToolAvailability)
    notes: ToolAvailability = Field(default_factory=ToolAvailability)
    preview: ToolAvailability = Field(default_factory=ToolAvailability)
    chapter_titles: list[str] = Field(default_factory=list)
