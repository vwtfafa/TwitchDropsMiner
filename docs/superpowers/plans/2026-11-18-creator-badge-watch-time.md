# Creator Badge Watch-Time (Follows) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mine Creator Badge Drop watch-time rewards on followed channels, without sub/gift rewards and without blocking finishable game-drop mining by default.

**Architecture:** Add a small `creator_badges` domain module (parse events, pick targets, stuck-guard). Discover candidates by paging `ChannelFollows` and batch-querying Creator Badge GQL (hashes from a blocking spike). Integrate into `Twitch.can_watch` / channel selection with `fallback` (default: only when no finishable wanted game remains) or `interrupt` priority. Stay on a channel until every incomplete watch-time reward for that event is done.

**Tech Stack:** Python 3.12, existing aiohttp GQL client in `twitch.py`, Tk settings UI, pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-creator-badge-watch-time-design.md`

## Global Constraints

- Watch-time Creator Badge rewards only; never select or progress sub/gift-only rewards.
- Discovery = followed channels only (`ChannelFollows`), not a global directory.
- Default priority = `fallback`; unfinishable leftover game campaigns do **not** block Creator Badge mining.
- On one event, finish **all** incomplete watch-time tiers before leaving the channel.
- Respect `channel_exclude` (casefold login).
- No persistent local “already owned” store; use Twitch-reported earned/progress state.
- Do not ship a working toggle until the GQL spike produces real operation hashes and sample fixtures.
- Keep campaign `fallback_badges_emotes` behavior unchanged.

## Review Focus

- Follow count above the hard cap (~500): miner still starts and only uses the first page budget, no hang.
- Channel goes offline mid-event: stop watching it and pick the next eligible Creator Badge target or game channel.
- Event has only a sub/gift reward: never becomes a watch target.
- Finishable wanted game campaign becomes available while in `fallback` Creator Badge mining: switch back to game mining.
- Event ends (or watch rewards all earned) while watching: leave the channel and refresh targets.

---

## File structure

| File | Responsibility |
| --- | --- |
| `creator_badges.py` | Pure domain: event/reward models, parse fixtures, filter watch-time, sort targets, stuck-guard helper, selection predicate vs finishable games |
| `tests/test_creator_badges.py` | Unit tests for domain + selection |
| `tests/fixtures/creator_badge_*.json` | Captured GQL payloads from spike |
| `constants.py` | New `GQL_QUERIES` entries (`ChannelFollows` + Creator Badge ops from spike) |
| `settings.py` | `creator_badge_watch: bool`, `creator_badge_priority: str` |
| `gui.py` / `translate.py` / `lang/English.json` / `lang/Deutsch.json` | Settings controls + strings |
| `twitch.py` | Follows fetch, event refresh, extend `can_watch` / selection / progress in watch loop |

---

### Task 1: GQL spike + fixtures (blocking)

**Files:**
- Create: `tests/fixtures/creator_badge_event_active.json`
- Create: `tests/fixtures/creator_badge_event_sub_only.json`
- Create: `docs/superpowers/spikes/2026-11-18-creator-badge-gql.md` (ops table)
- Modify: `constants.py` (only after hashes known)

**Interfaces:**
- Consumes: Twitch web client network tab while a Creator Badge event with watch-time is live
- Produces: documented `operationName`, `sha256Hash`, variables; two JSON fixtures; stub entries ready for `GQL_QUERIES`

- [ ] **Step 1: Capture GQL**

Open a channel with an active Creator Badge watch-time event (and one sub-only if available). Record ops for: follows list (confirm `ChannelFollows`), event presence on channel, watch progress, earned state, claim mutation if any.

- [ ] **Step 2: Write spike notes**

In `docs/superpowers/spikes/2026-11-18-creator-badge-gql.md`, table: operation key, name, hash, variables, which fixture field maps to progress / required minutes / earned / ends_at.

- [ ] **Step 3: Save fixtures**

Sanitize and save real responses as the two fixture files above (active multi-tier watch if possible; sub-only control).

- [ ] **Step 4: Add GQL stubs to `constants.py`**

Add `ChannelFollows` plus Creator Badge query/mutation entries using the spike hashes. Variables marked with `...` where required.

- [ ] **Step 5: Stop gate**

If no usable watch progress op exists, stop and report; do not continue Tasks 2–6 product wiring.

- [ ] **Step 6: Commit**

```bash
git add constants.py tests/fixtures/creator_badge_*.json docs/superpowers/spikes/2026-11-18-creator-badge-gql.md
git commit -m "chore: capture Creator Badge GQL ops and fixtures"
```

---

### Task 2: Domain model + selection helpers

**Files:**
- Create: `creator_badges.py`
- Create: `tests/test_creator_badges.py`
- Test fixtures from Task 1

**Interfaces:**
- Consumes: fixture JSON shape from spike notes
- Produces:
  - `@dataclass class CreatorBadgeWatchReward` with `id: str`, `name: str`, `required_minutes: int`, `current_minutes: int`, `is_earned: bool`
  - `@dataclass class CreatorBadgeEvent` with `channel_id: int`, `channel_login: str`, `ends_at: datetime`, `watch_rewards: list[CreatorBadgeWatchReward]`
  - `def parse_creator_badge_event(payload: dict[str, Any], *, channel_id: int, channel_login: str) -> CreatorBadgeEvent | None`
  - `def has_incomplete_watch_rewards(event: CreatorBadgeEvent) -> bool`
  - `def remaining_watch_minutes(event: CreatorBadgeEvent) -> int` — sum of remaining minutes on incomplete watch rewards only
  - `def sort_creator_badge_targets(events: list[CreatorBadgeEvent]) -> list[CreatorBadgeEvent]` — soonest `ends_at`, then least `remaining_watch_minutes`
  - `def should_mine_creator_badges(*, enabled: bool, priority: str, has_finishable_wanted_game: bool) -> bool` — `interrupt` ⇒ enabled; `fallback` ⇒ enabled and not `has_finishable_wanted_game`
  - `def filter_excluded(events: list[CreatorBadgeEvent], channel_exclude: set[str]) -> list[CreatorBadgeEvent]`
  - `STUCK_PROGRESS_MINUTES = 15` and `def is_stuck(*, last_progress_at: datetime, now: datetime) -> bool`

- [ ] **Step 1: Write failing tests**

```python
def test_parse_active_event_keeps_only_watch_rewards():
    ...
    assert has_incomplete_watch_rewards(event)
    assert all(r.required_minutes > 0 for r in event.watch_rewards)

def test_parse_sub_only_event_has_no_watch_targets():
    ...
    assert event is None or not has_incomplete_watch_rewards(event)

def test_remaining_minutes_sums_incomplete_tiers_only():
    ...

def test_sort_prefers_soonest_end_then_least_remaining():
    ...

def test_fallback_blocked_only_when_finishable_wanted_game():
    assert should_mine_creator_badges(
        enabled=True, priority="fallback", has_finishable_wanted_game=True
    ) is False
    assert should_mine_creator_badges(
        enabled=True, priority="fallback", has_finishable_wanted_game=False
    ) is True

def test_interrupt_mines_even_with_finishable_game():
    assert should_mine_creator_badges(
        enabled=True, priority="interrupt", has_finishable_wanted_game=True
    ) is True

def test_filter_excluded_casefold():
    ...

def test_stuck_after_fifteen_minutes_without_progress():
    ...
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_creator_badges.py -v`  
Expected: FAIL (module/import missing)

- [ ] **Step 3: Implement `creator_badges.py` to satisfy the interfaces above**

Map field names from the spike notes; if fixture nesting differs, adapt parsers only — keep the public dataclasses stable.

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_creator_badges.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add creator_badges.py tests/test_creator_badges.py
git commit -m "feat: add Creator Badge watch-time domain helpers"
```

---

### Task 3: Settings + UI strings

**Files:**
- Modify: `settings.py` (`SettingsFile`, `default_settings`, `Settings` attrs)
- Modify: `translate.py` (typed settings strings)
- Modify: `lang/English.json`, `lang/Deutsch.json`
- Modify: `gui.py` (mining settings row next to `fallback_badges_emotes`)
- Test: `tests/test_creator_badges.py` (settings defaults via constructing defaults dict or importing `default_settings`)

**Interfaces:**
- Consumes: none from Task 2 beyond priority string values `"fallback"` | `"interrupt"`
- Produces: `creator_badge_watch: bool` default `False`; `creator_badge_priority: str` default `"fallback"`

- [ ] **Step 1: Write failing test**

```python
def test_default_creator_badge_settings_off_and_fallback():
    from settings import default_settings
    assert default_settings["creator_badge_watch"] is False
    assert default_settings["creator_badge_priority"] == "fallback"
```

- [ ] **Step 2: Run test — expect FAIL**

Run: `pytest tests/test_creator_badges.py::test_default_creator_badge_settings_off_and_fallback -v`

- [ ] **Step 3: Add settings keys + GUI**

Checkbox “Mine Creator Badge watch-time from follows”; combobox/check for priority (`fallback` / `interrupt`). English + Deutsch labels. Wire toggles like `_toggle_fallback_badges`.

Exact English copy:
- `creator_badge_watch`: `Mine Creator Badge watch-time from followed channels: `
- `creator_badge_priority`: `Creator Badge priority: `
- priority values displayed as `After finishable games` / `Prefer over games`

- [ ] **Step 4: Run test — expect PASS**

Run: `pytest tests/test_creator_badges.py::test_default_creator_badge_settings_off_and_fallback -v`

- [ ] **Step 5: Commit**

```bash
git add settings.py gui.py translate.py lang/English.json lang/Deutsch.json tests/test_creator_badges.py
git commit -m "feat: add Creator Badge watch settings and UI"
```

---

### Task 4: Follows fetch + event refresh on `Twitch`

**Files:**
- Modify: `twitch.py`
- Test: `tests/test_creator_badges.py` (async helpers mocked — prefer extracting pure glue to `creator_badges.py` if needed: `def select_targets(events, *, exclude, enabled, priority, has_finishable) -> list[CreatorBadgeEvent]`)

**Interfaces:**
- Consumes: `GQL_QUERIES["ChannelFollows"]`, Creator Badge channel/event query from Task 1; `parse_creator_badge_event`, `filter_excluded`, `sort_creator_badge_targets`, `has_incomplete_watch_rewards`
- Produces:
  - `Twitch._creator_badge_events: list[CreatorBadgeEvent]`
  - `async def Twitch.fetch_creator_badge_targets(self) -> list[CreatorBadgeEvent]`
  - refresh on inventory reload path and at most every 45 minutes
  - follow hard cap: stop paging after 500 follows

- [ ] **Step 1: Write failing test for pure selector glue**

```python
def test_select_targets_applies_exclude_incomplete_and_sort():
    ...
```

Implement `select_targets` in `creator_badges.py` if not already covered.

- [ ] **Step 2: Run — expect FAIL then implement selector — expect PASS**

- [ ] **Step 3: Implement `fetch_creator_badge_targets` on `Twitch`**

Page follows → batch/live+event queries per spike → parse → `select_targets` → store on `self._creator_badge_events`. Skip when `creator_badge_watch` is False.

- [ ] **Step 4: Hook refresh**

Call from the inventory/games update path (same cadence family as campaign reload) and gate with a 45‑minute timestamp so maintenance does not hammer follows.

- [ ] **Step 5: Commit**

```bash
git add twitch.py creator_badges.py tests/test_creator_badges.py
git commit -m "feat: discover Creator Badge watch targets from follows"
```

---

### Task 5: Mining integration (`can_watch`, switch, progress, stuck)

**Files:**
- Modify: `twitch.py` (`can_watch`, `should_switch` / channel choose path, `_watch_loop` progress handling)
- Modify: `translate.py` / `lang/English.json` / `lang/Deutsch.json` (status line)
- Test: `tests/test_creator_badges.py` for selection predicates already covered; add focused tests for any new pure helpers (e.g. `active_creator_badge_event_for_channel`)

**Interfaces:**
- Consumes: `should_mine_creator_badges`, `is_stuck`, `remaining_watch_minutes`, `self._creator_badge_events`, settings
- Produces:
  - `def has_finishable_wanted_game(self) -> bool` — True if any campaign for a `wanted_games` entry `can_earn_within(now+1h)` and `can_finish()`
  - `can_watch` returns True for an online non-excluded channel that matches an incomplete Creator Badge watch event when `should_mine_creator_badges(...)` is True
  - Stay on channel until all incomplete watch rewards earned; then remove event / refresh
  - On stuck: drop event from active targets until next refresh
  - Status: `_("status", "creator_badge_watching").format(channel=..., minutes=...)`

- [ ] **Step 1: Write failing tests for helpers**

```python
def test_active_event_for_channel_matches_login():
    ...

def test_event_complete_when_all_watch_rewards_earned():
    ...
```

- [ ] **Step 2: Implement helpers + integrate `can_watch` / selection**

In `fallback`, Creator Badge watching must not win while `has_finishable_wanted_game()` is True (Review Focus: switch back when a finishable game appears — ensure maintenance/games update re-evaluates selection).

Offline channel: `can_watch` False (existing early return).

- [ ] **Step 3: Progress in watch loop**

After existing drop progress handling, if watching a Creator Badge target, query progress GQL, update `current_minutes`, detect earned tiers, keep watching until all watch rewards earned. Auto-claim only if spike documented a claim mutation.

- [ ] **Step 4: Stuck guard**

If `is_stuck` for the active event, print a short status, remove it from `_creator_badge_events`, `stop_watching` / reselect.

- [ ] **Step 5: Run unit tests**

Run: `pytest tests/test_creator_badges.py -v`  
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add twitch.py creator_badges.py translate.py lang/English.json lang/Deutsch.json tests/test_creator_badges.py
git commit -m "feat: mine Creator Badge watch-time on followed channels"
```

---

### Task 6: Smoke checklist + branch review prep

**Files:**
- Modify: none required unless bugs found
- Optional: note manual checklist under spike doc

**Interfaces:**
- Consumes: full feature from Tasks 1–5

- [ ] **Step 1: Run full unit suite for touched areas**

Run: `pytest tests/test_creator_badges.py tests/test_inventory_state.py tests/test_channel_state.py -v`  
Expected: PASS

- [ ] **Step 2: Manual smoke (when a live event exists)**

Toggle on → followed channel with watch-time event live → miner switches when no finishable game → progress moves → all tiers complete → leaves channel. Toggle off → no Creator Badge targeting.

- [ ] **Step 3: Commit only if fixes landed; otherwise done**

---

## Self-review notes

- Spec coverage: spike, follows discovery, watch-only, fallback vs unfinishable, interrupt option, sequential tiers, exclude, stuck guard, settings/UI, no campaign-badge regression — each mapped to a task.
- Review Focus items pinned in Tasks 2, 4, and 5 tests/behavior.
- GQL field mapping intentionally left to spike notes so Task 2 parsers stay the single adaptation point.
