-- schema.sql
-- Executed once at bot startup (see database/db.py). SQLite creates the file
-- and these tables if they don't already exist. Re-running this on an
-- existing database is safe because of "IF NOT EXISTS".

-- One row per Discord user, tracked globally (not per-server), matching the
-- design doc's rule that DragonCoin and raw materials are stored per-user,
-- not per-server.
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,   -- Discord snowflake ID
    dragoncoin      REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A user's stockpile of a given material (raw, smelted, or component).
-- material_id references a hardcoded key in data/materials.py (e.g. "iron_ore").
CREATE TABLE IF NOT EXISTS user_materials (
    user_id         INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, material_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Per-server settings: the server's custom currency name/emoji, machine
-- levels, and its shared raw-material mining pool.
CREATE TABLE IF NOT EXISTS server_config (
    guild_id            INTEGER PRIMARY KEY,
    currency_name       TEXT,
    currency_emoji      TEXT,
    furnace_level       INTEGER NOT NULL DEFAULT 1,
    factory_level       INTEGER NOT NULL DEFAULT 1,
    furnace_fees_collected REAL NOT NULL DEFAULT 0.0,
    factory_fees_collected REAL NOT NULL DEFAULT 0.0,
    furnace_max_queue   INTEGER NOT NULL DEFAULT 25,
    factory_max_queue   INTEGER NOT NULL DEFAULT 5,
    -- The blast furnace: an auxiliary furnace that smelts in batches of 100
    -- (data/materials.py: BLAST_FURNACE_BATCH_SIZE). Everything here is
    -- counted in BATCHES, not items - the fee is charged per batch and the
    -- queue cap is measured in them (config.py: DEFAULT_BLAST_FURNACE_FEE).
    blast_furnace_level          INTEGER NOT NULL DEFAULT 1,
    blast_furnace_fees_collected REAL NOT NULL DEFAULT 0.0,
    blast_furnace_max_queue      INTEGER NOT NULL DEFAULT 5,
    -- The hydraulic press. Its fee is charged per ruby-equivalent of press
    -- time; a recipe pays it multiplied by its press_days, so a diamond costs
    -- nine times a ruby (config.py: DEFAULT_PRESS_FEE).
    press_level         INTEGER NOT NULL DEFAULT 1,
    press_fees_collected REAL NOT NULL DEFAULT 0.0,
    press_max_queue     INTEGER NOT NULL DEFAULT 1,
    -- Fractional press-days carried between ticks. Unlike the furnace and
    -- factory, which keep their accumulator in memory, this one is persisted:
    -- press jobs run for days, so an in-memory total reset by every restart
    -- would mean a diamond never finishes on a bot that restarts weekly.
    press_progress      REAL NOT NULL DEFAULT 0.0,
    -- The scrapper: recycles components, containers and drills back into the
    -- materials they were made from.
    scrapper_level          INTEGER NOT NULL DEFAULT 1,
    scrapper_fees_collected REAL NOT NULL DEFAULT 0.0,
    scrapper_max_queue      INTEGER NOT NULL DEFAULT 5,
    -- The one channel Dragonhoard answers in. NULL (the default) means it
    -- answers anywhere, which is what every server starts out doing. Set with
    -- /setup channel; cleared automatically if that channel is deleted. See
    -- utils/channel_guard.py for what is and isn't restricted.
    bot_channel_id          INTEGER,
    -- 0/1 boolean: whether bot responses are public in this server instead of
    -- ephemeral (private). Off by default - see utils/responses.py.
    public_messages         INTEGER NOT NULL DEFAULT 0,
    -- 0/1 boolean: whether the "set your currency up" prompt has been posted in
    -- this server. It fires once on joining and stays fired, so re-inviting the
    -- bot doesn't re-nag a server that has already been told - and a server
    -- that deliberately runs without a named currency isn't pestered forever.
    setup_prompt_sent       INTEGER NOT NULL DEFAULT 0,
    -- 0/1 boolean: whether Dragonhoard is currently in this server. The row is
    -- kept rather than deleted when it's removed, so balances and market stock
    -- survive intact and come back if the bot is re-invited. What changes is
    -- that a departed server's currency stops appearing in /balance and
    -- /inventory, and its placed drills are returned to their owners.
    bot_present             INTEGER NOT NULL DEFAULT 1,
    -- How many raw materials are left in this server's current mining bag. The
    -- authoritative total; server_mining_pool holds the same figure broken down
    -- by material and the two must agree. There is deliberately no daily top-up
    -- and no cap - the bag refills when it empties (utils/mining_pool.py).
    mining_pool_remaining    INTEGER NOT NULL DEFAULT 0,
    -- The highest mining slot level this server has been TOLD about (see
    -- utils/db_helpers.py: announce_mining_slot_unlocks). Not the level itself:
    -- the cap is derived on read from the fees this server has collected
    -- (utils/db_helpers.py: fees_collected, the <machine>_fees_collected
    -- columns added together), so it is always current and needs no column.
    -- This one exists purely so a server notice fires once per unlock rather
    -- than on every fee paid afterwards. 1 is the level every server starts at,
    -- so a fresh row has nothing outstanding to announce.
    mining_slots_announced   INTEGER NOT NULL DEFAULT 1,
    -- Lifetime faucet/sink running totals for this server's currency, per
    -- docs/market.md section 4. Minted by the market buying materials from
    -- users and by the daily job board's bonus; burned by every machine's
    -- fees, by /donate infrastructure, and by the market selling materials
    -- back to users.
    currency_minted_total    REAL NOT NULL DEFAULT 0.0,
    currency_burned_total    REAL NOT NULL DEFAULT 0.0,
    -- The server government (1.4, docs/government.md). Everything from here
    -- down is set by the elected Mayor and Treasurer, never by an admin.
    --
    -- A machine's fee is its config.py default times its multiplier, one of
    -- utils/government.py: FEE_MULTIPLIERS. There is no stored fee: the base
    -- is the codebase default, so retuning a default moves every server at
    -- once. Each <machine>_fee_changed is the game date (the job board's
    -- America/Phoenix day) that multiplier last changed, because each setting
    -- may change once per game day.
    furnace_fee_multiplier       REAL NOT NULL DEFAULT 1.0,
    blast_furnace_fee_multiplier REAL NOT NULL DEFAULT 1.0,
    factory_fee_multiplier       REAL NOT NULL DEFAULT 1.0,
    press_fee_multiplier         REAL NOT NULL DEFAULT 1.0,
    scrapper_fee_multiplier      REAL NOT NULL DEFAULT 1.0,
    furnace_fee_changed          TEXT,
    blast_furnace_fee_changed    TEXT,
    factory_fee_changed          TEXT,
    press_fee_changed            TEXT,
    scrapper_fee_changed         TEXT,
    -- Infrastructure Enhancements bought for each machine; each doubles its
    -- speed (data/materials.py: enhancement_speed).
    furnace_enhancement_level       INTEGER NOT NULL DEFAULT 0,
    blast_furnace_enhancement_level INTEGER NOT NULL DEFAULT 0,
    factory_enhancement_level       INTEGER NOT NULL DEFAULT 0,
    press_enhancement_level         INTEGER NOT NULL DEFAULT 0,
    scrapper_enhancement_level      INTEGER NOT NULL DEFAULT 0,
    -- The share of every machine fee that goes to the government instead of
    -- being burned, in whole percent, and the bond premium in whole percent.
    tax_percent          INTEGER NOT NULL DEFAULT 0,
    tax_changed          TEXT,
    bond_rate_percent    INTEGER NOT NULL DEFAULT 0,
    bond_rate_changed    TEXT,
    -- Currency the government holds. NOT burned: utils/db_helpers.py:
    -- circulating_currency adds both back, as it does order and bet escrow.
    -- The treasury is what the Mayor spends; the repayment pool is tax
    -- collected while the server owes bondholders, paid out hourly.
    treasury             REAL NOT NULL DEFAULT 0.0,
    repayment_pool       REAL NOT NULL DEFAULT 0.0,
    -- NULL = vacant.
    mayor_id             INTEGER,
    treasurer_id         INTEGER,
    -- How much of the Mayor's current bond sale is still unsold, in cents.
    bond_sale_cents      INTEGER NOT NULL DEFAULT 0,
    -- Mining slot progress bought by the government rather than paid as a
    -- machine fee: every government burn once, Mining Slot Enhancement
    -- MINING_SLOT_ENHANCEMENT_MULTIPLIER times over. Added into the slot
    -- total by utils/db_helpers.py: slot_progress.
    mining_slot_credit   REAL NOT NULL DEFAULT 0.0,
    -- When the running Server Bonanza ends, UTC in datetime('now')'s layout.
    -- NULL, or in the past, means none is running.
    bonanza_until        TEXT,
    -- The last voting day (a Thursday, as a game date) whose votes have been
    -- counted, and the last one whose opening was announced. Each is what
    -- makes its step run once per week however many times it is reached.
    election_counted     TEXT,
    election_announced   TEXT
);

-- A user's balance of ONE specific server's custom currency. Unlike
-- DragonCoin (global), this is scoped per (guild, user).
CREATE TABLE IF NOT EXISTS server_currency_balances (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    balance         REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (guild_id, user_id)
);

-- The server's own material storage - the market's inventory, acquired from
-- and sold back to users (docs/market.md section 3). Only raw and smelted
-- materials are ever stored here.
--
-- That is a rule about what the SERVER trades, not about what is tradeable.
-- Since 1.4 players trade components, containers, gemstones and drills with
-- each other through market_listings and market_orders below; none of it ever
-- passes through here, because the server is not a party to those trades.
CREATE TABLE IF NOT EXISTS server_material_storage (
    guild_id        INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, material_id)
);

-- The daily job board: one task per server per day on the board's own Arizona
-- clock (see job_date below), asking players to sell the market a material it's
-- short of. Posted lazily the first time anyone looks at the board or sells
-- into it (see utils/job_board.py) rather than by a background loop - a task
-- nobody has looked at has nothing to accrue, so a loop would only be one more
-- thing to keep running.
--
-- quantity and reward are frozen at posting time rather than recomputed on
-- read, so the task somebody is partway through stays the task they started
-- even across a balance retune. Since 1.3 neither derives from the server's
-- stock or size at all: quantity is a constant of the material and reward is a
-- flat JOB_BOARD_TARGET_PAYOUT per completion.
CREATE TABLE IF NOT EXISTS daily_jobs (
    guild_id        INTEGER NOT NULL,
    -- ISO date on the board's OWN clock (midnight America/Phoenix) - see
    -- JOB_BOARD_TIMEZONE in utils/job_board.py. Compared as text, so ISO is
    -- load-bearing.
    job_date        TEXT NOT NULL,
    material_id     TEXT NOT NULL,
    -- Both frozen at posting time (see above).
    quantity        INTEGER NOT NULL,   -- units per completion: data/materials.py: job_quantity
    reward          REAL NOT NULL,      -- paid PER completion: JOB_BOARD_TARGET_PAYOUT
    posted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, job_date)
);

-- One row per user who has sold ANY of the day's material, so progress
-- accumulates across as many /market sell calls as it takes.
--
-- claims_paid is how many completions have already been paid for. Since 1.3
-- the bonus is paid per completion rather than once a day, so this is the
-- running count rather than the boolean it replaced: a payout is worth
-- sold/quantity - claims_paid completions, and banking that difference in the
-- same statement is what makes each completion pay exactly once.
CREATE TABLE IF NOT EXISTS daily_job_progress (
    guild_id        INTEGER NOT NULL,
    job_date        TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    sold            INTEGER NOT NULL DEFAULT 0,
    claims_paid     INTEGER NOT NULL DEFAULT 0,
    -- When the most recent completion was paid. Kept as a record of when
    -- someone last finished a task; claims_paid is what the payout reads.
    claimed_at      TEXT,
    PRIMARY KEY (guild_id, job_date, user_id)
);

-- One-off notices shown to a player the next time they use the bot, once each.
-- Two scopes, which are deliberately independent feeds rather than one list
-- with a filter: 'global' is an announcement or disclaimer from the bot itself
-- and is read once per USER, 'server' belongs to one guild and is read once per
-- user PER GUILD, so somebody in five servers sees a global notice once and
-- each server's notice once.
--
-- Only the newest notice of each scope is ever shown (see utils/notifications.py
-- - the read marker stores an id, so anything older is skipped rather than
-- queued up). That is a brevity rule, not a retention one: superseded rows stay
-- here as a record of what was announced and when.
CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope           TEXT NOT NULL CHECK (scope IN ('global', 'server')),
    guild_id        INTEGER,               -- NULL for global, the guild for 'server'
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    -- Stable identifier for a notice that ships WITH a release as data
    -- (data/notifications.py) rather than being posted at runtime. Seeding is
    -- an INSERT OR IGNORE on this column, so restarting the bot - which reseeds
    -- every time - can't repost the same announcement to everyone. NULL for
    -- notices raised at runtime by a feature.
    notice_key      TEXT UNIQUE,
    -- What a reader can DO about this notice, as an opaque "<kind>:<id>"
    -- string, or NULL for the overwhelming majority that are just text.
    -- utils/responses.py hands it to whichever feature registered that kind and
    -- puts the buttons it builds on the same reply the notice rides in on; see
    -- the action registry there.
    --
    -- It lives on the notice rather than being looked up per command because
    -- respond() has already fetched this row - a notice's delivery is the one
    -- query that runs on every successful command in the bot, and asking a
    -- second question there ("does this server have an open bet?") would charge
    -- every command in every server for a feature most of them never use. A
    -- column on a row already in hand costs nothing.
    action_key      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- A global notice belongs to no guild and a server notice must name one.
    -- Without this a 'global' row carrying a guild_id would be invisible to
    -- both lookups at once.
    CHECK ((scope = 'global') = (guild_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_notifications_scope ON notifications(scope, guild_id);

-- How far through each feed a user has read. One row per (user, feed), where
-- the feed is a guild_id or 0 for the global one.
--
-- 0 is a sentinel rather than NULL because SQLite treats NULLs in a PRIMARY KEY
-- as distinct from each other, so a nullable column here would let one user
-- accumulate unlimited global markers and be shown the same announcement on
-- every single command forever. Discord snowflakes are never 0.
CREATE TABLE IF NOT EXISTS notification_reads (
    user_id         INTEGER NOT NULL,
    guild_id        INTEGER NOT NULL,   -- 0 = the global feed
    last_seen_id    INTEGER NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);

-- Personal one-off notices: something that happened to ONE player, shown to
-- them once, the next time they use the bot. The first gemstone of a kind
-- somebody mines or presses is the only thing that raises one so far - it
-- unlocks a command they have no other reason to know exists
-- (data/notifications.py: GEM_UNLOCK_NOTICES).
--
-- Deliberately its own table rather than a third scope on `notifications`.
-- The two scopes there are BROADCASTS with a per-feed watermark: everyone gets
-- the same row, only the newest is shown, and notification_reads stores how far
-- through the feed each reader is. A personal notice is not a broadcast - the
-- row belongs to one player - so its read state belongs on the row itself, and
-- nothing about it should be skipped for being superseded. A player who earns a
-- ruby and an obsidian before next running a command has two things to be told,
-- not one.
--
-- notice_key is what makes raising one idempotent: it is part of the primary
-- key, so the INSERT OR IGNORE that posts "your first ruby" is a no-op on every
-- ruby after it, and the row itself is the record that this player has been
-- told. That is the same job notifications.notice_key does for a shipped
-- announcement, and it means no separate "have they been told yet" column has
-- to be kept in step with anything.
CREATE TABLE IF NOT EXISTS user_notifications (
    user_id     INTEGER NOT NULL,
    notice_key  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    -- NULL until the player has actually been shown it. Set after the reply
    -- sends, not before - see utils/notifications.py on at-least-once.
    seen_at     TEXT,
    PRIMARY KEY (user_id, notice_key)
);
-- Partial index: every read of this table asks for one player's UNSEEN notices,
-- and the seen rows are kept forever as the dedupe record, so they would
-- otherwise grow the thing the hot path scans.
CREATE INDEX IF NOT EXISTS idx_user_notifications_unseen
    ON user_notifications(user_id) WHERE seen_at IS NULL;

-- One row per drill for that drill's entire lifetime. A drill is never a
-- fungible stack in user_materials, because its level and attached container
-- have to survive being unplaced - so it gets an identity the moment it's
-- crafted and keeps it. guild_id NULL means the drill is sitting in its
-- owner's inventory; non-NULL means it's placed and mining in that server
-- (mining is server-wide, not channel-scoped). drill_type and container_type
-- reference data/materials.py (e.g. "iron_drill", "steel_container").
CREATE TABLE IF NOT EXISTS drills (
    drill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER,                       -- NULL = unplaced, in inventory
    owner_id         INTEGER NOT NULL,
    drill_type       TEXT NOT NULL,
    -- Each level past 1 adds a fifth of the drill type's base mining rate, so
    -- an upgrade is worth the same proportion at every tier (LEVEL_RATE_ANCHOR
    -- in data/materials.py).
    level            INTEGER NOT NULL DEFAULT 1,
    container_type   TEXT,                          -- NULL = no container attached
    stored_amount    INTEGER NOT NULL DEFAULT 0,    -- raw materials waiting for /collect
    -- Fractional carry between harvest ticks. A tick is 5 minutes
    -- (cogs/mining.py: HARVEST_TICK_MINUTES), so a tick's share of a drill's
    -- hourly rate is generally a fraction of an item - banking the remainder
    -- here is what stops a level's bonus being rounded away.
    harvest_progress REAL NOT NULL DEFAULT 0.0,
    is_full          INTEGER NOT NULL DEFAULT 0,    -- 0/1 boolean: stopped until /collect
    -- The moment this drill's mining has been credited up to, as
    -- datetime('now') text: written by every harvest tick, and reset to "now"
    -- by anything that STARTS the drill mining - /mine place, or a /collect or
    -- a container that frees up a full one - so that time it wasn't mining is
    -- never paid for. Before it existed every tick credited a whole tick to
    -- every drill, including one placed or emptied a second earlier. NULL on
    -- a drill no tick has reached yet, which reads as one whole tick
    -- (utils/db_helpers.py: elapsed_work_hours) - what it always got.
    mined_until      TEXT,
    -- production_jobs.job_id of the queued job acting on this drill - a
    -- /factory upgrade or a /scrapper drill - else NULL. A locked drill can't
    -- be placed, removed, attached to, or queued a second time.
    locked_job_id    INTEGER,
    -- When this drill was last placed, as datetime('now') text. What voting
    -- eligibility is measured against (utils/government.py: can_vote): the
    -- free starter drill means any account can have A drill placed within one
    -- command, so the rule is a drill that has been placed for a week.
    -- Not cleared when the drill is pulled out - only read together with a
    -- guild_id match, and rewritten by the next placement.
    --
    -- NULL on a PLACED drill means it was placed before this column existed,
    -- and counts as placed long enough. Nobody could have placed a drill to
    -- win an election that did not exist yet, and backfilling "now" instead
    -- would have shut every existing player out of the first election. So
    -- anything that places a drill must write this, or it hands out that
    -- same grandfathering (cogs/mining.py: /mine place is the one place).
    placed_at        TEXT,
    -- market_listings.listing_id of the listing offering this drill for sale,
    -- else NULL. A listed drill can't be placed, removed, attached to, or
    -- queued, exactly as a locked one can't.
    --
    -- DELIBERATELY NOT locked_job_id, which would have been the obvious reuse.
    -- utils/drills.py: release_stale_drill_locks frees any locked_job_id that
    -- doesn't name a live production_jobs row, and a listing is not a job - so
    -- a drill escrowed there would be handed back to its owner, still listed,
    -- the next time that sweep ran.
    listed_id        INTEGER,
    CHECK (level >= 1),
    -- Buys back what dropping "guild_id NOT NULL" gave up: an unplaced drill
    -- can't be holding materials or be flagged full.
    CHECK (guild_id IS NOT NULL OR (stored_amount = 0 AND is_full = 0))
);
CREATE INDEX IF NOT EXISTS idx_drills_owner ON drills(owner_id);
CREATE INDEX IF NOT EXISTS idx_drills_guild ON drills(guild_id);

-- What a drill is actually holding, by material. Added in 1.2.
--
-- Before it, a drill banked a bare count and only decided WHAT it had mined
-- when /collect rolled each item at handover. That was fine while every roll
-- was independent, and became impossible the moment the server's pool acquired
-- a finite composition: a guaranteed diamond sitting in a shared pool cannot be
-- drawn per-player at collection time, because two players collecting would
-- each draw their own copy of it.
--
-- drills.stored_amount is kept as the total and MUST equal SUM(quantity) here
-- for that drill. It is denormalised on purpose - capacity, is_full, the CHECK
-- on unplaced drills and /mine status are all counts, and rewriting them to
-- aggregate this table would buy nothing. Every write to one goes in the same
-- transaction as the write to the other; tests/test_mining_focus.py pins that
-- they agree.
CREATE TABLE IF NOT EXISTS drill_contents (
    drill_id        INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    PRIMARY KEY (drill_id, material_id),
    FOREIGN KEY (drill_id) REFERENCES drills(drill_id)
);

-- The composition of a server's mining bag: how many of each raw material are
-- actually sitting in it. Added in 1.2 alongside drill_contents.
--
-- This table is what makes the gemstone guarantee a guarantee. A drill draws
-- from these real counts without replacement (data/materials.py:
-- draw_from_pool), so a diamond is a single object somebody will dig up before
-- the bag empties rather than a chance re-rolled forever. There is no accrual
-- and no clock: utils/mining_pool.py refills the bag the moment it runs out.
--
-- As with drill_contents, server_config.mining_pool_remaining stays as the
-- authoritative TOTAL and must equal SUM(quantity) here for that guild.
CREATE TABLE IF NOT EXISTS server_mining_pool (
    guild_id        INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, material_id)
);

-- A player's mining focus: which ore everything they mine arrives as. Global
-- per user, not per server, matching user_materials - /collect empties drills
-- across every server in one call, so a per-server focus would convert each
-- drill's haul differently inside one receipt.
--
-- THE ROW'S EXISTENCE IS THE UNLOCK. A player with no row is on the default
-- focus and has never paid the ruby; inserting the row is what the payment buys.
-- There is deliberately no separate `unlocked` flag to fall out of sync with it.
--
-- `carry` is the fraction of the focus's primary ore still owed from rounding
-- (see apply_mining_focus). It must be reset to 0 whenever focus_id changes, or
-- a fraction of a copper owed under one focus is paid out as iron under the next.
CREATE TABLE IF NOT EXISTS user_mining_focus (
    user_id         INTEGER PRIMARY KEY,
    focus_id        TEXT NOT NULL,
    carry           REAL NOT NULL DEFAULT 0.0,
    -- ISO date of the last change, on the job board's Arizona clock, which is
    -- what rate-limits switching to once a day. Empty string means never
    -- changed since unlocking.
    last_changed    TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- A player's mining efficiency: which SMELTED material their haul is boosted
-- and re-proportioned for (data/materials.py: apply_mining_efficiency).
-- Separate from user_mining_focus in every sense - a player may have either,
-- both or neither, and the two features multiply. See docs/mining-efficiency.md.
--
-- As with the focus, THE ROW IS THE UNLOCK: no row means the obsidian has
-- never been paid and the player is on DEFAULT_MINING_EFFICIENCY.
CREATE TABLE IF NOT EXISTS user_mining_efficiency (
    user_id         INTEGER PRIMARY KEY,
    efficiency_id   TEXT NOT NULL,
    -- ISO date of the last change, on the job board's Arizona clock, same
    -- once-a-day rule the focus has. Empty string means never changed.
    last_changed    TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- The rounding remainders an efficiency owes, ONE PER MATERIAL rather than the
-- single float user_mining_focus keeps.
--
-- A focus has exactly one primary ore, so every fraction it owes is a fraction
-- of the same material and one column covers it. An efficiency's correction
-- produces fractions on both sides at once, and which materials those are
-- depends on the player's focus - so a single shared carry would pay a
-- fraction of a coal out as iron ore the first time the direction flipped.
--
-- Cleared wholesale when efficiency_id changes, for the same reason
-- user_mining_focus.carry is.
CREATE TABLE IF NOT EXISTS user_mining_efficiency_carry (
    user_id         INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    carry           REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (user_id, material_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- A player's mining affinity: which GEMSTONE every other gem they mine arrives
-- as (data/materials.py: apply_mining_affinity). The focus's mirror one tier
-- up, and the third independent member of that set - a player may have any of
-- the three, all of them or none. See docs/mining-affinity.md.
--
-- THE ROW IS THE UNLOCK, as with both siblings: no row means the diamond has
-- never been paid and the player is on DEFAULT_MINING_AFFINITY.
CREATE TABLE IF NOT EXISTS user_mining_affinity (
    user_id         INTEGER PRIMARY KEY,
    affinity_id     TEXT NOT NULL,
    -- Fractional units of affinity_id still owed from rounding. Unlike the two
    -- carries above this one is worth real money - up to 89 rubies of mining
    -- sits here when the target is a diamond - which is why it is CONVERTED to
    -- the new target on a change rather than reset the way
    -- user_mining_focus.carry is, and why every whole unit it holds is paid out
    -- at that moment (utils/mining_affinity.py: set_affinity).
    carry           REAL NOT NULL DEFAULT 0.0,
    -- ISO date of the last change, on the job board's Arizona clock, the same
    -- once-a-day rule the focus and the efficiency have.
    last_changed    TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- A queued furnace (smelting), blast furnace (bulk smelting), factory
-- (crafting), press or scrapper job for a user in a guild. target_id is the
-- material_id being produced or broken down (e.g. "iron", "wiring", "ruby"),
-- or one of the two drill sentinels below.
--
-- quantity is counted in whatever unit the machine charges and drains in,
-- which for a 'blast_furnace' row is BATCHES of BLAST_FURNACE_BATCH_SIZE
-- items rather than single items.
CREATE TABLE IF NOT EXISTS production_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    job_type        TEXT NOT NULL CHECK (job_type IN ('furnace', 'blast_furnace', 'factory', 'press', 'scrapper')),
    target_id       TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_progress', 'complete')),
    -- Set only on the two job kinds that act on one specific drill rather than
    -- on a stack of some material: a 'factory' job whose target_id is the
    -- DRILL_UPGRADE_JOB_TARGET sentinel, and a 'scrapper' job whose target_id
    -- is DRILL_SCRAP_JOB_TARGET. Points at the drills row being upgraded or
    -- broken down, which is locked (drills.locked_job_id) until the job ends.
    target_drill_id INTEGER
);
-- Every hot read of this table asks for LIVE jobs - the five processing loops
-- each tick, queue_room on every queue command, the status embeds - and a
-- finished job is kept as a row with status 'complete' (for
-- utils/db_helpers.py: COMPLETED_JOB_HISTORY_DAYS) rather than deleted on the
-- spot. Without this the live-job lookups scanned every finished job the server
-- had ever run, which is the one table here that grows with play. PARTIAL, so
-- it holds only the live rows and stays a few entries long however long the
-- history behind it gets; SQLite uses it only for queries whose WHERE clause
-- carries the same `status != 'complete'` term, which is why every live-job
-- query spells the condition exactly that way. Keep in step with the copy in
-- database/db.py's production_jobs rebuild, which recreates it.
CREATE INDEX IF NOT EXISTS idx_production_jobs_live
    ON production_jobs (job_type, guild_id) WHERE status != 'complete';

-- What was PRODUCED, as opposed to what currency changed hands. server_config
-- has held the faucet/sink totals (currency_minted_total, currency_burned_total
-- and the five <machine>_fees_collected columns) since 1.1, but nothing has ever
-- recorded goods, which is what /economy's GDP figure is summed from. See
-- docs/market.md section 5 for the value-added model this table serves.
--
-- THERE IS NOTHING TO BACKFILL. The events this records were never written down
-- anywhere, so the table starts empty on every existing database and GDP is only
-- meaningful from the moment 1.4 ships - which is why /economy shows a "tracked
-- since" date rather than implying a lifetime total.
--
-- One row per production event, appended and never updated. Value added is
-- output_value - input_value, DERIVED on read rather than stored, so a balance
-- retune can't leave two disagreeing numbers in one row.
CREATE TABLE IF NOT EXISTS production_ledger (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Where the work physically happened, which for mining is drills.guild_id -
    -- the pool the ore came out of - and NOT interaction.guild_id. /collect
    -- empties a player's drills in every server at once (cogs/mining.py), so one
    -- invocation writes rows for several guilds; attributing them to the server
    -- the command was typed in would credit it with every other server's ore.
    guild_id     INTEGER NOT NULL,
    occurred_at  TEXT NOT NULL DEFAULT (datetime('now')),   -- UTC, like every other timestamp here
    source       TEXT NOT NULL CHECK (source IN
                     ('mining', 'furnace', 'blast_furnace', 'factory', 'press', 'scrapper')),
    material_id  TEXT NOT NULL,      -- what came out
    quantity     INTEGER NOT NULL,   -- in ITEMS, even for the blast furnace, which queues in batches
    -- Both valued at data/materials.py market prices, which are global constants
    -- and identical on every server - so GDP is denominated in the server's own
    -- currency and is still comparable between two servers, unlike a balance
    -- total. A material the market does not price (components, drills,
    -- containers, ultra dense matter) contributes 0 rather than an invented
    -- figure; see docs/market.md section 5.
    output_value REAL NOT NULL,
    input_value  REAL NOT NULL,      -- 0 for mining, which consumes nothing
    is_gemstone  INTEGER NOT NULL DEFAULT 0
);
-- Every read is one guild's rows over a recent window, which is exactly this.
CREATE INDEX IF NOT EXISTS idx_ledger_guild_time ON production_ledger (guild_id, occurred_at);

-- The player market's sell side: goods a player has offered to the server's
-- other members at a price of their own (1.4). Per-guild, because the currency
-- they are priced in is.
--
-- THE GOODS ARE ESCROWED HERE. Listing deducts them from user_materials (or
-- claims the drill via drills.listed_id) and the listing row is where they live
-- until it fills or is cancelled. Without that a player could list a stack and
-- sell the same stack to the server before anyone filled it, and the listing
-- would promise goods that had already gone.
--
-- A row holds EITHER a material stack or one drill, never both. A drill is not
-- a stack - it carries a level and a container that are most of its value - so
-- it is named by drill_id and its quantity is always 1.
CREATE TABLE IF NOT EXISTS market_listings (
    listing_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    seller_id   INTEGER NOT NULL,
    material_id TEXT,                 -- NULL on a drill listing
    drill_id    INTEGER,              -- NULL on a material listing
    -- Decremented as the listing fills; the row is deleted when it reaches 0.
    quantity    INTEGER NOT NULL,
    -- Per unit, in ten-thousandths of a currency unit (data/materials.py:
    -- PLAYER_PRICE_SCALE). An integer for the same reason MARKET_PRICE_CENTS
    -- is: a price is multiplied by a quantity up to a million, and a float
    -- would drift off its own sub-cent before it got there.
    price_units INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((material_id IS NULL) != (drill_id IS NULL)),
    CHECK (drill_id IS NULL OR quantity = 1),
    CHECK (quantity > 0 AND price_units > 0)
);
-- Every read is one guild's book for one material, cheapest first.
CREATE INDEX IF NOT EXISTS idx_listings_book
    ON market_listings (guild_id, material_id, price_units);
CREATE INDEX IF NOT EXISTS idx_listings_seller ON market_listings (seller_id);

-- The player market's buy side: standing bids for a material at a price the
-- buyer has already paid for.
--
-- THE CURRENCY IS ESCROWED HERE, deducted from the buyer's balance when the
-- order is placed, for the same reason the goods are above: an order that
-- promises payment the buyer has since spent is an order that cannot be filled.
--
-- That escrow is NOT a burn. It has left server_currency_balances but not the
-- economy, so utils/db_helpers.py: circulating_currency adds it back - see
-- docs/market.md section 4.
--
-- No drill orders. An order names a KIND of thing, and a drill is never just
-- its kind, so "I bid 250 for a Steel Drill" is not a well-formed offer.
-- Drills are listing-side only.
CREATE TABLE IF NOT EXISTS market_orders (
    order_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    buyer_id    INTEGER NOT NULL,
    material_id TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    price_units INTEGER NOT NULL,     -- per unit, PLAYER_PRICE_SCALE
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (quantity > 0 AND price_units > 0)
);
-- Every read is one guild's book for one material, dearest first.
CREATE INDEX IF NOT EXISTS idx_orders_book
    ON market_orders (guild_id, material_id, price_units);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON market_orders (buyer_id);

-- A server prediction bet (1.4): somebody proposes an outcome, players back it
-- or oppose it, and an admin decides which way it went. See docs/betting.md.
--
-- NO CURRENCY IS CREATED OR DESTROYED BY ANY OF THIS, which is the constraint
-- the whole feature is built around. The pot is exactly what was staked, the
-- winners split exactly the pot, and a cancelled bet hands back exactly what it
-- took. A house cut would have made this a sink in the sense docs/market.md
-- section 1 likes, and was ruled out for that reason - see docs/betting.md.
--
-- STAKES ARE ESCROWED on the wager row below, the same arrangement
-- market_orders has and for the same reason: a bet that promises currency the
-- better has since spent at /market buy is a bet that cannot pay out. As there,
-- the escrow is NOT a burn - utils/db_helpers.py: circulating_currency adds it
-- back.
CREATE TABLE IF NOT EXISTS prediction_bets (
    bet_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    creator_id  INTEGER NOT NULL,
    prediction  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    -- Absolute UTC in datetime('now')'s layout, frozen when the bet is opened
    -- rather than stored as a duration, for the reason daily_jobs freezes its
    -- quantity and reward: a restart or a retune must not move a deadline
    -- somebody has already bet against. Compared as text, so the layout is
    -- load-bearing.
    closes_at   TEXT NOT NULL,
    -- open      - taking wagers
    -- closed    - past closes_at, waiting on an admin to call it
    -- resolved  - called, and the pot has been paid out
    -- cancelled - voided, and every stake has been handed back
    --
    -- Nothing moves the row to 'closed' on a timer. Every path that touches a
    -- bet checks closes_at and flips it there, which is the same lazy approach
    -- the job board takes to posting the day's task: a bet nobody is looking at
    -- has nothing to accrue, so a loop would only be one more thing to keep
    -- running. See utils/betting.py: settle_status.
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'closed', 'resolved', 'cancelled')),
    outcome     TEXT CHECK (outcome IN ('for', 'against')),  -- NULL until resolved
    resolved_by INTEGER,
    resolved_at TEXT,
    -- A resolved bet names the side that won, and nothing else may. A cancelled
    -- bet has no outcome by definition: it was voided precisely because no
    -- outcome could be paid.
    CHECK ((status = 'resolved') = (outcome IS NOT NULL))
);
-- Every hot read asks for one guild's LIVE bets - the autocompletes, /bet
-- status, the open-bet cap - and a settled bet is kept as a row rather than
-- deleted, because the row is the record that the bet happened and what it
-- paid. PARTIAL for the same reason idx_production_jobs_live is: it holds only
-- the live rows however long the history behind it gets. SQLite uses it only
-- for queries whose WHERE carries the same `status IN ('open', 'closed')`
-- term, which is why every live-bet query spells the condition exactly that
-- way.
CREATE INDEX IF NOT EXISTS idx_prediction_bets_live
    ON prediction_bets (guild_id) WHERE status IN ('open', 'closed');

-- One player's position on one bet. THE STAKE LIVES HERE until the bet settles
-- (see above).
--
-- UNIQUE (bet_id, user_id) - not (bet_id, user_id, side) - is what enforces
-- "one side, locked in": a player picks a side with their first wager and
-- every later one tops up that same row. Letting somebody hold both sides
-- would be harmless to the pot arithmetic (it is their own money either way)
-- but it makes "who won" unanswerable for that player, and hedging a
-- prediction you yourself proposed is not what this is for.
--
-- stake_cents is an INTEGER for the reason market_listings.price_units is: the
-- payout apportions a pot between arbitrarily many winners and must come out
-- to exactly the pot, and a float cannot be relied on to add back up to
-- itself. Cents rather than PLAYER_PRICE_SCALE because a stake is a sum of
-- money a player typed, not a unit price that has to fit inside a band one
-- cent wide - see MARKET_PRICE_CENTS and PLAYER_PRICE_SCALE in
-- data/materials.py for the two scales and what each is for.
CREATE TABLE IF NOT EXISTS prediction_wagers (
    wager_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id       INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('for', 'against')),
    stake_cents  INTEGER NOT NULL CHECK (stake_cents > 0),
    placed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- What this wager was actually paid when the bet settled, in cents: the
    -- winner's share of the pot, the full stake back on a cancellation, 0 for a
    -- loser. NULL while the bet is live. Kept as the record of the payout
    -- rather than derived on read, because the pot it was a share of is gone
    -- the moment the bet settles - unlike production_ledger's value figures,
    -- which can be recomputed from prices that still exist.
    payout_cents INTEGER,
    UNIQUE (bet_id, user_id),
    FOREIGN KEY (bet_id) REFERENCES prediction_bets(bet_id)
);
-- Every read of this table is one bet's wagers: the pools on /bet status, the
-- apportionment at resolve time, the refunds at cancel time.
CREATE INDEX IF NOT EXISTS idx_prediction_wagers_bet ON prediction_wagers (bet_id);

-- The server government's elections (docs/government.md). One row per voter
-- per office per voting day; voting again replaces the row, which is how
-- "the latest vote counts" is enforced. Rows for a voting day are deleted once
-- it has been counted, so votes never carry into the next week.
--
-- cast_at breaks ties: of two candidates on the same count, the one whose
-- last vote arrived first reached that count first.
CREATE TABLE IF NOT EXISTS government_votes (
    guild_id      INTEGER NOT NULL,
    voting_day    TEXT NOT NULL,     -- the Thursday, as a game date
    office        TEXT NOT NULL CHECK (office IN ('mayor', 'treasurer')),
    voter_id      INTEGER NOT NULL,
    candidate_id  INTEGER NOT NULL,
    cast_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, voting_day, office, voter_id)
);

-- A bond: currency a player lent the server, repaid out of tax.
--
-- Integer cents for the reason prediction_wagers.stake_cents is: repayment
-- divides one pool between every creditor (utils/betting.py: apportion) and
-- has to add back up to exactly the pool.
--
-- owed_cents is principal plus the premium, fixed at sale; remaining_cents is
-- what is still to be paid. frozen marks a holder who has left the server:
-- payouts skip them and the debt cap ignores them until they are back.
-- tax_percent_at_sale is what stops the Treasurer cutting taxes below the
-- rate the latest bond was sold under while debt is owed.
CREATE TABLE IF NOT EXISTS government_bonds (
    bond_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    holder_id           INTEGER NOT NULL,
    principal_cents     INTEGER NOT NULL CHECK (principal_cents > 0),
    rate_percent        INTEGER NOT NULL,
    owed_cents          INTEGER NOT NULL,
    remaining_cents     INTEGER NOT NULL CHECK (remaining_cents >= 0),
    tax_percent_at_sale INTEGER NOT NULL,
    frozen              INTEGER NOT NULL DEFAULT 0,
    sold_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Every hot read is one guild's bonds still owed. A repaid bond is kept as the
-- record of what it paid, so the index is partial on the live ones, and every
-- query that should use it spells `remaining_cents > 0` literally.
CREATE INDEX IF NOT EXISTS idx_government_bonds_owed
    ON government_bonds (guild_id) WHERE remaining_cents > 0;

-- Tax collected per server per game day. What the bond debt cap is measured
-- against: debt may not exceed the previous 7 days' tax. Pruned past
-- utils/government.py: TAX_HISTORY_DAYS.
CREATE TABLE IF NOT EXISTS government_tax_daily (
    guild_id  INTEGER NOT NULL,
    day       TEXT NOT NULL,         -- game date
    amount    REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (guild_id, day)
);
