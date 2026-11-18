# Creator Badge Drops — Watch-Time via Follows

**Date:** 2026-11-18  
**Status:** approved  
**Repo:** TwitchDropsMiner (fork `vwtfafa`)

## Intent

Let the miner earn **Creator Badge Drop watch-time rewards** on channels the user follows, without touching sub/gift-sub rewards and without replacing normal game-drop mining.

Creator Badge Drops are channel-specific, time-boxed events (max 28 days). Watch-time badges (optional, 1–8 hours) only progress while watching **that** channel live. They are not game drop campaigns and do not appear in the usual campaign inventory the same way.

## Why

Users want channel Creator-Abzeichen that unlock by watching. The fork already handles **campaign** badge/emote drops; this is a different Twitch product surface.

## Goals

- Discover active Creator Badge events with a **watch-time** reward among followed channels.
- Mine those channels (send watch heartbeats) until the watch-time badge is earned or the event ends.
- Ignore sub/gift-only rewards entirely.
- Keep game-drop mining as the primary job unless the user opts otherwise.
- Stay within Twitch’s GQL/watch model already used by the miner (no media download).

## Non-goals

- Mining or prompting for sub/gift Creator Badge rewards.
- Global discovery of events on channels the user does **not** follow.
- Equipping badges in chat / badge UI management.
- Guaranteeing ownership detection if Twitch never exposes earned-state (same class of problem as campaign badges).
- Changing how campaign `BADGE`/`EMOTE` drops work (`fallback_badges_emotes` stays as-is).

## Anti-goals

- Interrupting an active finishable game campaign by default just to chase a Creator Badge.
- Spamming GQL by re-scanning every follow on every watch tick.
- Getting stuck forever on a channel whose watch badge cannot be detected as complete.

## Constraints

- Twitch GQL ops for Creator Badge **event discovery / progress** are not yet documented in this repo. Implementation starts with a **spike** to capture hashes from the Twitch web client (channel page / Drops tracker / inventory Rewards).
- Follows listing can use the known `ChannelFollows` persisted query (same pattern as Channel Points miners).
- Progress only counts on the event channel while it is live.
- Respect existing `channel_exclude`.
- No persistent “already owned” store unless Twitch itself reports earned/claimed; prefer Twitch-sourced state (project convention).

## Approaches considered

| Approach | Summary | Trade-off |
| --- | --- | --- |
| 1. Manual channel list | User enters logins | Controllable; misses automatic discovery |
| 2. Opportunistic | Progress only while already watching for game drops | Almost free; never seeks events |
| **3. Scan follows (chosen)** | Page `ChannelFollows`, filter live + watch-time event | Automatic for followed creators; needs GQL spike + scan budget |

## Design

### Settings

- `creator_badge_watch: bool` (default `False`) — master toggle.
- `creator_badge_priority: Literal["fallback", "interrupt"]` (default `"fallback"`):
  - **fallback:** only mine Creator Badge watch targets when there is no **finishable** wanted game channel to watch (same spirit as `fallback_badges_emotes`). Unfinishable leftover campaigns do **not** block Creator Badge mining.
  - **interrupt:** prefer an eligible Creator Badge watch target over game drops when one is available.
- Reuse `channel_exclude`; no separate exclude list in v1.

### Discovery loop

1. When the toggle is on, periodically fetch follows via `ChannelFollows` (paginated, hard cap e.g. 500 follows per refresh).
2. Refresh cadence: on inventory/reload cycles and at a slower interval (e.g. 30–60 min), not every watch heartbeat.
3. For each follow (batch GQL where possible): check live + active Creator Badge event with `requiredMinutesWatched > 0` and watch reward not already earned.
4. Build `creator_badge_targets: list[Channel]` (or lightweight structs) sorted by: soonest event end, then least remaining minutes.
5. Skip channels in `channel_exclude` and offline channels.

### Mining integration

- Extend channel selection so that when a Creator Badge target is eligible under the chosen priority mode, the miner watches that channel with the existing watch loop (playback token + periodic watch requests).
- Progress: use the spiked Creator Badge / drops-tracker GQL (or whatever the web client uses). Do **not** assume `DropCurrentSessionContext` works until the spike confirms it.
- Completion: stay on the channel until **all** incomplete watch-time rewards for that event are earned (e.g. 1h then 8h). Only then drop the channel from targets and move on.
- Stuck guard: if no progress for N minutes (reuse existing drop saturation / local estimate ideas), skip that event for the rest of the current event window or until next full refresh — builder picks exact constant.

### UI

- Settings checkbox: enable Creator Badge watch-time from follows.
- Optional small dropdown or second checkbox for interrupt vs fallback (keep wording plain).
- Status/log line when switching to a Creator Badge target (channel + remaining minutes if known).
- No new main tab in v1.

### Spike (blocking)

Before product code lands:

1. Capture GQL while opening a channel with an active Creator Badge event and the chat Drops tracker / Rewards inventory.
2. Record operation names, hashes, variables, and response shapes for: event presence, watch requirement, current progress, earned state.
3. Confirm whether award is automatic (no claim mutation) or needs a claim call.
4. If ops cannot be found or are unstable, stop and report; do not ship a half-wired toggle.

### Testing

- Unit tests with fixtures for: follow paging, filter watch-time vs sub-only, exclude list, fallback vs interrupt selection order.
- No live Twitch CI dependency; spike artifacts can live as checked-in sample JSON fixtures once captured.

## What’s left to the builder

- Exact GQL operation names/hashes (from spike).
- Exact refresh interval, follow cap, and stuck-guard minutes.
- Whether targets are full `Channel` objects or a thin parallel list.
- German/English settings strings.
- Whether interrupt mode needs a confirmation warning in the Advanced/settings area.

## Open for partner (resolved in conversation)

- Scope: **watch-time rewards only** — yes.
- Discovery: **scan follows** — yes.
- Priority default: **fallback**.
- Fallback vs unfinishable games: Creator Badge mining **may take over** when nothing finishable remains — yes.
- Multiple watch-time tiers on one event: stay and finish **all** incomplete watch rewards in sequence — yes.

## Builder calls (correct if wrong)

- Default priority mode: `fallback` (not interrupt).
- Follow refresh: on inventory/reload + ~30–60 min; hard cap ~500 follows per refresh.
- Stuck guard: skip event for current window / until next refresh if no Twitch progress for a fixed N minutes (constant left to builder).
- No new main Inventory tab in v1 — settings + status/log only.
- Spike must succeed before any product toggle ships.
