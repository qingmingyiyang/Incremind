"""Explicit, human-reviewed restructuring routes; no model or publish shortcut."""

from fastapi import APIRouter, Request

from backend.recognition import RecognitionError
from backend.recognition.restructuring import RestructureProposalService


def install_restructure_routes(router: APIRouter, records, *, mutate, read_body, scope_for, revision, text, service, models):
    proposals = RestructureProposalService(service)

    def fields(body, allowed):
        if set(body).difference(allowed):
            raise RecognitionError("restructure request contains unsupported fields")



    @router.get("/restructure-proposals")
    async def listing(project_id: str = "default"):
        return {"items": proposals.list(scope=scope_for(project_id))}


    @router.patch("/restructure-proposals/{proposal_id}")
    async def review(proposal_id: str, request: Request):
        body = await read_body(request)
        fields(body, {"project_id", "expected_revision", "decision"})
        return mutate(proposals.review, scope=scope_for(body.get("project_id")),
                      proposal_id=proposal_id, expected_revision=revision(body.get("expected_revision")),
                      decision=body.get("decision"), reviewer="local-user")
