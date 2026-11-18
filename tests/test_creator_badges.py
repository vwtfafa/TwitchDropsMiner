from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creator_badges import (
    STUCK_PROGRESS_MINUTES,
    CreatorBadgeEvent,
    CreatorBadgeWatchReward,
    active_creator_badge_event_for_channel,
    apply_creator_badge_progress,
    event_watch_rewards_complete,
    filter_excluded,
    has_incomplete_watch_rewards,
    is_stuck,
    parse_creator_badge_event,
    remaining_watch_minutes,
    select_targets,
    should_mine_creator_badges,
    sort_creator_badge_targets,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHANNEL_ID = 111882197
CHANNEL_LOGIN = "ExampleCreator"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_active_event_keeps_only_watch_rewards():
    payload = _load("creator_badge_event_active.json")
    event = parse_creator_badge_event(
        payload, channel_id=CHANNEL_ID, channel_login=CHANNEL_LOGIN
    )
    assert event is not None
    assert event.channel_id == CHANNEL_ID
    assert event.channel_login == CHANNEL_LOGIN
    assert event.ends_at == datetime(2026, 11, 26, 8, 30, tzinfo=timezone.utc)
    assert len(event.watch_rewards) == 1
    reward = event.watch_rewards[0]
    assert reward.id == "3c7dec6b-d364-11f1-8010-0a58a9feac02"
    assert reward.name == "RIP Sweet Bella <3"
    assert reward.required_minutes is None
    assert reward.current_minutes == 0
    assert reward.is_earned is False
    assert has_incomplete_watch_rewards(event)
    assert remaining_watch_minutes(event) is None


def test_parse_sub_only_event_has_no_watch_targets():
    payload = _load("creator_badge_event_sub_only.json")
    event = parse_creator_badge_event(
        payload, channel_id=CHANNEL_ID, channel_login=CHANNEL_LOGIN
    )
    assert event is None or not has_incomplete_watch_rewards(event)
    if event is not None:
        assert event.watch_rewards == []


def test_remaining_minutes_sums_incomplete_tiers_only():
    ends = datetime(2026, 12, 1, tzinfo=timezone.utc)
    event = CreatorBadgeEvent(
        channel_id=1,
        channel_login="a",
        ends_at=ends,
        watch_rewards=[
            CreatorBadgeWatchReward("1", "done", 60, 60, True),
            CreatorBadgeWatchReward("2", "open", 120, 30, False),
            CreatorBadgeWatchReward("3", "open2", 40, 10, False),
        ],
    )
    assert remaining_watch_minutes(event) == 120  # (120-30) + (40-10)

    unknown = CreatorBadgeEvent(
        channel_id=1,
        channel_login="a",
        ends_at=ends,
        watch_rewards=[CreatorBadgeWatchReward("1", "open", None, 5, False)],
    )
    assert remaining_watch_minutes(unknown) is None


def test_sort_prefers_soonest_end_then_least_remaining():
    early = CreatorBadgeEvent(
        channel_id=1,
        channel_login="early",
        ends_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
        watch_rewards=[CreatorBadgeWatchReward("1", "a", 100, 0, False)],
    )
    late_less = CreatorBadgeEvent(
        channel_id=2,
        channel_login="late_less",
        ends_at=datetime(2026, 11, 25, tzinfo=timezone.utc),
        watch_rewards=[CreatorBadgeWatchReward("2", "b", 30, 0, False)],
    )
    late_more = CreatorBadgeEvent(
        channel_id=3,
        channel_login="late_more",
        ends_at=datetime(2026, 11, 25, tzinfo=timezone.utc),
        watch_rewards=[CreatorBadgeWatchReward("3", "c", 90, 0, False)],
    )
    unknown = CreatorBadgeEvent(
        channel_id=4,
        channel_login="unknown",
        ends_at=datetime(2026, 11, 25, tzinfo=timezone.utc),
        watch_rewards=[CreatorBadgeWatchReward("4", "d", None, 0, False)],
    )
    sorted_events = sort_creator_badge_targets(
        [late_more, unknown, early, late_less]
    )
    assert [e.channel_login for e in sorted_events][:1] == ["early"]
    # Same ends_at: known remaining ascending; None remaining does not invent a key
    same_end = [e for e in sorted_events if e.ends_at == late_less.ends_at]
    assert same_end[0].channel_login == "late_less"
    assert same_end[1].channel_login == "late_more"


def test_fallback_blocked_only_when_finishable_wanted_game():
    assert (
        should_mine_creator_badges(
            enabled=True, priority="fallback", has_finishable_wanted_game=True
        )
        is False
    )
    assert (
        should_mine_creator_badges(
            enabled=True, priority="fallback", has_finishable_wanted_game=False
        )
        is True
    )
    assert (
        should_mine_creator_badges(
            enabled=False, priority="fallback", has_finishable_wanted_game=False
        )
        is False
    )


def test_interrupt_mines_even_with_finishable_game():
    assert (
        should_mine_creator_badges(
            enabled=True, priority="interrupt", has_finishable_wanted_game=True
        )
        is True
    )
    assert (
        should_mine_creator_badges(
            enabled=False, priority="interrupt", has_finishable_wanted_game=False
        )
        is False
    )


def test_filter_excluded_casefold():
    events = [
        CreatorBadgeEvent(
            1,
            "FooBar",
            datetime(2026, 12, 1, tzinfo=timezone.utc),
            [CreatorBadgeWatchReward("1", "a", None, 0, False)],
        ),
        CreatorBadgeEvent(
            2,
            "keepme",
            datetime(2026, 12, 1, tzinfo=timezone.utc),
            [CreatorBadgeWatchReward("2", "b", None, 0, False)],
        ),
    ]
    kept = filter_excluded(events, {"foobar"})
    assert [e.channel_login for e in kept] == ["keepme"]


def test_stuck_after_fifteen_minutes_without_progress():
    assert STUCK_PROGRESS_MINUTES == 15
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert is_stuck(last_progress_at=now - timedelta(minutes=15), now=now) is True
    assert is_stuck(last_progress_at=now - timedelta(minutes=14), now=now) is False


def test_default_creator_badge_settings_off_and_fallback():
    from settings import default_settings
    assert default_settings["creator_badge_watch"] is False
    assert default_settings["creator_badge_priority"] == "fallback"


def test_creator_badge_owned_campaigns_query_is_full_body_not_persisted():
    from constants import creator_badge_owned_campaigns_query

    op = creator_badge_owned_campaigns_query(["111882197"])
    assert op["operationName"] == "CreatorBadgeOwnedCampaigns"
    assert "channelOwnedDropCampaigns" in op["query"]
    assert op["variables"] == {"channelIDs": ["111882197"]}
    assert "extensions" not in op


def test_parse_maps_login_from_follows_not_payload():
    """Fixtures only expose channel.id; login must come from ChannelFollows map."""
    payload = _load("creator_badge_event_active.json")
    channel = payload["data"]["channelOwnedDropCampaigns"][0]["channel"]
    assert set(channel.keys()) == {"id"}
    event = parse_creator_badge_event(
        payload, channel_id=int(channel["id"]), channel_login="FromFollows"
    )
    assert event is not None
    assert event.channel_login == "FromFollows"


def test_parse_skips_non_active_campaigns_and_expired_watch_tiers():
    payload = _load("creator_badge_event_active.json")
    campaign = payload["data"]["channelOwnedDropCampaigns"][0]["campaigns"][0]
    campaign["status"] = "EXPIRED"
    assert (
        parse_creator_badge_event(
            payload, channel_id=CHANNEL_ID, channel_login=CHANNEL_LOGIN
        )
        is None
    )

    payload = _load("creator_badge_event_active.json")
    for group in payload["data"]["channelOwnedDropCampaigns"][0]["campaigns"][0][
        "rewardGroups"
    ]:
        criteria = group.get("progressCriteria") or {}
        if criteria.get("requirementType") == "WATCH":
            group["self"]["status"] = "EXPIRED"
    assert (
        parse_creator_badge_event(
            payload, channel_id=CHANNEL_ID, channel_login=CHANNEL_LOGIN
        )
        is None
    )


def test_select_targets_applies_exclude_incomplete_and_sort():
    ends_early = datetime(2026, 11, 20, tzinfo=timezone.utc)
    ends_late = datetime(2026, 11, 25, tzinfo=timezone.utc)
    incomplete_early = CreatorBadgeEvent(
        1,
        "keep_early",
        ends_early,
        [CreatorBadgeWatchReward("1", "a", 60, 0, False)],
    )
    incomplete_late = CreatorBadgeEvent(
        2,
        "keep_late",
        ends_late,
        [CreatorBadgeWatchReward("2", "b", 60, 0, False)],
    )
    complete = CreatorBadgeEvent(
        3,
        "done_chan",
        ends_early,
        [CreatorBadgeWatchReward("3", "c", 60, 60, True)],
    )
    excluded = CreatorBadgeEvent(
        4,
        "Nope",
        ends_early,
        [CreatorBadgeWatchReward("4", "d", 60, 0, False)],
    )
    selected = select_targets(
        [incomplete_late, complete, excluded, incomplete_early],
        exclude={"nope"},
        enabled=True,
        priority="fallback",
        has_finishable=False,
    )
    assert [e.channel_login for e in selected] == ["keep_early", "keep_late"]
    assert select_targets(
        [incomplete_early],
        exclude=set(),
        enabled=False,
        priority="fallback",
        has_finishable=False,
    ) == []


def test_disable_clears_events_and_refresh_timestamp():
    """Re-enable within 45 min must not hit a stale gate returning []."""
    import asyncio
    from types import SimpleNamespace

    import pytest

    pytest.importorskip("pystray")
    from twitch import Twitch

    class _FakeGUI:
        def __init__(self) -> None:
            self.status = SimpleNamespace(update=lambda *_: None)
            self.tray = SimpleNamespace(change_icon=lambda *_: None)

        def set_state_hint(self, hint: str) -> None:
            pass

    twitch = Twitch(
        SimpleNamespace(creator_badge_watch=False),
        gui=_FakeGUI(),
    )
    kept = CreatorBadgeEvent(
        1,
        "x",
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("1", "a", None, 0, False)],
    )
    twitch._creator_badge_events = [kept]
    twitch._creator_badge_events_at = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    result = asyncio.run(twitch.fetch_creator_badge_targets())
    assert result == []
    assert twitch._creator_badge_events == []
    assert twitch._creator_badge_events_at is None


def test_active_event_for_channel_matches_login():
    ends = datetime(2026, 12, 1, tzinfo=timezone.utc)
    incomplete = CreatorBadgeEvent(
        1,
        "FooBar",
        ends,
        [CreatorBadgeWatchReward("1", "a", None, 0, False)],
    )
    complete = CreatorBadgeEvent(
        2,
        "other",
        ends,
        [CreatorBadgeWatchReward("2", "b", None, 10, True)],
    )
    events = [complete, incomplete]
    assert active_creator_badge_event_for_channel(events, channel_login="foobar") is incomplete
    assert active_creator_badge_event_for_channel(events, channel_login="other") is None
    assert active_creator_badge_event_for_channel(events, channel_login="missing") is None


def test_event_complete_when_all_watch_rewards_earned():
    ends = datetime(2026, 12, 1, tzinfo=timezone.utc)
    done = CreatorBadgeEvent(
        1,
        "a",
        ends,
        [
            CreatorBadgeWatchReward("1", "t1", 60, 60, True),
            CreatorBadgeWatchReward("2", "t2", 120, 120, True),
        ],
    )
    open_tier = CreatorBadgeEvent(
        1,
        "a",
        ends,
        [
            CreatorBadgeWatchReward("1", "t1", 60, 60, True),
            CreatorBadgeWatchReward("2", "t2", 120, 30, False),
        ],
    )
    empty = CreatorBadgeEvent(1, "a", ends, [])
    assert event_watch_rewards_complete(done) is True
    assert event_watch_rewards_complete(open_tier) is False
    assert event_watch_rewards_complete(empty) is False


def test_apply_progress_updates_minutes_and_detects_earn():
    payload = _load("creator_badge_event_active.json")
    event = parse_creator_badge_event(
        payload, channel_id=CHANNEL_ID, channel_login=CHANNEL_LOGIN
    )
    assert event is not None
    assert event.watch_rewards[0].current_minutes == 0
    assert apply_creator_badge_progress(event, payload) is False

    group = payload["data"]["channelOwnedDropCampaigns"][0]["campaigns"][0]["rewardGroups"]
    for g in group:
        criteria = g.get("progressCriteria") or {}
        if criteria.get("requirementType") == "WATCH":
            g["self"]["currentMinutesWatched"] = 12
            g["self"]["status"] = "IN_PROGRESS"
    assert apply_creator_badge_progress(event, payload) is True
    assert event.watch_rewards[0].current_minutes == 12
    assert event.watch_rewards[0].is_earned is False

    for g in group:
        criteria = g.get("progressCriteria") or {}
        if criteria.get("requirementType") == "WATCH":
            g["self"]["currentMinutesWatched"] = 12
            g["self"]["status"] = "CLAIMABLE"
    assert apply_creator_badge_progress(event, payload) is True
    assert event_watch_rewards_complete(event) is True


def test_safe_refresh_keeps_prior_events_on_failure():
    import asyncio
    from types import SimpleNamespace

    import pytest

    pytest.importorskip("pystray")
    from twitch import Twitch

    class _FakeGUI:
        def __init__(self) -> None:
            self.status = SimpleNamespace(update=lambda *_: None)
            self.tray = SimpleNamespace(change_icon=lambda *_: None)

        def set_state_hint(self, hint: str) -> None:
            pass

    twitch = Twitch(
        SimpleNamespace(creator_badge_watch=True),
        gui=_FakeGUI(),
    )
    prior = CreatorBadgeEvent(
        1,
        "prior",
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("1", "a", None, 0, False)],
    )
    twitch._creator_badge_events = [prior]
    twitch._creator_badge_events_at = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)

    async def boom(*, force: bool = False):
        raise RuntimeError("gql down")

    twitch.fetch_creator_badge_targets = boom  # type: ignore[method-assign]
    asyncio.run(twitch._refresh_creator_badge_targets_safe())
    assert twitch._creator_badge_events == [prior]


def _twitch_for_badge_progress(**settings_kwargs):
    import pytest
    from types import SimpleNamespace

    pytest.importorskip("pystray")
    from twitch import Twitch

    class _FakeGUI:
        def __init__(self) -> None:
            self.status = SimpleNamespace(update=lambda *_: None)
            self.tray = SimpleNamespace(change_icon=lambda *_: None)
            self.channels = SimpleNamespace(
                set_watching=lambda *_: None,
                clear_watching=lambda: None,
            )

        def set_state_hint(self, hint: str) -> None:
            pass

        def clear_drop(self) -> None:
            pass

        def print(self, message: str) -> None:
            pass

    defaults = {
        "creator_badge_watch": True,
        "creator_badge_priority": "fallback",
        "channel_exclude": set(),
    }
    defaults.update(settings_kwargs)
    return Twitch(SimpleNamespace(**defaults), gui=_FakeGUI())


def test_fallback_dual_purpose_channel_does_not_stop_watching():
    """Badge mining disallowed but channel still earnable for a game → no thrash."""
    import asyncio
    from types import SimpleNamespace

    from translate import _

    twitch = _twitch_for_badge_progress()
    event = CreatorBadgeEvent(
        42,
        "dual",
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("1", "a", None, 0, False)],
    )
    twitch._creator_badge_events = [event]
    twitch.has_finishable_wanted_game = lambda: True  # type: ignore[method-assign]
    channel = SimpleNamespace(id=42, login="dual", name="Dual")
    twitch._can_watch_for_game_campaign = lambda ch: True  # type: ignore[method-assign]

    status_updates: list[str] = []
    twitch.gui.status.update = lambda text: status_updates.append(text)  # type: ignore[method-assign]
    stopped: list[bool] = []
    requested: list[object] = []
    twitch.stop_watching = lambda: stopped.append(True)  # type: ignore[method-assign]
    twitch.request_state = lambda state: requested.append(state)  # type: ignore[method-assign]

    asyncio.run(twitch._update_creator_badge_progress(channel))  # type: ignore[arg-type]
    assert stopped == []
    assert requested == []
    assert twitch._creator_badge_events == [event]
    expected = _("status", "watching").format(channel="Dual")
    assert status_updates == [expected]
    assert "Creator Badge" not in expected


def test_fallback_badge_only_channel_stops_when_mining_disallowed():
    import asyncio
    from types import SimpleNamespace

    from constants import State

    twitch = _twitch_for_badge_progress()
    event = CreatorBadgeEvent(
        7,
        "badgeonly",
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("1", "a", None, 0, False)],
    )
    twitch._creator_badge_events = [event]
    twitch.has_finishable_wanted_game = lambda: True  # type: ignore[method-assign]
    channel = SimpleNamespace(id=7, login="badgeonly", name="BadgeOnly")
    twitch._can_watch_for_game_campaign = lambda ch: False  # type: ignore[method-assign]

    stopped: list[bool] = []
    requested: list[object] = []
    twitch.stop_watching = lambda: stopped.append(True)  # type: ignore[method-assign]
    twitch.request_state = lambda state: requested.append(state)  # type: ignore[method-assign]

    asyncio.run(twitch._update_creator_badge_progress(channel))  # type: ignore[arg-type]
    assert stopped == [True]
    assert requested == [State.CHANNEL_SWITCH]


def test_progress_gql_failure_still_applies_stuck_guard():
    import asyncio
    from types import SimpleNamespace

    from constants import State

    twitch = _twitch_for_badge_progress()
    event = CreatorBadgeEvent(
        9,
        "stuckchan",
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("1", "a", None, 0, False)],
    )
    twitch._creator_badge_events = [event]
    twitch.has_finishable_wanted_game = lambda: False  # type: ignore[method-assign]
    twitch._creator_badge_progress_at[9] = datetime.now(timezone.utc) - timedelta(
        minutes=STUCK_PROGRESS_MINUTES
    )
    channel = SimpleNamespace(id=9, login="stuckchan", name="Stuck")

    async def boom(_op):
        raise RuntimeError("gql down")

    twitch.gql_request = boom  # type: ignore[method-assign]
    stopped: list[bool] = []
    requested: list[object] = []
    twitch.stop_watching = lambda: stopped.append(True)  # type: ignore[method-assign]
    twitch.request_state = lambda state: requested.append(state)  # type: ignore[method-assign]

    asyncio.run(twitch._update_creator_badge_progress(channel))  # type: ignore[arg-type]
    assert twitch._creator_badge_events == []
    assert stopped == [True]
    assert requested == [State.CHANNEL_SWITCH]


def test_get_priority_orders_badge_targets_by_discovery_list():
    from types import SimpleNamespace

    twitch = _twitch_for_badge_progress()
    early = CreatorBadgeEvent(
        1,
        "early",
        datetime(2026, 11, 20, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("1", "a", 30, 0, False)],
    )
    late = CreatorBadgeEvent(
        2,
        "late",
        datetime(2026, 11, 25, tzinfo=timezone.utc),
        [CreatorBadgeWatchReward("2", "b", 30, 0, False)],
    )
    twitch._creator_badge_events = [early, late]
    twitch.has_finishable_wanted_game = lambda: False  # type: ignore[method-assign]

    ch_early = SimpleNamespace(login="early", game=None)
    ch_late = SimpleNamespace(login="late", game=None)
    assert twitch.get_priority(ch_early) < twitch.get_priority(ch_late)  # type: ignore[arg-type]
    assert twitch.get_priority(ch_early) < 0  # type: ignore[arg-type]
