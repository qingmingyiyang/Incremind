from __future__ import annotations

import json

import pytest

from backend.security.network_adapter import LoopbackHttpConnectProxy
from backend.security.network_egress_decision import NetworkEgressDecisionError, NetworkEgressDecisionStore


REVISION = "a" * 40


def test_direct_decision_is_default_immutable_and_replayable(tmp_path) -> None:
    store = NetworkEgressDecisionStore(tmp_path)
    first = store.decide(
        project_id="project-a", source_revision=REVISION,
        manifest_revision="runtime-v1", generation=0,
    )
    replay = store.decide(
        project_id="project-a", source_revision=REVISION,
        manifest_revision="runtime-v1", generation=0,
    )
    assert replay == first == store.get(first.decision_revision)
    assert first.connect_proxy() is None


def test_loopback_decision_requires_explicit_confirmation_and_stores_no_url_or_credentials(tmp_path) -> None:
    store = NetworkEgressDecisionStore(tmp_path)
    selection = {"mode": "loopback_http_connect", "literal_address": "127.0.0.1", "port": 7890}
    with pytest.raises(NetworkEgressDecisionError, match="explicit operation confirmation"):
        store.decide(project_id="project-a", source_revision=REVISION, manifest_revision="runtime-v1",
                     generation=0, network_egress=selection)
    fact = store.decide(project_id="project-a", source_revision=REVISION, manifest_revision="runtime-v1",
                        generation=0, network_egress=selection, confirm_loopback=True)
    assert fact.connect_proxy() == LoopbackHttpConnectProxy("127.0.0.1", 7890)
    payload = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "url" not in payload.lower() and "credential" not in payload.lower()


@pytest.mark.parametrize("address", ["8.8.8.8", "example.com"])
def test_decision_rejects_non_loopback_or_non_literal_endpoints(tmp_path, address) -> None:
    with pytest.raises(NetworkEgressDecisionError, match="loopback literal"):
        NetworkEgressDecisionStore(tmp_path).decide(
            project_id="project-a", source_revision=REVISION, manifest_revision="runtime-v1", generation=0,
            network_egress={"mode": "loopback_http_connect", "literal_address": address, "port": 7890},
            confirm_loopback=True,
        )


def test_tampered_decision_fails_closed(tmp_path) -> None:
    store = NetworkEgressDecisionStore(tmp_path)
    fact = store.decide(project_id="project-a", source_revision=REVISION,
                        manifest_revision="runtime-v1", generation=0)
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["mode"] = "loopback_http_connect"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NetworkEgressDecisionError, match="digest drifted"):
        store.get(fact.decision_revision)
