from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from creator_badges import (
    STUCK_PROGRESS_MINUTES,
    CreatorBadgeEvent,
    CreatorBadgeWatchReward,
    filter_excluded,
    has_incomplete_watch_rewards,
    is_stuck,
    parse_creator_badge_event,
    remaining_watch_minutes,
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
