"""Version-bound source guard shared by candidate generation entries."""
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict


def generation_source_guard(egress, models, scope, refs):
    snapshot = egress.snapshot(scope, refs)
    configured = models.public()["generation"]
    requires_remote_permission = urlsplit(str(configured.get("base_url", ""))).hostname not in {
        "localhost", "127.0.0.1", "::1"}

    def validate():
        egress.validate_snapshot(scope, snapshot)
        if models.public()["generation"] != configured:
            raise RecognitionConflict("generation configuration changed during source authorization")
        if requires_remote_permission:
            egress.require(snapshot, "generation")

    validate()
    return validate
