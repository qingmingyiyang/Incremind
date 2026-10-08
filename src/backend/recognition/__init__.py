"""User-reviewed, versioned project recognitions.

The package deliberately has no model client or FastAPI dependency.  A route
can submit user-selected experience and candidate text, then use this service
as the single write boundary for the SQLite authority.
"""

from .service import (
    CANDIDATE_GENERATION_STEP_VERSION,
    MAX_CONTENT_CHARS,
    MarkdownPreview,
    Experience,
    FixedQuestion,
    Recognition,
    RecognitionCandidate,
    RecognitionConflict,
    RecognitionError,
    RecognitionService,
    WorkScope,
    normalize_conditions,
)
from .provenance import ExperienceProvenance, ExperienceProvenanceError, SourceRef

__all__ = [
    "CANDIDATE_GENERATION_STEP_VERSION",
    "MAX_CONTENT_CHARS",
    "MarkdownPreview",
    "Experience",
    "ExperienceProvenance",
    "ExperienceProvenanceError",
    "FixedQuestion",
    "Recognition",
    "RecognitionCandidate",
    "RecognitionConflict",
    "RecognitionError",
    "RecognitionService",
    "SourceRef",
    "WorkScope",
    "normalize_conditions",
]
