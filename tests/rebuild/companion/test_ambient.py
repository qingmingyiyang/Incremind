from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.companion_core import CompanionAmbientService, CompanionConflict, CompanionModelRouter, CompanionRepository, CompanionStateReducer

ROOT = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 22, 5, 0, tzinfo=timezone.utc)

    def now(self): return self.value
    def advance(self, minutes: int): self.value += timedelta(minutes=minutes)


def make_service(tmp_path, clock):
    repository = CompanionRepository.at_data_root(tmp_path, now=clock.now)
    reducer = CompanionStateReducer(repository, rules_path=ROOT / "config/companion/economy-rules.json", now=clock.now)
    return CompanionAmbientService(repository, rules=reducer.rules, now=clock.now, choice=lambda values: values[0]), reducer


def test_frequency_uses_next_at_and_quiet_modes_suppress(tmp_path) -> None:
    clock = Clock(); ambient, _ = make_service(tmp_path, clock)
    saved = ambient.configure(enabled=True, interval_minutes=30, idle_enabled=True, idle_minutes=20, expected_revision=0)
    assert saved["config"]["interval_minutes"] == 30
    assert ambient.offer(require_due=True)["reason"] == "not_due"
    clock.advance(30)
    assert ambient.offer(require_due=True, sleeping=True)["reason"] == "sleep"
    offered = ambient.offer(require_due=True)
    assert offered["event"]["scene"].startswith("散步时")
    assert len(offered["event"]["options"]) == 2
    assert ambient.offer(require_due=True)["replayed"] is True


def test_first_automatic_poll_initializes_sixty_minute_deadline(tmp_path) -> None:
    clock = Clock(); ambient, _ = make_service(tmp_path, clock)
    first = ambient.offer(require_due=True)
    assert first["reason"] == "not_due" and ambient.status()["event"] is None
    clock.advance(59)
    assert ambient.offer(require_due=True)["reason"] == "not_due"
    clock.advance(1)
    assert ambient.offer(require_due=True)["event"] is not None


def test_choice_settles_once_in_same_state_and_wallet_transaction(tmp_path) -> None:
    clock = Clock(); ambient, reducer = make_service(tmp_path, clock)
    event = ambient.offer(require_due=False)["event"]
    result = ambient.choose(event["event_id"], "hand_in", event["revision"])
    assert result["event"]["state"] == "settled"
    assert result["event"]["result"]["changes"] == {"affinity": 2, "mood": 2, "coins": 1}
    assert reducer.snapshot().coins == 1
    assert reducer.wallet(limit=5)[0].delta == 1
    replay = ambient.choose(event["event_id"], "hand_in", event["revision"])
    assert replay["replayed"] is True and reducer.snapshot().coins == 1
    with pytest.raises(CompanionConflict):
        ambient.choose(event["event_id"], "keep", event["revision"])


def test_positive_coin_reward_is_limited_to_four_events_per_day(tmp_path) -> None:
    clock = Clock(); ambient, reducer = make_service(tmp_path, clock)
    last = None
    for _ in range(5):
        event = ambient.offer(require_due=False)["event"]
        last = ambient.choose(event["event_id"], "hand_in", event["revision"])
    assert reducer.snapshot().coins == 4
    assert last["event"]["result"]["reward_limited"] is True


def test_idle_message_respects_threshold_quiet_and_contains_no_user_content(tmp_path) -> None:
    clock = Clock(); ambient, _ = make_service(tmp_path, clock)
    ambient.configure(enabled=True, interval_minutes=60, idle_enabled=True, idle_minutes=20, expected_revision=0)
    assert ambient.idle_message(idle_seconds=1199)["reason"] == "active"
    assert ambient.idle_message(idle_seconds=1200, quiet=True)["reason"] == "quiet"
    result = ambient.idle_message(idle_seconds=1200)
    assert result["active"] is True and "忙" in result["message"]
    assert set(result) == {"message", "reason", "active"}


class AmbientProvider:
    def __init__(self, text): self.text, self.requests = text, []
    def generate(self, request): self.requests.append(request); return {"text": self.text}


def test_provider_scene_uses_local_result_authority_and_invalid_output_falls_back(tmp_path) -> None:
    clock = Clock(); repository = CompanionRepository.at_data_root(tmp_path, now=clock.now)
    reducer = CompanionStateReducer(repository, rules_path=ROOT / "config/companion/economy-rules.json", now=clock.now)
    provider = AmbientProvider('{"scene":"午后窗边有一阵轻风，我们要怎样回应这份安静？","options":[{"label":"把窗帘拉开一点","result_template_id":"lost_coin:hand_in"},{"label":"留下纸箱小窝","result_template_id":"rainy_cat:shelter"}]}')
    ambient = CompanionAmbientService(repository, rules=reducer.rules, now=clock.now,
        model_router=CompanionModelRouter(provider=provider, provider_capabilities=("text_generation",), egress_consented=True, enabled_routes={"companion.ambient": True}))
    event = ambient.offer(require_due=False)["event"]
    assert event["scene"].startswith("午后窗边") and [item["id"] for item in event["options"]] == ["lost_coin:hand_in", "rainy_cat:shelter"]
    settled = ambient.choose(event["event_id"], "lost_coin:hand_in", event["revision"])
    assert settled["event"]["result"]["changes"] == {"affinity": 2, "mood": 2, "coins": 1}
    assert provider.requests and provider.requests[0]["route_key"] == "companion.ambient"
    request_text = str(provider.requests[0])
    assert "master_profile" not in request_text
    assert "聊天记录" not in request_text
    assert "温和、简洁、低打扰。" in request_text
    bad = AmbientProvider('{"scene":"坏输出","options":[]}')
    fallback = CompanionAmbientService(repository, rules=reducer.rules, now=clock.now, choice=lambda values: values[0],
        model_router=CompanionModelRouter(provider=bad, provider_capabilities=("text_generation",), egress_consented=True, enabled_routes={"companion.ambient": True}))
    # The existing offered event blocks a second offer; settle it first, then validate fallback on the next event.
    clock.advance(1)
    next_event = fallback.offer(require_due=False)["event"]
    assert next_event["scene"].startswith("散步时")


def test_ambient_quiet_suppression_makes_no_provider_request(tmp_path) -> None:
    clock = Clock(); repository = CompanionRepository.at_data_root(tmp_path, now=clock.now)
    reducer = CompanionStateReducer(repository, rules_path=ROOT / "config/companion/economy-rules.json", now=clock.now)
    provider = AmbientProvider('{"scene":"午后窗边有一阵轻风，我们要怎样回应这份安静？","options":[{"label":"把窗帘拉开一点","result_template_id":"lost_coin:hand_in"},{"label":"留下纸箱小窝","result_template_id":"rainy_cat:shelter"}]}')
    ambient = CompanionAmbientService(repository, rules=reducer.rules, now=clock.now,
        model_router=CompanionModelRouter(provider=provider, provider_capabilities=("text_generation",), egress_consented=True, enabled_routes={"companion.ambient": True}))
    assert ambient.offer(require_due=False, sleeping=True)["suppressed"] is True
    assert provider.requests == []


def test_ambient_disabled_or_not_due_makes_no_provider_request(tmp_path) -> None:
    clock = Clock(); repository = CompanionRepository.at_data_root(tmp_path, now=clock.now)
    reducer = CompanionStateReducer(repository, rules_path=ROOT / "config/companion/economy-rules.json", now=clock.now)
    provider = AmbientProvider('{"scene":"午后窗边有一阵轻风，我们要怎样回应这份安静？","options":[{"label":"把窗帘拉开一点","result_template_id":"lost_coin:hand_in"},{"label":"留下纸箱小窝","result_template_id":"rainy_cat:shelter"}]}')
    ambient = CompanionAmbientService(repository, rules=reducer.rules, now=clock.now,
        model_router=CompanionModelRouter(provider=provider, provider_capabilities=("text_generation",), egress_consented=True, enabled_routes={"companion.ambient": True}))
    assert ambient.offer(require_due=True)["reason"] == "not_due"
    assert provider.requests == []
    status = ambient.status()
    ambient.configure(enabled=False, interval_minutes=60, idle_enabled=True, idle_minutes=20, expected_revision=status["revision"])
    assert ambient.offer(require_due=False)["reason"] == "disabled"
    assert provider.requests == []


@pytest.mark.parametrize(
    ("capabilities", "consented", "enabled"),
    [
        ((), True, True),
        (("text_generation",), False, True),
        (("text_generation",), True, False),
    ],
)
def test_ambient_provider_gate_falls_back_locally_without_egress(tmp_path, capabilities, consented, enabled) -> None:
    clock = Clock(); repository = CompanionRepository.at_data_root(tmp_path, now=clock.now)
    reducer = CompanionStateReducer(repository, rules_path=ROOT / "config/companion/economy-rules.json", now=clock.now)
    provider = AmbientProvider('{"scene":"午后窗边有一阵轻风，我们要怎样回应这份安静？","options":[{"label":"把窗帘拉开一点","result_template_id":"lost_coin:hand_in"},{"label":"留下纸箱小窝","result_template_id":"rainy_cat:shelter"}]}')
    ambient = CompanionAmbientService(
        repository, rules=reducer.rules, now=clock.now, choice=lambda values: values[0],
        model_router=CompanionModelRouter(
            provider=provider, provider_capabilities=capabilities, egress_consented=consented,
            enabled_routes={"companion.ambient": enabled},
        ),
    )
    event = ambient.offer(require_due=False)["event"]
    assert event["scene"].startswith("散步时")
    assert provider.requests == []
