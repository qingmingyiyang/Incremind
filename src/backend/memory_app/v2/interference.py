"""Confirmation-time usage boundary for superseded memories; no content revisions."""

COLLECTION = "v2_insight_interference"


def mark_interference(transaction, old_id, new_id, confirmed_at):
    usage = transaction.read("v2_usage_insight", old_id)
    previous = transaction.read(COLLECTION, old_id)
    transaction.put(
        COLLECTION,
        old_id,
        {
            "superseding_id": new_id,
            "confirmed_at": confirmed_at,
            "count_at_confirmation": usage.payload["count"] if usage else 1,
        },
        expected_revision=previous.revision if previous else 0,
    )


def recovery_allowed(marker, usage):
    """Spreading activation has count=False and cannot clear supersession interference."""
    count = usage.payload["count"] if usage else 1
    return marker is None or count > marker.payload["count_at_confirmation"]
