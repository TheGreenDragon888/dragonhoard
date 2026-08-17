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

-- Per-server settings: the server's custom currency name/emoji, furnace/
-- factory levels, and its shared raw-material mining pool.
CREATE TABLE IF NOT EXISTS server_config (
    guild_id            INTEGER PRIMARY KEY,
    currency_name       TEXT,
    currency_emoji      TEXT,
    furnace_level       INTEGER NOT NULL DEFAULT 1,
    factory_level       INTEGER NOT NULL DEFAULT 1,
    -- Keep these DEFAULTs in sync with DEFAULT_FURNACE_FEE / DEFAULT_FACTORY_FEE
    -- in config.py (used for databases created before a default changed).
    furnace_fee         REAL NOT NULL DEFAULT 0.01,
    factory_fee         REAL NOT NULL DEFAULT 0.25,
    furnace_fees_collected REAL NOT NULL DEFAULT 0.0,
    factory_fees_collected REAL NOT NULL DEFAULT 0.0,
    furnace_max_queue   INTEGER NOT NULL DEFAULT 25,
    factory_max_queue   INTEGER NOT NULL DEFAULT 5,
    -- The hydraulic press. press_fee is the fee for ONE ruby-equivalent of
    -- press time; a recipe pays it multiplied by its press_days, so a diamond
    -- costs nine times a ruby. Keep the DEFAULT in sync with
    -- DEFAULT_PRESS_FEE in config.py.
    press_level         INTEGER NOT NULL DEFAULT 1,
    press_fee           REAL NOT NULL DEFAULT 5.0,
    press_fees_collected REAL NOT NULL DEFAULT 0.0,
    press_max_queue     INTEGER NOT NULL DEFAULT 1,
    -- Fractional press-days carried between ticks. Unlike the furnace and
    -- factory, which keep their accumulator in memory, this one is persisted:
    -- press jobs run for days, so an in-memory total reset by every restart
    -- would mean a diamond never finishes on a bot that restarts weekly.
    press_progress      REAL NOT NULL DEFAULT 0.0,
    -- The scrapper: recycles components, containers and drills back into the
    -- materials they were made from. Keep scrapper_fee's DEFAULT in sync with
    -- DEFAULT_SCRAPPER_FEE in config.py.
    scrapper_level          INTEGER NOT NULL DEFAULT 1,
    scrapper_fee            REAL NOT NULL DEFAULT 0.10,
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
    -- Lifetime faucet/sink running totals for this server's currency, per
    -- docs/market.md section 4. Minted by the market buying materials from
    -- users and by the daily job board's bonus; burned by every machine's
    -- fees, by /donate infrastructure, and by the market selling materials
    -- back to users.
    currency_minted_total    REAL NOT NULL DEFAULT 0.0,
    currency_burned_total    REAL NOT NULL DEFAULT 0.0
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
-- materials are ever stored here; components/drills are not tradeable.
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
-- read, because both derive from member count: without that, someone joining
-- halfway through the day would move the goalposts on a player already partway
-- through the task.
CREATE TABLE IF NOT EXISTS daily_jobs (
    guild_id        INTEGER NOT NULL,
    -- ISO date on the board's OWN clock (midnight America/Phoenix) - see
    -- JOB_BOARD_TIMEZONE in utils/job_board.py. Compared as text, so ISO is
    -- load-bearing.
    job_date        TEXT NOT NULL,
    material_id     TEXT NOT NULL,
    -- Both frozen at posting time. They derive from the server's stock of the
    -- material, which the day's own selling moves constantly, so recomputing
    -- either on read would grow the task under someone partway through it.
    quantity        INTEGER NOT NULL,   -- fewest units paying JOB_BOARD_TARGET_PAYOUT, capped
    reward          REAL NOT NULL,      -- data/materials.py: job_reward
    posted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, job_date)
);

-- One row per user who has sold ANY of the day's material, so progress
-- accumulates across as many /market sell calls as it takes. claimed_at is the
-- once-per-user guard: the payout UPDATE carries "claimed_at IS NULL" in its
-- WHERE clause, so two sells racing can't both pay the reward out.
CREATE TABLE IF NOT EXISTS daily_job_progress (
    guild_id        INTEGER NOT NULL,
    job_date        TEXT NOT NULL,
    user_id         INTEGER NOT NULL,
    sold            INTEGER NOT NULL DEFAULT 0,
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
    -- Fractional carry between harvest ticks. A tick is 24 minutes (2.5
    -- ticks/hour), so a tick's share of a drill's hourly rate is generally a
    -- fraction of an item - banking the remainder here is what stops a level's
    -- bonus being rounded away.
    harvest_progress REAL NOT NULL DEFAULT 0.0,
    is_full          INTEGER NOT NULL DEFAULT 0,    -- 0/1 boolean: stopped until /collect
    -- production_jobs.job_id of the queued job acting on this drill - a
    -- /factory upgrade or a /scrapper drill - else NULL. A locked drill can't
    -- be placed, removed, attached to, or queued a second time.
    locked_job_id    INTEGER,
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

-- A queued furnace (smelting), factory (crafting), press or scrapper job for a
-- user in a guild. target_id is the material_id being produced or broken down
-- (e.g. "iron", "wiring", "ruby"), or one of the two drill sentinels below.
CREATE TABLE IF NOT EXISTS production_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    job_type        TEXT NOT NULL CHECK (job_type IN ('furnace', 'factory', 'press', 'scrapper')),
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
