from core.capability_packages.timeline_preview import timeline_preview


def test_timeline_preview_contract() -> None:
    assert timeline_preview(({"ref": "crp://event/1", "occurred_at": "1"},))[0]["ref"] == "crp://event/1"
