# The Server Government (1.4)

Every server elects a **Mayor** and a **Treasurer** each week. The Treasurer
sets what the machines charge and how much of it is taxed; the Mayor spends
the tax, and money borrowed against it, on projects. The purpose is community
engagement - a server has something to argue about and organise around - and,
for large servers, a way past the machine-level cost wall.

Code: `utils/government.py` (every rule and every currency movement) and
`cogs/government.py` (commands, embeds, the hourly loop, member events).
Tests: `tests/test_government.py`.

## 1. Offices and elections

- **Admins have no say.** `/setup fee` was removed; nothing in the government
  checks Manage Server. Admins can still kick an officeholder - the bot cannot
  prevent that - and leaving the server vacates an office.
- **Voting is Thursdays only**, midnight to midnight on the job board's
  America/Phoenix clock, announced by a server notice when it opens. The count
  runs at the midnight that ends it: lazily from any government command, and
  from the hourly loop.
- **Who votes:** anyone with a drill that has been placed in the server for at
  least `VOTER_DRILL_DAYS` (7) when voting opens. A drill placed at all is not
  enough, because a player who owns no drill gets a free one on their first
  `/mine place` - an alt account would qualify with one command. That is what
  `drills.placed_at` exists for. A drill that was already placed when the
  update shipped has no `placed_at` and qualifies outright: nobody could have
  placed one to win an election that did not exist yet, and counting those
  drills from the migration instead would have left every existing player a
  week short of the first election.
- **Who can be elected:** a member who has played in the server (held its
  currency or placed a drill there), never a bot, never yourself. A winner who
  has left is skipped at the count.
- **One vote per office per cycle; the latest counts.** Votes are deleted once
  counted, so nothing carries into the next week.
- **Persistence:** an office changes hands only if somebody votes for that
  office. Nobody voting keeps the holder in place.
- **Order:** the Mayor is decided first. The Treasurer ballot skips the new
  Mayor, since nobody holds both. If that leaves nobody, the sitting Treasurer
  stays - unless the sitting Treasurer *is* the new Mayor, in which case the
  seat is empty (`decide`).
- **Ties** go to the incumbent, then to whoever reached the tied count first
  (the latest `cast_at` among their votes), then to the lower id - never to
  chance.
- **No recall.** A Treasurer who sets x4 fees and 100% tax lasts until the next
  Friday; the weekly term is the remedy.

## 2. The Treasurer's settings

| Setting | Values | Default |
| --- | --- | --- |
| Fee multiplier, per machine | x0.25, x0.5, x1, x2, x4 | x1 |
| Tax rate | 0-100% | 0% |
| Bond rate (a one-time premium) | 0-5% | 0% |

A machine's fee is its `config.py` default times its multiplier
(`machine_fee`). There is no per-server fee column: the base is the codebase
default, so retuning a default moves every server at once. The defaults are
the ungoverned server, so a server that never elects anyone plays exactly as it
did before 1.4.

Each setting (each machine's multiplier separately) may change **once per game
day**. Without that, a Treasurer could set x0.25, queue their own jobs - fees
are charged at queue time - and set x4 again.

While the server owes active creditors, the tax cannot drop below the rate in
force when the most recent bond still owed was sold (`tax_floor`). Bondholders
lent against that tax, and it is the only thing that repays them.

Removing `/setup fee` discarded every server's custom fee. In the production
backup of 2026-08-30 (`/opt/dragonhoard/data/backup-2026-08-30-022248.db`, a
live figure not reproducible from this repo) that changed 7 of 24 servers,
including one that had switched the press off with a fee of 999.

## 3. Money flow - the invariant

**Currency leaves the government only as a burn or as a bond repayment.**

- A fee's untaxed share is handled exactly as a whole fee was before 1.4:
  burned, banked to the machine's level and counted toward mining slots.
- The taxed share is **held**, not burned: in the repayment pool while the
  server owes active creditors, in the treasury otherwise. It levels no
  machine and counts toward nothing until the Mayor spends it.
- `spend_treasury` is the only way money leaves the treasury, and every caller
  is a project, so every such payment is a burn (and counts toward mining
  slots). `pay_bondholders` is the only way money leaves the repayment pool.
- `circulating_currency` adds the treasury and the pool back, for the reason it
  adds order and bet escrow back: the money has left players' balances, not the
  economy.

Why that keeps the supply where it was: a bond moves X from a player to the
treasury, the Mayor's project burns X, and later X of tax that would have been
burned repays the player instead. The total burned is unchanged; it only
happens sooner. `tests/test_government.py: SupplyTests` runs that cycle and
checks it.

**The one leak is the premium.** At the 5% cap a bond's premium is 5/105 -
4.76% - of what repays it, and that currency, which would have been burned,
goes to a player. That ceiling is what `MAX_BOND_RATE_PERCENT` exists to hold.

At 100% tax, machines stop levelling from use entirely and level only as fast
as the Mayor funds them. That is a political choice the design leaves to the
server.

## 4. Bonds

- **Denominations:** 1, 5, 10 and 50 units of the server's currency
  (`BOND_DENOMINATIONS_CENTS`). Small because the economies are: in the
  2026-08-30 production backup no server's players held more than 77.46
  between them. Revisit against live data as servers grow.
- **The premium** is fixed at sale; a later rate change affects only later
  bonds. A running rate could grow a debt faster than a low tax repays it.
- **The cap:** a sale is refused if the debt still owed to active creditors
  plus the new bond (premium included) would exceed the tax collected over the
  previous `DEBT_CAP_DAYS` (7) complete game days - "a server can repay
  everything within a week". Today is excluded: it is not over. A server with
  no tax history cannot sell bonds at all, so a new government's first week is
  bond-free. Checked when the Mayor opens a sale, enforced at every purchase.
- **Sales** belong to a Mayor: `/mayor bonds <amount>` opens one (replacing any
  open sale), and it is cancelled when the Mayor changes. The debt belongs to
  the server and carries over.
- **Repayment:** while anything is owed to active creditors, *all* tax goes to
  the repayment pool, and once an hour the pool is split pro-rata on each
  creditor's **remaining** balance - so every active creditor is repaid by the
  same final payout - in whole cents by `apportion()`, the same function that
  splits a bet's pot. The fraction of a cent it cannot split waits for the next
  payout. Once everybody active is repaid, the rest of the pool moves to the
  treasury and new tax follows it.
- **Officeholders may buy bonds.** Repayment is pro-rata, so nobody is paid
  first; the premium leak is capped either way.
- **Leaving freezes, never voids.** A departed creditor's bonds are skipped by
  payouts and ignored by the cap until they return (`on_member_join`, or the
  hourly check for anybody who rejoined while the bot was down). Voiding would
  let an admin wipe the server's debt by kicking its creditors. A banned
  creditor stays frozen for good - admins keep that power, and it is accepted.

Why hourly: machine rates are hourly, so repayment moves at the pace players
already watch, and the loop visits only servers with something in the pool. A
lazy running-total design would avoid the loop, but freezing makes its
bookkeeping much harder.

## 5. The Mayor's projects

All four are burns, and all four count toward mining slots.

### Machine funding

`/mayor fund` is `/donate infrastructure` paid from the treasury. It goes
through `bank_infrastructure_fee`, so it levels the machine and counts 1x
toward slots through the machine's own fee column.

### Infrastructure Enhancement

Each level doubles one machine's speed on top of what its own level gives it:
the rate a loop runs at is `effective_level x 2^enhancement` (`run_level`). The
first costs `ENHANCEMENT_PRICE_BASE` (1,000) and each after it five times the
last.

Machines really are shared - the furnace runs one server-wide queue in
`queued_at` order - so a big server's machines are its bottleneck, and this is
aimed at those servers. Against the level ladder it is cheap: doubling a
machine by levelling it means climbing from level L to 2L, which on
`upgrade_threshold` costs 150 more fees from level 2, 3,875 from level 3,
97,500 from level 4 and 2,440,625 from level 5. Against income it is not: in
the production backups of 2026-08-18 and 2026-08-30 (11.14 days apart; live
figures) the busiest server banked 85.3 of fees a week, so 1,000 is 11.7 weeks
of its entire fee flow at 100% tax. That was judged right for a feature meant
to be a large, congested server's multi-week goal.

Enhancements raise speed only, not the queue cap.

### Mining Slot Enhancement

Treasury money spent here counts `MINING_SLOT_ENHANCEMENT_MULTIPLIER` (5) times
over toward the mining slot ladder and levels no machine. The slot ladder's
base is five times the machine ladder's and they share their step, so at 5x a
slot rung costs exactly what the same machine rung does. docs/mining.txt
prices slots above machine levels on purpose; the multiplier was judged a fair
trade rather than a way around that, because this money buys the slot alone,
where the same money paid as fees would have levelled a machine as well.

### Server Bonanza

For `BONANZA_HOURS` (48), every drill in the server mines and every machine
runs at double speed. It cannot be bought while one is running, and it
multiplies with enhancements.

**Price: half the server's 7-day GDP, never less than 50** (`bonanza_price`).

- *It stays a net sink.* Doubling everything for 48 of a week's 168 hours adds
  at most 2/7 of a week's output (about 0.29 of the week's GDP). Any share of
  2/7 or more takes out at least as much currency as that extra output could
  mint by being sold; at 0.5 it returns at most 57% of the price.
- *Gaming it does not pay.* Idling to depress GDP costs a server that output
  one-for-one and saves only half as much on the price - true of any share
  below 1.
- *It scales with the server* above the minimum, which sets the price for every
  server whose 7-day GDP is under 100.
- *Gaps:* GDP counts mining, the furnace and the blast furnace only
  (`GDP_SOURCES`), so extra factory, press and scrapper output is unpriced, and
  gemstones are excluded.
- *History:* the production ledger ships in the same release, and a ledger
  younger than a week under-reads the 7-day window, so a Bonanza is not offered
  until the server has `BONANZA_HISTORY_DAYS` (7) of ledger history.

## 6. Ruled out

- **A lottery seeded from the treasury.** It would be the one project that is
  not a burn - tax money handed to one player - and was removed from this
  feature set. A `/lottery` command may come separately.
- **Voiding debts when a creditor leaves** - see Bonds.
