from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from utils import timestamp

STUCK_PROGRESS_MINUTES = 15

_EARNED_STATUSES = frozenset({"CLAIMABLE", "CLAIMED", "FULFILLED"})
_CREATOR_BADGE_BRAND = "CREATOR_LED_BADGE_CAMPAIGN"


@dataclass(slots=True)
class CreatorBadgeWatchReward:
    id: str
    name: str
    required_minutes: int | None
    current_minutes: int
    is_earned: bool


@dataclass(slots=True)
class CreatorBadgeEvent:
    channel_id: int
    channel_login: str
    ends_at: datetime
    watch_rewards: list[CreatorBadgeWatchReward]


def _reward_name(group: dict[str, Any]) -> str:
    group_name = (group.get("name") or "").strip()
    if group_name:
        return group_name
    rewards = group.get("rewards") or []
    if rewards:
        return str(rewards[0].get("name") or "")
    return ""


def _parse_watch_reward(group: dict[str, Any]) -> CreatorBadgeWatchReward:
    self_edge = group.get("self") or {}
    status = self_edge.get("status")
    current = self_edge.get("currentMinutesWatched")
    return CreatorBadgeWatchReward(
        id=str(group["id"]),
        name=_reward_name(group),
        required_minutes=None,
        current_minutes=int(current) if current is not None else 0,
        is_earned=status in _EARNED_STATUSES,
    )


def parse_creator_badge_event(
    payload: dict[str, Any],
    *,
    channel_id: int,
    channel_login: str,
) -> CreatorBadgeEvent | None:
    owned = (payload.get("data") or {}).get("channelOwnedDropCampaigns") or []
    watch_rewards: list[CreatorBadgeWatchReward] = []
    ends_at: datetime | None = None

    for entry in owned:
        for campaign in entry.get("campaigns") or []:
            if campaign.get("brandName") != _CREATOR_BADGE_BRAND:
                continue
            # Prefer ACTIVE campaigns only (EXPIRED/UPCOMING are not mineable).
            if campaign.get("status") != "ACTIVE":
                continue
            campaign_watch: list[CreatorBadgeWatchReward] = []
            for group in campaign.get("rewardGroups") or []:
                criteria = group.get("progressCriteria") or {}
                if criteria.get("requirementType") != "WATCH":
                    continue
                self_edge = group.get("self") or {}
                # EXPIRED tiers are non-mineable even when requirementType is WATCH.
                if self_edge.get("status") == "EXPIRED":
                    continue
                campaign_watch.append(_parse_watch_reward(group))
            if not campaign_watch:
                continue
            campaign_end = timestamp(campaign["endAt"])
            if ends_at is None or campaign_end < ends_at:
                ends_at = campaign_end
            watch_rewards.extend(campaign_watch)

    if not watch_rewards or ends_at is None:
        return None

    return CreatorBadgeEvent(
        channel_id=channel_id,
        channel_login=channel_login,
        ends_at=ends_at,
        watch_rewards=watch_rewards,
    )


def has_incomplete_watch_rewards(event: CreatorBadgeEvent) -> bool:
    return any(not reward.is_earned for reward in event.watch_rewards)


def remaining_watch_minutes(event: CreatorBadgeEvent) -> int | None:
    total = 0
    for reward in event.watch_rewards:
        if reward.is_earned:
            continue
        if reward.required_minutes is None:
            return None
        total += max(0, reward.required_minutes - reward.current_minutes)
    return total


def sort_creator_badge_targets(
    events: list[CreatorBadgeEvent],
) -> list[CreatorBadgeEvent]:
    def sort_key(event: CreatorBadgeEvent) -> tuple:
        remaining = remaining_watch_minutes(event)
        # None remaining: secondary key ignored (sort only by ends_at among unknowns)
        secondary = remaining if remaining is not None else float("inf")
        return (event.ends_at, secondary)

    return sorted(events, key=sort_key)


def should_mine_creator_badges(
    *,
    enabled: bool,
    priority: str,
    has_finishable_wanted_game: bool,
) -> bool:
    if not enabled:
        return False
    if priority == "interrupt":
        return True
    if priority == "fallback":
        return not has_finishable_wanted_game
    return False


def filter_excluded(
    events: list[CreatorBadgeEvent],
    channel_exclude: set[str],
) -> list[CreatorBadgeEvent]:
    excluded = {name.casefold() for name in channel_exclude}
    return [e for e in events if e.channel_login.casefold() not in excluded]


def select_targets(
    events: list[CreatorBadgeEvent],
    *,
    exclude: set[str],
    enabled: bool,
    priority: str,
    has_finishable: bool,
) -> list[CreatorBadgeEvent]:
    # Mining interrupt/fallback is applied at can_watch time; discovery only needs
    # the master toggle. Keep priority/has_finishable in the signature for callers.
    if not enabled:
        return []
    _ = (priority, has_finishable)
    filtered = filter_excluded(events, exclude)
    incomplete = [e for e in filtered if has_incomplete_watch_rewards(e)]
    return sort_creator_badge_targets(incomplete)


def is_stuck(*, last_progress_at: datetime, now: datetime) -> bool:
    return now - last_progress_at >= timedelta(minutes=STUCK_PROGRESS_MINUTES)


def active_creator_badge_event_for_channel(
    events: list[CreatorBadgeEvent],
    *,
    channel_login: str,
) -> CreatorBadgeEvent | None:
    """Return the incomplete event whose login matches ``channel_login`` (casefold)."""
    login = channel_login.casefold()
    for event in events:
        if event.channel_login.casefold() != login:
            continue
        if has_incomplete_watch_rewards(event):
            return event
    return None


def event_watch_rewards_complete(event: CreatorBadgeEvent) -> bool:
    """True when the event has watch rewards and every one is earned."""
    return bool(event.watch_rewards) and not has_incomplete_watch_rewards(event)


def apply_creator_badge_progress(
    event: CreatorBadgeEvent,
    payload: dict[str, Any],
) -> bool:
    """
    Merge a CreatorBadgeOwnedCampaigns response into ``event``.

    Returns True if any tier's ``current_minutes`` increased or became earned.
    """
    fresh = parse_creator_badge_event(
        payload,
        channel_id=event.channel_id,
        channel_login=event.channel_login,
    )
    if fresh is None:
        return False
    old_minutes = {r.id: r.current_minutes for r in event.watch_rewards}
    old_earned = {r.id: r.is_earned for r in event.watch_rewards}
    event.ends_at = fresh.ends_at
    event.watch_rewards = fresh.watch_rewards
    progressed = False
    for reward in event.watch_rewards:
        prev_m = old_minutes.get(reward.id, 0)
        prev_e = old_earned.get(reward.id, False)
        if reward.current_minutes > prev_m or (reward.is_earned and not prev_e):
            progressed = True
    return progressed
