# Three-Tier Member Model — Sense-Check Info Pack

> **Reader (codex):** You are doing a READ-ONLY pre-deploy sense-check of a
> production Discord bot migration. **Do not modify any files, do not run any
> write operations, do not connect to Discord or Supabase.** Audit the code,
> the SQL, the scripts, and the deployment plan below. Report findings and a
> pass/fail verdict per checklist item. This repo is
> `/Users/peteromalley/Documents/banodoco-workspace/brain-of-bndc`.

## 1. Goal

BNDC (a Discord community bot) previously had a binary access model: one
"Speaker" role + per-channel `speaker_mode` (`normal`/`readonly`/`exempt`).
Muting = removing the Speaker role. Gaps found in production:

1. **#introductions was open to everyone** (it must be, so new members can
   introduce themselves to earn Speaker) — so a muted member could still post there.
2. **The moderation channel was `exempt`** (open to everyone), so anyone could
   post in it, not just the muted person.
3. **DM→admin-chat was gated by `members.can_message_bot`**, a role-independent
   flag never revoked by mute — muted members could still DM the bot.

Goal: three explicit member tiers — **Newbie** (non-speaker in good standing),
**Speaker**, **Moderated** (can only post in the moderation/appeal channel) —
with exactly one tier role per non-staff member, and @everyone denied send in
**every** channel (no exempt channels remain).

## 2. User-confirmed decisions

- #introductions is **gated to Newbie+Speaker** (not @everyone-open); Newbie is auto-assigned on join.
- Moderation channel is postable by **Moderated + Speaker** (publicly readable by all).
- **`can_message_bot` is revoked on moderation**, restored on unmute.
- Newbie tier can post in: **#introductions, grants forum, help/support** (reads everywhere else).
- **Gate channel (`bot` mode) is read-only but readable by Newbie + Speaker** — the pinned onboarding / welcome message must stay visible to them, so the bot enforces `view_channel=True` for those two roles there (see `_VIEW_ROLE_ALLOWED` in `speaker_perms.py`).

## 3. Permission model

New per-channel `speaker_mode` values replacing `normal/readonly/exempt`:
`bot` (nobody posts), `newbie` (Newbie+Speaker), `community` (Speaker only),
`appeal` (Speaker+Moderated). Managed roles: @everyone, Newbie, Speaker, Moderated.

| mode | @everyone | Newbie | Speaker | Moderated |
|---|---|---|---|---|
| `bot` | ✗ | ✗ | ✗ | ✗ |
| `newbie` | ✗ | ✓ | ✓ | ✗ |
| `community` | ✗ | ✗ | ✓ | ✗ |
| `appeal` | ✗ | ✗ | ✓ | ✓ |

Discord mechanic: for a member holding multiple roles, channel-overwrite allows
and denies are OR'd across all roles and applied as `(base & ~denies) | allows` —
so an **allow from any role beats a deny from another role** for the same member;
role position does NOT settle it (verified against `discord.py permissions_for`).
Moderated therefore carries an explicit Deny on every non-`appeal` channel AND the
block is enforced by moderation **removing** Newbie/Speaker synchronously, with
the 5-minute reconciliation loop + `on_member_update` stripping stale tier roles.
@everyone is denied in all modes.

## 4. Implementation (files changed)

- `src/common/speaker_perms.py` — 4-mode × 4-role permission table; `apply_perms_to_channel(channel, roles: Mapping, mode)` manages all four role overwrites.
- `src/common/db_handler.py` — `member_status` layer (`set/get_member_status`, `get_member_prior_status`, `get_members_by_status`), `get/set_member_can_message_bot`, timed-mute `prior_status`/`prior_can_message_bot` snapshots, channel-mode normalization, backward-compat `set_is_speaker`/`get_is_speaker` shims.
- `src/features/admin/admin_cog.py` — join auto-assigns Newbie; role-aware `on_member_update`; `/mute` swaps Newbie/Speaker→Moderated + snapshots prior state + revokes `can_message_bot`; `/unmute` restores prior tier + DM; `check_expired_mutes` (prior-status restore + DB-vs-roles reconciliation); `enforce_channel_permissions` (new modes, gate→`bot`, 3 roles).
- `src/features/gating/gating_cog.py` — approval swaps Newbie→Speaker; refuses moderated members.
- `src/features/admin_chat/tools.py` — DM/admin-chat mute/unmute mirror the swap logic.
- `src/features/curating/curator_cog.py` — `curate` now owner-gated.
- `.migrations_staging/20260806000000_member_status_three_tier.sql` — schema + value migration.
- `scripts/backfill_member_status.py`, `scripts/migrate_channel_modes.py`, `scripts/sense_check_three_tier.py`, `scripts/create_tier_roles.py`, `scripts/setup_channel_perms.py` (updated signature).
- `.env.example` — NEWBIE_ROLE_ID / MODERATED_ROLE_ID.
- `tests/test_three_tier_member_model.py` — 24 unit tests.

## 5. Current ops state (IMPORTANT — verify this matches reality)

**DONE:**
- Newbie + Moderated roles created in production via the bot token:
  - Newbie `1534854919562072115` (position 1)
  - Speaker `1475121624855482418` (position 2)
  - Moderated `1534854920732151838` (position 3) — above Speaker ✓
  - admin at position 4 (above Moderated ✓)

**NOT DONE (still pending, in this order):**
1. Supabase SQL migration NOT applied (`member_status` columns don't exist yet).
2. `.env` NOT updated (needs NEWBIE_ROLE_ID / MODERATED_ROLE_ID).
3. New bot code NOT deployed.
4. `scripts/backfill_member_status.py` and `scripts/migrate_channel_modes.py` NOT run.
5. `scripts/sense_check_three_tier.py` NOT run (it is the gate, see §7).

**Deploy sequencing (the critical ordering):** the NEW bot code must be deployed
(and the DB migrated) BEFORE running the backfill/channel-mode migration —
otherwise the OLD running bot's 30-min `enforce_channel_permissions` loop
re-applies the legacy `normal/readonly/exempt` modes and fights the new channel
perms (e.g. the moderation channel would flip back to @everyone-allowed).

## 6. Known pre-existing test failures (unrelated to this change)

8 failures in `tests/test_live_runtime_wiring.py`, `tests/test_topic_editor_core.py`,
`tests/test_topic_editor_runtime.py` — caused by in-progress live-update work in
the working tree (e.g. `FakeTopicEditor` missing `llm_client` kwarg at
`src/features/summarising/summariser_cog.py:152`; citation-rendering assertion
changes). None of these tests import files changed by this migration.
Everything else passes (804 passed / 8 failed / 19 skipped).

## 7. What to sense-check

Audit each and give a PASS/FAIL + notes:

1. **Permission model** — is the 4-mode × 4-role table in `speaker_perms.py`
   correct? Does the Moderated-deny-beats-Speaker-allow invariant actually hold
   given role-position tie-breaking? Is @everyone denied in every mode?
2. **Mute/unmute lifecycle** (`admin_cog.mute_user`/`unmute_user`, `tools.py`
   mirrors) — prior-tier snapshot, `can_message_bot` revoke/restore, timed-mute
   restore path, cache invalidation, "already muted/unmuted" edge cases.
3. **Enforcement loops** — `check_expired_mutes` Phase 1 (prior-status restore)
   and Phase 2 (DB↔roles reconciliation), `enforce_channel_permissions` modes,
   `on_member_update` role-aware restore (moderated members never regain tiers).
4. **Gating** — `_approve_member` Newbie→Speaker swap + moderated refusal.
5. **DB layer** — `member_status` get/set fallbacks, legacy `speaker_muted`
   reconciliation, channel-mode normalization, `.or_` filters syntax.
6. **SQL migration** — columns, defaults, value mapping, `member_status`
   backfill correctness.
7. **Migration scripts** — `backfill_member_status.py` (assigns Newbie to every
   non-Speaker/non-muted member), `migrate_channel_modes.py` (channel mapping:
   gate→bot, intro/grants/help→newbie, moderation→appeal, rest→community),
   `sense_check_three_tier.py` (8 checks), `create_tier_roles.py`.
8. **Deploy sequencing** (§5) — is the ordering sound? Any step that fights
   another? Anything missing (e.g. currently-muted members, the `can_message_bot`
   snapshot for pre-existing mutes being NULL, forum-thread overwrites)?
9. **Config plumbing** — role ID resolution (server_config first, env fallback)
   in admin_cog, tools.py, gating_cog, and the scripts.
10. **Tests** — do `tests/test_three_tier_member_model.py` meaningfully cover the
    above? Any behavior gap the tests miss?

## 7b. Sense-check v1 findings (ALREADY FIXED — re-verify)

A prior codex pass (v1) returned FAIL with these findings. All have been
addressed — re-audit them:

- **[BLOCKER] Existing muted members not revoked from `can_message_bot`** → `backfill_member_status.py` now calls `set_member_can_message_bot(False)` for moderated members after snapshotting; SQL backfills `timed_mutes.prior_status='speaker'`.
- **[BLOCKER] Sense-check treated unset @everyone overwrites as safe** → `sense_check_three_tier.py` now requires explicit deny (`is not False`) and validates tier-role overwrites against the mode table.
- **[BLOCKER] Gating lacked env fallback + only checked the Moderated role** → `gating_cog._get_guild_config` now falls back to env; `_approve_member` also refuses members whose DB status is `moderated`.
- **[HIGH] Admin-chat re-mute clobbered the prior snapshot** → `tools.execute_mute_speaker` now preserves `prior_status`/`prior_can_message_bot` on re-mute (`set_prior=not was_already_muted`).
- **[HIGH] Timed expiry / admin-chat didn't invalidate the DM cache** → `check_expired_mutes` pops `_dm_access_cache`; `tools._invalidate_dm_cache` via `bot.get_cog('Admin')`.
- **[HIGH] Backfill included staff + didn't strip stale tier roles** → optional `STAFF_ROLE_IDS` env skip; backfill now strips extra tier roles.
- **[HIGH] Runbook/script ordering contradiction** → `migrate_channel_modes.py` message corrected to "deploy new code (SQL applied) → sense-check".
- **[MEDIUM] Gate channel read only from server_config** → `enforce_channel_permissions` now falls back to `GATE_CHANNEL_ID` env.
- **[MEDIUM] Fail-open defaults** (unknown mode→community; DB error→speaker/True) → **intentionally retained**; they match pre-existing behavior and are the least-harm defaults. Documented, not changed.

New tests added in v2: re-mute snapshot preservation, DB-moderated approval refusal (26 total in `tests/test_three_tier_member_model.py`).

## 7c. Sense-check v2 findings (ALREADY FIXED / RESOLVED — re-verify)

- **[BLOCKER] Permission invariant** — codex was RIGHT that discord.py OR's all
  role allows/denies and allows win (verified against
  `discord.abc.GuildChannel.permissions_for`: `(base & ~denies) | allows`). Role
  position does NOT make a deny beat an allow. The design does NOT rely on
  position: moderation REMOVES the granting roles synchronously and the 5-min
  reconciliation strips strays. `speaker_perms.py` docstring corrected; the
  behavior was already correct. Sense-check item 5 already asserts moderated
  members hold only Moderated.
- **[BLOCKER] Backfill `members` undefined** — a real bug introduced in v2's
  backfill edit. Fixed: `members = sorted(...)` restored before the loop.
- **[BLOCKER] Sense-check could exit 0 on early failure** — the tier-roles-missing
  (and other fatal) early returns now `sys.exit(1)`.
- **[HIGH] Ordering contradictions** — `sense_check_three_tier.py` docstring,
  `migrate_channel_modes.py` and `backfill_member_status.py` next-step messages
  now all state the same runbook: SQL → deploy code → backfill → channel modes →
  sense-check.
- **[HIGH] Timed-expiry absent-member didn't restore `can_message_bot`** — fixed
  in `admin_cog.check_expired_mutes` absent path.
- **[HIGH] `.env` lacked the new role IDs** — added: `NEWBIE_ROLE_ID=1534854919562072115`,
  `MODERATED_ROLE_ID=1534854920732151838`; `SPEAKER_EXEMPT_CHANNELS` deprecated.
- SQL "not rerunnable" claim — the migration is idempotent (`ADD COLUMN IF NOT
  EXISTS`, `WHERE ... IS NULL`, value CASE maps migrated values to themselves).

**v3 re-verify targets:** confirm the backfill `members` fix, sense-check exit
codes, ordering docstrings, absent-member restore, and the corrected permission
invariant framing.

## 7d. Sense-check v3 findings (ALREADY FIXED — re-verify)

- **[BLOCKER] Backfill destroyed legacy mutes** — the SQL marks `speaker_muted=true`
  rows as `moderated`, but the backfill classified purely by roles, so legacy
  mutes (Speaker removed, Moderated not yet assigned) were demoted to Newbie.
  Fixed: `backfill_member_status.py` now honours `db_status == 'moderated'`,
  assigns the Moderated role to those members, and revokes `can_message_bot`.
- **[HIGH] Admin-chat DM-cache lookup used the wrong cog name** — verified:
  `AdminCog.__cog_name__` is `AdminCog` (discord.py does NOT strip "Cog").
  `_invalidate_dm_cache` now tries `('AdminCog', 'Admin')`.
- **[HIGH] Join/approval race** — DB status was written AFTER the role change, so
  `on_member_update` (fired by `add_roles`) saw status defaulting to `speaker` for
  unknown members and could strip the just-added role. Fixed: `on_member_join`
  and `_approve_member` now write the DB status FIRST.
- **[HIGH] Backfill not repeatable** — `set_prior=True` re-snapshotted
  `can_message_bot` on every run (clobbering the original prior with `False`).
  Fixed: only snapshots when no prior exists.
- **[HIGH] Sense-check check 7 too weak** — now asserts `can_message_bot=False`
  for ALL moderated members, not just snapshot-gated ones.
- **[HIGH] `migrate_channel_modes.py --dry-run` wrote Discord perms** — fixed:
  dry-run now logs intent only, never calls `apply_perms_to_channel`.
- **[HIGH] SQL mode CASE not idempotent** — `else 'community'` collapsed existing
  `bot/newbie/appeal` on re-run. Fixed: restricted to legacy values with
  `WHERE speaker_mode in ('normal','readonly','exempt')`.
- **[HIGH] Ordering wording** — sense-check success message now "safe to go live"
  (it runs post-deploy); stale "position beats allow" claims corrected across
  `speaker_perms.py`, `create_tier_roles.py`, `backfill_member_status.py`,
  `.env.example`.

**v4 re-verify targets:** the legacy-mute backfill path, cog-name lookup, the
status-before-role ordering, backfill idempotency, sense-check check 7,
dry-run no-write, and SQL idempotency.

## 8. Explicit constraints for you

- **READ-ONLY.** Do not edit files, run migrations, create roles, or connect to
  Discord/Supabase.
- You MAY run `python -m pytest <file>` and `python -c "import ast; ..."` to
  sanity-check, but nothing that writes.
- Do not run `scripts/sense_check_three_tier.py` — it connects to live Discord
  and is only valid post-migration.
- Return: a findings list (severity-tagged), the PASS/FAIL verdict per checklist
  item, and any blocking issues before deploy.
