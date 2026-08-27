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

**This directory (`/opt/dragonhoard-beta`) is the only place to edit code.**
`/opt/dragonhoard` is a separate, unrelated checkout that only ever runs
`git pull` from GitHub's `main` — never edit it directly (see docs/testing.md
for the full beta/production workflow, backups, and rollback).

## Commands

```bash
# Run the full test suite
venv/bin/python -m pytest tests/ -q
# (unittest discover also works: python -m unittest discover tests)

# Run a single test file / test case / test method
venv/bin/python -m pytest tests/test_market.py -q
venv/bin/python -m pytest tests/test_market.py::TradeableOrderTests -q
venv/bin/python -m pytest tests/test_market.py::TradeableOrderTests::test_it_is_exactly_the_ores_and_smelted_materials -q

# Run the bot locally (needs a filled-in .env — see .env.example)
python bot.py
```

There is no separate lint/build step; `pytest` is the thing to run before
calling anything done. Most cogs' logic is covered by tests that construct a
temporary SQLite database directly (`unittest.IsolatedAsyncioTestCase`), not
by spinning up Discord — no gateway connection is needed to test game logic.

## Architecture

**Entry point**: `bot.py` builds a `commands.Bot` with a custom
`tree_cls=DragonhoardTree`, attaches one shared `bot.db = Database(...)`
(SQLite wrapper), loads every cog listed in `INITIAL_EXTENSIONS`, seeds
global notices, and syncs the command tree (to `DEV_GUILD_ID` instantly if
set — beta — or globally otherwise — production). `config.py` is the single
place that reads `.env`/environment; everything else imports from there.

**Cogs** (`cogs/`): one file per command group (`/mine`, `/market`,
`/furnace`, `/blast`, `/factory`, `/press`, `/scrapper`, `/jobboard`,
`/setup`, ...). Each cog's own docstring lists exactly which slash commands it
implements — read that first when working in a given feature area.

**Database layer** (`database/db.py`): `sqlite3` is synchronous, so every
query runs via `asyncio.to_thread`. `Database.execute/fetchone/fetchall` each
open their own connection and commit standalone — fine for one statement.
**Any operation that reads a value and then writes based on it must use
`async with db.transaction()` instead** (see the docstring on
`Database.transaction` for the torn-write and read-then-write races this
prevents). Never await Discord/network calls inside a transaction block — it
holds the write lock for the whole bot. Migrations for schema changes live in
`Database._init_schema_sync`, gated either on introspecting the live table
(structural changes) or on `PRAGMA user_version` (pure data changes that
can't be detected from the schema alone) — see the extensive comments there
before adding a new one, and `tests/test_migrations.py` for how an old-schema
database is hand-built and asserted to survive being opened by new code.

**`utils/db_helpers.py`**: shared inventory/balance/stock/fee helpers used
across cogs, all taking a `_Executor` (either a `Database` or a
`Transaction`, so the same helper works standalone or inside a transaction).
`MACHINES = ("furnace", "blast_furnace", "factory", "press", "scrapper")` —
these five share uniform `server_config` column naming (`<machine>_level`,
`_fee`, `_fees_collected`, `_max_queue`) and the `production_jobs` queue
table, which is what lets `queue_room`, `apply_machine_upgrades`, etc. be one
implementation instead of five. A sixth machine should follow this pattern:
the blast furnace (1.3) needed nothing from this module but its entry in that
tuple and its default fee in `ensure_server_row`.

What a machine COUNTS in is not uniform. Four of them count items; the blast
furnace counts batches of `BLAST_FURNACE_BATCH_SIZE` items — in
`production_jobs.quantity`, in its fee and in its queue cap alike. The helpers
that render those numbers to a player take a `unit` argument for that reason
(`queue_full_message`, `queue_field_name`, `queue_limit_field_value`).

Every fee a cog charges is banked through `bank_infrastructure_fee`, which is
the single place a fee turns into progress: it credits
`<machine>_fees_collected`, re-levels that machine, and re-checks the server's
mining slots. Do not write that UPDATE in a cog. Mining slots (1.3) are the
reason it exists — the cap on drills per player per server is unlocked by the
SUM of all five machines' lifetime fees (`mining_slot_status`), so a rule that
had been copy-pasted at seven fee sites was about to be copy-pasted at eight.
The cap is derived on read and never stored; the only stored column,
`mining_slots_announced`, dedupes the unlock notice and nothing else. See
docs/mining.txt.

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
workflow (branch → test in beta → merge to `main` → `/opt/dragonhoard/
update.sh` to ship), backup/rollback procedure, and how to copy live data
into beta for testing against real data. Key point: `BOT_ENVIRONMENT=beta`
in `.env` is what flips `config.IS_BETA`, which selects `beta_id` emoji and
syncs commands to `DEV_GUILD_ID` instead of globally.

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
behind the numbers lives there, not in code comments.

Market prices are static as of 1.3 and are whole numbers of cents, enforced
at import by `MARKET_PRICE_CENTS`. Three constants now hold each other up and
should be changed together or not at all: the price table, `MARKET_BUY_MARKUP`
(2), and `JOB_BOARD_TARGET_PAYOUT` (1.00). The job board pays per completion
with no daily cap, and what stops that printing currency is only that buying
the goods back costs twice what selling them paid — see docs/market.md
section 1.
