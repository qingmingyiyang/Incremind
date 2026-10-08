"""Strict, unregistered structured output shape for the comment extraction leaf."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .comparative_insights import ComparativeInsight, EvidenceSupport


class BodyInsight(ComparativeInsight):
    origin: Literal['body']


class CommentReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: StrictStr
    revision: StrictInt = Field(gt=0)
    ordinal: StrictInt = Field(gt=0)
    quote: StrictStr = Field(min_length=1)


class CommentInsight(ComparativeInsight):
    origin: Literal['comment']
    kind: Literal['supplement', 'differs']
    against: Literal['source_body', 'recognition']
    comment: CommentReference
    body_quote: StrictStr | None

    @model_validator(mode='after')
    def comparison_shape(self):
        if self.kind == 'supplement' and self.relation != 'supplement':
            raise ValueError('comment_comparison_invalid')
        if self.kind == 'differs' and self.relation not in {'differs', 'may_supersede'}:
            raise ValueError('comment_comparison_invalid')
        if self.against == 'source_body':
            if (self.target_id is not None or self.relation != self.kind
                    or self.body_quote is None or not self.body_quote.strip()):
                raise ValueError('comment_comparison_invalid')
        elif self.target_id is None or self.body_quote is not None:
            raise ValueError('comment_comparison_invalid')
        return self


class CommentOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    insights: list[Annotated[BodyInsight | CommentInsight, Field(discriminator='origin')]]
    supports: list[EvidenceSupport] = Field(max_length=5)
