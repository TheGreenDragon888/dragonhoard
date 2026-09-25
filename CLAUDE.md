# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Dragonhoard: a Discord economy game played via slash commands, built with
`discord.py` and SQLite. Every server the bot joins gets its own currency and
economy — nothing carries over between servers. The core loop: mine raw
materials with drills → collect them → sell to the server market → reinvest
in the furnace/blast furnace/factory/press/scrapper to produce better
materials and upgrade drills, with production fees burning currency back out.
See README.md for the full player-facing rundown of the loop and command list.

**Code is written on a development machine, on the `beta` branch or a branch
that merges into it — never on the server.** The server's two checkouts are
deployment targets that only pull from GitHub: `/opt/dragonhoard-beta` follows
`beta` (`update-beta.sh`, by hand, or by an optional two-minute timer) and
`/opt/dragonhoard` follows `main` (`update.sh`, by hand). A push to `beta` is
what the beta bot runs next, and with the timer on it is the deploy: push only
when asked. `main` only ever moves by a fast-forward
release from `beta` or a hotfix, and `beta` is never squashed or rebased into
it. See docs/testing.md for the full workflow, backups, and rollback.

## Commands

```bash
# Run the full test suite (~15s on the server). tests/conftest.py puts the
# temporary databases on tmpfs: on the server's disk the same run takes about
# nine minutes, every second of it SQLite fsyncs. Set TMPDIR to override.
# Needs Python 3.12+ and DISCORD_BOT_TOKEN set to anything (config.py refuses
# to import without one; a placeholder in .env does). On Windows the venv's
# interpreter is venv\Scripts\python instead of venv/bin/python.
venv/bin/python -m pytest tests/ -q
# (unittest discover also works, but reads no conftest, so it runs on the
# disk: TMPDIR=/dev/shm python -m unittest discover tests)

# Run a single test file / test case / test method
venv/bin/python -m pytest tests/test_market.py -q
venv/bin/python -m pytest tests/test_market.py::TradeableOrderTests -q
venv/bin/python -m pytest tests/test_market.py::TradeableOrderTests::test_it_is_exactly_the_ores_and_smelted_materials -q

# Run the bot locally (needs a filled-in .env — see .env.example)
python bot.py
```

There is no separate lint/build step; `pytest` is the thing to run before
calling anything done. GitHub Actions runs the same suite on every push
(`.github/workflows/tests.yml`). Most cogs' logic is covered by tests that
construct a temporary SQLite database directly (`unittest.IsolatedAsyncioTestCase`), not
by spinning up Discord — no gateway connection is needed to test game logic.

## Architecture

**Entry point**: `bot.py` builds a `commands.Bot` with a custom
`tree_cls=DragonhoardTree`, attaches one shared `bot.db = Database(...)`
(SQLite wrapper), loads every cog listed in `INITIAL_EXTENSIONS`, seeds
global notices, and syncs the command tree (to `DEV_GUILD_ID` instantly if
set — beta — or globally otherwise — production). `config.py` is the single
place that reads `.env`/environment; everything else imports from there.

**Cogs** (`cogs/`): one file per command group (`/mine`, `/market`,
`/furnace`, `/blast`, `/factory`, `/press`, `/scrapper`, `/jobboard`, `/bet`,
`/setup`, ...). Each cog's own docstring lists exactly which slash commands it
implements — read that first when working in a given feature area.

**Database layer** (`database/db.py`): `sqlite3` is synchronous, so every
query runs via `asyncio.to_thread`. `Database.execute/fetchone/fetchall` each
run as a standalone statement that commits on its own — fine for one
statement. **Any operation that reads a value and then writes based on it
must use `async with db.transaction()` instead** (see the docstring on
`Database.transaction` for the torn-write and read-then-write races this
prevents). Never await Discord/network calls inside a transaction block — it
holds the write lock for the whole bot. Connections are reused, not opened
per statement: standalone statements borrow from a small pool and every
transaction runs on one long-lived connection (1.4; the module docstring
says why — per-statement opening was the dominant cost of a command on
small hardware). A statement that raises discards its connection rather
than returning it to the pool, so nothing has to prove it left no
transaction open. Migrations for schema changes live in
`Database._init_schema_sync`, gated either on introspecting the live table
(structural changes) or on `PRAGMA user_version` (pure data changes that
can't be detected from the schema alone) — see the extensive comments there
before adding a new one, and `tests/test_migrations.py` for how an old-schema
database is hand-built and asserted to survive being opened by new code.

**`utils/db_helpers.py`**: shared inventory/balance/stock/fee helpers used
across cogs, all taking a `_Executor` (either a `Database` or a
`Transaction`, so the same helper works standalone or inside a transaction).
Three of them are how the five machine loops stay cheap when nothing is
happening, and a sixth machine's loop should use the same three: each tick
visits only `guilds_with_queued_work(db, machine)` rather than every
`server_config` row; a job is finished through `complete_job` and advanced
through `advance_job` rather than an inline UPDATE, because `complete_job`
is also where finished rows older than `COMPLETED_JOB_HISTORY_DAYS` get
pruned. How MUCH work a tick does is `ProductionClock.earn`, shared by the
four item-counting loops: work is earned from elapsed time and never from
before the oldest live job was queued, and a queue that empties drops its
leftover fraction. Before it, each tick credited a whole tick's rate to
whatever had just been queued, so a level 3 furnace filled a fresh one-item
order on the very next tick, seconds after the command. `guilds_with_queued_work`
returns everything that needs - `level`, `collected` and `work_started` - from
the one query it already ran, so an idle server still costs nothing. The rule
underneath is `elapsed_work_hours`, which the press and the drill harvest use
too, keeping their state in the database rather than in memory: the press
clears `press_progress` when a job is queued onto an empty press, and a drill's
`mined_until` is reset to now by anything that starts it mining (`/mine place`,
and a `/collect` or a container that frees a full one - see
`COLLECT_EMPTY_DRILL_SQL` and `set_container`). A new way to start a drill
mining has to reset it too, or its first tick pays for time it wasn't mining.

A machine's SPEED is not its whole-number level: `effective_level(level,
fees_collected)` (data/materials.py) is the level plus how far its fees are
through that level, so halfway to level 2 runs halfway between level 1's and
level 2's speeds. The rate functions are linear in the level, which is what
lets them take it unchanged. Only speed interpolates - the queue cap, the
level players see and levelling itself stay whole. What a loop actually runs
at is `run_level(cfg, now)` (utils/db_helpers.py): the effective level times
the government's multipliers - x2 per Infrastructure Enhancement and x2 more
while a Server Bonanza runs (1.4). `guilds_with_queued_work` and
`machine_speed_level` carry the columns it needs; a status embed or receipt
that computes a speed any other way will quote the wrong one. Speeds reach players
through `format_rate` (utils/formatting.py), which truncates rather than
rounds so no machine is shown running at a speed it hasn't reached. The partial index `idx_production_jobs_live` (schema.sql) is what
makes live-job lookups cheap, and it only applies to a query whose WHERE
carries the literal `status != 'complete'` — spell it that way. The furnace
additionally gates its auto-smelt on one read of every server's ore
(`_auto_smelt_pass`) so idle servers cost nothing per tick.
`MACHINES = ("furnace", "blast_furnace", "factory", "press", "scrapper")` —
these five share uniform `server_config` column naming (`<machine>_level`,
`_fees_collected`, `_max_queue`, `_fee_multiplier`, `_enhancement_level`)
and the `production_jobs` queue table, which is what lets `queue_room`,
`apply_machine_upgrades`, etc. be one implementation instead of five. A sixth
machine should follow this pattern: the blast furnace (1.3) needed nothing
from this module but its entry in that tuple and its default fee (now
`MACHINE_DEFAULT_FEES`). There is no per-server fee column any more: a fee is
`machine_fee(machine, multiplier)`, the config.py default times the elected
Treasurer's multiplier.

What a machine COUNTS in is not uniform. Four of them count items; the blast
furnace counts batches of `BLAST_FURNACE_BATCH_SIZE` items — in
`production_jobs.quantity`, in its fee and in its queue cap alike. The helpers
that render those numbers to a player take a `unit` argument for that reason
(`queue_full_message`, `queue_field_name`, `queue_limit_field_value`).

Every fee a cog charges goes through `charge_machine_fee`
(utils/government.py), which takes it from the player, holds the Treasurer's
tax share for the government, and burns and banks the rest through
`bank_infrastructure_fee` - the single place a fee turns into progress: it
credits `<machine>_fees_collected`, re-levels that machine, and re-checks the
server's mining slots. Do not write either step in a cog. Mining slots (1.3) are the
reason it exists — the cap on drills per player per server is unlocked by the
the fees it has COLLECTED (`mining_slot_status`), so a rule that had been copy-pasted at
seven fee sites was about to be copy-pasted at eight. The cap is derived on
read and never stored; the only stored column, `mining_slots_announced`,
dedupes the unlock notice and nothing else. See docs/mining.txt.

That total is `slot_progress(cfg)`, summed over `SLOT_PROGRESS_COLUMNS`: every
machine's fees plus `mining_slot_credit`, what the government has bought
toward slots (1.4). It is one function, not a sum written out per call site,
because it has two consumers that must agree — the figure `/economy status`
shows as "Mining slot progress" (it was "Fees collected" until the credit
column made it more than fees) and the mining slot ladder priced in that same
figure. Do not add the columns up anywhere else; `web/queries.py` calls the
same function for the same reason.

**`utils/government.py` + `cogs/government.py`**: the server government (1.4),
an elected Mayor and Treasurer. Admins have no say in it - that is why
`/setup fee` is gone. Voting is Thursdays on the job board's America/Phoenix
clock, counted at the midnight that ends it, both lazily by any government
command and by an hourly loop that also pays bondholders. The rule the module
keeps: **currency leaves the government only as a burn or a bond repayment.**
Tax is held, not burned, in `treasury` or `repayment_pool`, so
`circulating_currency` counts both (the same argument as order escrow);
`spend_treasury` is the one way money leaves the treasury, and every caller is
a project, so every such payment is a burn. The bond premium is the one leak.
Bonds are integer cents split by `apportion()` like bet pots; live-bond
queries spell `remaining_cents > 0` literally for `idx_government_bonds_owed`.
A departed creditor's bonds are frozen, never voided, so kicking creditors
can't wipe the debt. See docs/government.md, where every number is argued.

`/market` is a group of seven: `sell`, `buy`, `status`, and 1.4's `list`,
`order`, `cancel` and `entries`. `status` is the market - the server's prices
and both player books aggregated per material - and `entries` is the caller's
own rows with the ids `cancel` takes. They are separate pages because only the
second needs a per-viewer read, and `status` is the one everybody runs. `sell` and `buy` take an autocomplete rather than the
six static choices they had before 1.4, and their parameter is `item`, not
`material` - it can be a drill. Both lists are filtered to what the command
could actually fill right now: `buy` offers a material only if the server holds
some or another player has listed some (plus listed drills, by
`listing:<id>`), and `sell` offers one only if the caller holds some AND
somebody will buy it - the server bids for `TRADEABLE_ORDER` and nothing else.
Both exclude the caller's own book entries, which `plan_buy`/`plan_sell` will
not fill against. The lists are a convenience; the membership check in each
command is the enforcement.

`/economy` is a group, not a bare command: `/economy status` is the overview
page and `/economy gdp` holds every GDP figure and the reasoning behind it
(both windows, the per-stage breakdown, the import/export comparison). Discord
will not let a command with subcommands be invoked on its own, which is why the
overview has a name at all.

**`utils/betting.py` + `cogs/betting.py`**: `/bet` (1.4), server prediction
bets. Parimutuel: everything staked goes into one pot and the winning side
splits all of it. **No currency is created or destroyed by any of it** — that
was the specified constraint, and it is why there is no rake, why nothing here
calls `record_minted`/`record_burned`, and why stakes are integer
`stake_cents`. The split is `apportion()`, floor plus largest remainder, the
one place the pot is divided and the one place the invariant is asserted; a
proportional split of floats does not add back up to the pot. Stakes are
escrowed on the wager row exactly as an order's currency is, so
`circulating_currency` counts both (it takes each in its own unit — bids in
`PLAYER_PRICE_SCALE`, stakes in cents). Live-bet queries must spell
`status IN ('open', 'closed')` literally, the same partial-index rule
`idx_production_jobs_live` has. There is no background loop: `refresh_status`
closes an expired bet lazily, the way the job board posts its task. See
docs/betting.md.

**Notice actions** (`notifications.action_key`, the registry in
`utils/responses.py`): a notice can carry buttons, which is how a new bet
reaches a server without the bot posting into a channel unbidden — that would
override `/setup messages private`. The reason it hangs off the notice row
rather than a lookup is cost: `respond()` has already fetched that row, so an
idle server pays nothing, whereas asking "any open bets?" on the reply path
would charge every command in every server. Buttons are `DynamicItem`s so they
survive restarts; `respond()` drops a duplicate `custom_id` because a reply and
its notice can offer the same bet's buttons at once, which Discord rejects.

**`utils/guild_helpers.py`**: `human_member_count` is the only consumer of
the members intent, and it COUNTS a guild's members over the gateway rather
than reading a cache — `bot.py` turns the member cache off
(`MemberCacheFlags.none()`, `chunk_guilds_at_startup=False`) because holding
every member of every server in memory for one number was the largest thing
the process kept in RAM. The count is remembered for
`MEMBER_COUNT_CACHE_SECONDS`. Do not reintroduce `guild.members` or
`guild.get_member` reads anywhere; they read as empty now.

**`utils/channel_guard.py`**: the designated-bot-channel restriction
(`/setup channel`) is enforced in exactly one place — `DragonhoardTree.
interaction_check` — rather than per-command or per-cog, so a newly added
command can't accidentally opt out of it. Read the module docstring before
touching command dispatch; it documents non-obvious discord.py behavior
(interaction_check fires before the command resolves and for autocomplete
too) verified against the installed version.

**`utils/responses.py`**: `respond()` is the one funnel for a command's
successful reply — it applies the server's public/private setting
(`/setup messages`, ephemeral by default) and attaches any unseen
notifications (`utils/notifications.py`). Error/validation messages should
keep using `interaction.response.send_message(..., ephemeral=True)` directly
since they're personal to the invoking user regardless of server setting.

**`data/materials.py`**: game balance data — drop rates, recipes, drill
stats, market target stock. `data/manual.py` is the text served by
`/help`/`/manual`/`/man`. `data/emoji.py` + `custom_emoji("Name", live_id,
beta_id)` resolve custom Discord emoji per-application, because the live and
beta bots are separate Discord applications with separately uploaded icons
(`config.IS_BETA` picks which id).

**Embeds** (`utils/embeds.py`): every embed goes through `make_embed()`
(palette color + standard footer) or, for the five machine status commands,
`make_infrastructure_embed()`. See `docs/stylization.md` for the full color
table (each feature area owns a fully-saturated color) and layout rules
before adding a new embed or a sixth machine's status command.

## Beta vs. production

Two completely separate installations share no code checkout, database, or
Discord application — see `docs/testing.md` for the full day-to-day
workflow (commit on `beta` → push → `update-beta.sh` on the server →
fast-forward `main` to `beta` → `/opt/dragonhoard/update.sh` to ship; hotfixes
branch from `main` and are merged back into `beta`), backup/rollback
procedure, and how to copy live data into beta for testing against real data.
Key point: `BOT_ENVIRONMENT=beta` in `.env` is what flips `config.IS_BETA`,
which selects `beta_id` emoji and syncs commands to `DEV_GUILD_ID` instead of
globally.

## Writing comments

This codebase leans hard on rationale comments — most of them explain *why*,
and that is the point. Two rules keep them worth trusting:

**Only state rationale you were actually given** — by the person, by an
existing doc, or by a test you can point to. If you don't know why existing
code is written a certain way, don't invent a plausible-sounding reason.
Leave it uncommented, or ask.

**Every specific figure in a comment must be one you actually computed**, and
the computation has to be reproducible from the repo. A sound argument welded
to a made-up number is the failure mode this rule exists for, and it is the
more common one: an audit of every comment in the codebase found the
reasoning almost always correct and the illustrative figures — float
artifacts, hour counts, item ratios — wrong often enough to matter, because
they read as though they had been run when they had not. If a number is worth
stating, run it; if it can't be run from the repo (a live-database figure, a
simulation nobody kept), either leave it out or say plainly where it came
from and when.

The corollary for balance data: prefer numbers that can't go stale. A
historical figure ("the previous ladder was 150/200/300/400/500") is safe
because it describes a past state. A derived one ("a level 2 Steel Drill mines
9/hr") goes wrong the moment anything is retuned — point at the test that pins
it instead.

## Design docs

`docs/market.md` and `docs/mining.txt` explain the economic/gameplay rules
behind the market's prices and the mining pool respectively — read these
before changing pricing, drop rates, or pool mechanics, since the *why*
behind the numbers lives there, not in code comments. `docs/betting.md` does
the same for `/bet`, and is where the no-rake decision and the rounding rule
are argued rather than merely stated. `docs/government.md` does it for the elected
Mayor and Treasurer: the supply argument, the bond cap, and the prices of the
projects.

**The player market (1.4)**: `/market list` and `/market order` put players'
own asks and bids on two per-guild books (`market_listings`, `market_orders`),
and `/market buy`/`/market sell` route across both before falling back to the
server (`utils/market_book.py`). Three things there are load-bearing and easy
to break:

- **Only the server's share of a sale may credit the job board.** A player-to-
  player leg mints and burns nothing, so crediting it would let two players
  wash-trade one stack and mint the bonus on every leg. `market_sell` passes
  `server_quantity(fills)` to `credit_job_progress`, never the full quantity.
- **A listed drill is escrowed in `drills.listed_id`, NOT `locked_job_id`** -
  `release_stale_drill_locks` frees any lock that doesn't name a live job, and
  would hand back a drill that was still on the book. `drill_unavailable_reason`
  /`DRILL_AVAILABLE_SQL` in `utils/drills.py` are the one place that rule lives;
  eight call sites read them.
- **Order escrow is not a burn.** It leaves `server_currency_balances` but not
  the economy, so `circulating_currency` in `utils/db_helpers.py` adds it back -
  one function, two consumers (`/economy status` and `web/queries.py`), the same
  argument as `slot_progress`.

`PERMANENT_MATERIALS` (keyed on `PRESS_MATERIALS`, the "Exotic Matter"
category) is the single definition of what can never be traded, scrapped or
consumed. Add a second exotic material to that table and it inherits every
exclusion; state the reason nowhere else.

Market prices are static as of 1.3 and are whole numbers of cents, enforced
at import by `MARKET_PRICE_CENTS`. Player-set prices are whole numbers of
ten-thousandths of a currency unit (`PLAYER_PRICE_SCALE`), because the band a
player ask must fit inside is one cent wide for iron ore and contains no whole
cent. Three constants now hold each other up and
should be changed together or not at all: the price table, `MARKET_BUY_MARKUP`
(2), and `JOB_BOARD_TARGET_PAYOUT` (1.00). The job board pays per completion
with no daily cap, and what stops that printing currency is only that buying
the goods back costs twice what selling them paid — see docs/market.md
section 1.
