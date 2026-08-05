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
    -- 0/1 boolean: whether Dragonhoard is currently in this server. The row is
    -- kept rather than deleted when it's removed, so balances and market stock
    -- survive intact and come back if the bot is re-invited. What changes is
    -- that a departed server's currency stops appearing in /balance and
    -- /inventory, and its placed drills are returned to their owners.
    bot_present             INTEGER NOT NULL DEFAULT 1,
    -- The server-wide shared pool of unharvested raw materials that drills draw
    -- from. Topped up once/day by mining_pool_last_topup's date changing.
    mining_pool_remaining    INTEGER NOT NULL DEFAULT 0,
    mining_pool_last_topup   TEXT NOT NULL DEFAULT '',
    -- Lifetime faucet/sink running totals for this server's currency, per
    -- docs/market.md section 4. Minted only by the market buying materials
    -- from users; burned by furnace/factory fees and the market selling
    -- materials back to users.
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

-- The daily job board: one task per server per UTC day, asking players to sell
-- the market a material it's short of. Posted lazily the first time anyone
-- looks at the board or sells into it (see utils/job_board.py) rather than by a
-- background loop - unlike the mining pool, a task that nobody has looked at
-- doesn't need to have accrued anything.
--
-- quantity and reward are frozen at posting time rather than recomputed on
-- read, because both derive from member count: without that, someone joining
-- halfway through the day would move the goalposts on a player already partway
-- through the task.
CREATE TABLE IF NOT EXISTS daily_jobs (
    guild_id        INTEGER NOT NULL,
    job_date        TEXT NOT NULL,      -- UTC ISO date, same convention as mining_pool_last_topup
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    reward          REAL NOT NULL,      -- the material's market_ceiling_price * quantity
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
    -- ticks/hour), so a level's +1 item/hour is +0.4 items/tick - banking the
    -- remainder here is what stops that bonus being rounded away.
    harvest_progress REAL NOT NULL DEFAULT 0.0,
    is_full          INTEGER NOT NULL DEFAULT 0,    -- 0/1 boolean: stopped until /collect
    -- production_jobs.job_id of a queued /factory upgrade, else NULL. A locked
    -- drill can't be placed, removed, attached to, or queued a second time.
    locked_job_id    INTEGER,
    CHECK (level >= 1),
    -- Buys back what dropping "guild_id NOT NULL" gave up: an unplaced drill
    -- can't be holding materials or be flagged full.
    CHECK (guild_id IS NOT NULL OR (stored_amount = 0 AND is_full = 0))
);
CREATE INDEX IF NOT EXISTS idx_drills_owner ON drills(owner_id);
CREATE INDEX IF NOT EXISTS idx_drills_guild ON drills(guild_id);

-- A queued furnace (smelting), factory (crafting) or press job for a user in a
-- guild. target_id is the material_id being produced (e.g. "iron", "wiring",
-- "ruby").
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
