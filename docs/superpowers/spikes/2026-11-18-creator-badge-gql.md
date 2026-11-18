# Spike: Creator Badge GQL (watch-time)

**Date:** 2026-11-18  
**Status:** usable discovery + progress/earned fields found; **required-minutes field absent** from live schema; progress query has **no registered persisted hash**

## Summary

Creator Badge events are `DropsCampaign` rows with `brandName == "CREATOR_LED_BADGE_CAMPAIGN"`, fetched via root field `channelOwnedDropCampaigns(channelIDs:)`.

Watch vs sub/gift is **not** `distributionType` (always `BADGE` in samples). Filter with:

- `rewardGroups[].progressCriteria.requirementType == "WATCH"` → watch-time tier  
- `requirementType == "SUB"` → sub/gift-style tier (ignore for mining)

Progress / earned state live on `rewardGroups[].self`:

- `status`: `IN_PROGRESS` | `CLAIMABLE` | `CLAIMED` | `FULFILLED` | `EXPIRED` (UI also uses these)
- `currentMinutesWatched`: Int or null (null in all non-watching samples)
- `grantCount`: Int

**Not available:** any `requiredMinutes` / `requiredMinutesWatched` / `minuteWatchedGoal` on `DropsProgressCriteria`, `DropsRewardGroup`, or `DropsReward` (exhaustive live probes). Sorting by remaining minutes is blocked until Twitch exposes a goal field or another op is found.

`DropCurrentSessionContext` tracks **game/quest** drops, not Creator Badge campaign IDs (verified against live badge channels).

`isAutoProgressable: true` on samples → treat award as automatic; **no Creator Badge claim mutation** found in client assets.

## Ops table

| Key | operationName | sha256Hash | Variables | Fixture field map |
| --- | --- | --- | --- | --- |
| `ChannelFollows` | `ChannelFollows` | `eecf815273d3d949e5cf0085cc5084cd8a1b5b7b6f7990cf43cb0beadf546907` | `limit`, `order` (`DESC`); optional `cursor` | Follow discovery only |
| `SideNavCreatorBadgeDrops` | `SideNavCreatorBadgeDrops` | `132c6783c8383048c1c74fb52c49a996de8c000a6084fa817f2cbc5453a9f643` | `channelIDs: [ID!]!` | Event presence; `campaigns[].endAt`; `rewardGroups.rewards.distributionType` only (no progress) |
| `CreatorBadgeOwnedCampaigns` | *(full query)* `CreatorBadgeOwnedCampaigns` | **none registered** (`PersistedQueryNotFound` for computed printed/compact hashes). Send **full query body**. | `channelIDs: [ID!]!` | See field map below |

### `CreatorBadgeOwnedCampaigns` field map (fixtures)

| Concern | Path |
| --- | --- |
| ends_at | `campaigns[].endAt` |
| watch vs sub | `rewardGroups[].progressCriteria.requirementType` (`WATCH` / `SUB`) |
| progress | `rewardGroups[].self.currentMinutesWatched` |
| earned / complete | `rewardGroups[].self.status` ∈ {`CLAIMABLE`,`CLAIMED`,`FULFILLED`} (exact completion rule TBD in product) |
| required minutes | **missing from schema** |
| brand filter | `campaigns[].brandName == "CREATOR_LED_BADGE_CAMPAIGN"` |

### Full query (verified live 2026-11-18)

```graphql
query CreatorBadgeOwnedCampaigns($channelIDs: [ID!]!) {
  channelOwnedDropCampaigns(channelIDs: $channelIDs) {
    channel { id }
    campaigns {
      id brandName status startAt endAt detailsURL imageURL description
      rewardGroups {
        id name __typename lane
        self { status currentMinutesWatched grantCount __typename }
        progressCriteria {
          __typename requirementType isAutoProgressable isRepeatable
          channels { id }
          categories { id }
          repeatableConfig { repeatableTimes shouldCompleteAllRepeatsToEarn }
        }
        rewards { id name distributionType isAccountConnected accountLinkURL __typename }
      }
    }
  }
}
```

Computed sha256 of printed query (not Twitch-persisted):  
`87c5a377605a2d09152032942a198401b37d53e11010f94f325a1ca84cf5d77e`

## Fixtures

| File | Nature |
| --- | --- |
| `tests/fixtures/creator_badge_event_active.json` | **Real** response (`emilycc` / `111882197`) with WATCH + SUB groups |
| `tests/fixtures/creator_badge_event_sub_only.json` | **Filtered real** response: same campaign, WATCH groups removed (no live SUB-only-only event among follows). Labeled in `_comment` |

Live channels with active Creator Badge among follows (2026-11-18): `emilycc`, `jasontheween`, `lacy`, `gamestar`.

## Client mining notes

- Side-nav op extracted from Twitch `core-*.js` Document AST; hash verified with persistedQuery.
- JS chunk `33267-*.js` has inventory UI helpers (`progressCriteria`, `CREATOR_LED_BADGE_CAMPAIGN`) but **no** GraphQL Document selecting `progressCriteria` / required minutes.
- No Document AST in downloaded inventory/channel assets requests `requiredMinutes*` for Creator Badge.
- PubSub topic name `user-creator-campaign-events` exists in client topic maps; not verified for watch progress payloads.

## constants.py

Added persisted stubs: `ChannelFollows`, `SideNavCreatorBadgeDrops` only.  
Did **not** add a fake persisted hash for `CreatorBadgeOwnedCampaigns`.

## Gate implication

Product wiring can discover WATCH tiers and poll `self.status` / `currentMinutesWatched`, but **cannot** read required minutes or sort by remaining time until a goal field/op is found. Recommend Tasks 2–6 proceed only with that limitation accepted, or pause for another capture while a drops-tracker UI with visible “Xh watch” is open.
