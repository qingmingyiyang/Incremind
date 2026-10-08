from types import SimpleNamespace

import pytest

from backend.api.four_layer_memory_candidate_ai_runtime import (
    FourLayerMemoryCandidateEvidenceGrantStore,
    ScopedFourLayerMemoryEvidenceCapability,
)


def test_memory_evidence_grant_is_opaque_short_lived_and_revokeable():
    store=FourLayerMemoryCandidateEvidenceGrantStore()
    public=store.issue(source_id="source-1",evidence_kind="source_content_read",evidence_id="evidence-1",allowed_layers=("atom",),project_id="project-a",grant_id="stable-grant")
    assert public["grant_id"]=="stable-grant" and set(public)=={"grant_id","revision"}
    assert "source-1" not in str(public) and store.inspect("stable-grant").public()==public
    assert store.revoke("stable-grant") is True and store.revoke("stable-grant") is False
    with pytest.raises(ValueError,match="unavailable"): store.inspect("stable-grant")


def test_scoped_evidence_reads_app_grant_and_includes_revision_baseline_without_preview():
    store=FourLayerMemoryCandidateEvidenceGrantStore(); public=store.issue(source_id="source-1",evidence_kind="source_content_read",evidence_id="evidence-1",allowed_layers=("atom",),project_id="project-a")
    app=SimpleNamespace(state=SimpleNamespace(four_layer_memory_candidate_evidence_grant_store=store))
    loader=lambda grant:{"status":"completed","evidence_id":grant.evidence_id,"source_id":grant.source_id,"kind":grant.evidence_kind,"revision":1,"summary":"secret preview","refs":["crp://default/sources/source-1"]}
    authority=lambda:{"prompt_revision":1,"route_revision":1,"provider_revision":1,"provider_id":"provider"}
    capability=ScopedFourLayerMemoryEvidenceCapability(application=app,evidence_loader=loader,authority_loader=authority)
    result=capability.invoke({"arguments":{"grant_id":public["grant_id"]}})["result"]
    assert result["grant"]==public and result["baseline"]["grant_revision"]==public["revision"]
    assert "secret preview" not in str(result["baseline"])
    with pytest.raises(ValueError,match="unavailable"):
        ScopedFourLayerMemoryEvidenceCapability(application=SimpleNamespace(state=SimpleNamespace()),evidence_loader=loader,authority_loader=authority).invoke({"arguments":{"grant_id":public["grant_id"]}})
