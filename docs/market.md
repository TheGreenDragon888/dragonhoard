# Server Market & Economy Design

This document describes the design principles behind Dragonhoard's
per-server economy: how currency enters and leaves circulation, how the
server's market functions as an independent economic actor, and how this
groundwork supports a future cross-server exchange. This is a conceptual
design reference, not an implementation spec — treat the mechanics
described here as the intended behavior to evaluate the current codebase
against.

All market menu embeds are yellow, `#FFE600` (see docs/stylization.md).

---

## 1. Faucets and Sinks — Why They Matter

Every source of new currency entering a server's economy is a **faucet**.
Every mechanic that permanently removes currency from circulation is a
**sink**. The health of a server's currency — and by extension, its
eventual value on a cross-server exchange — depends entirely on the
balance between the two.

A faucet without a corresponding sink doesn't just cause "some inflation."
It guarantees that currency accumulates faster than the economy can give it
meaning, and every unit of that currency becomes worth proportionally less
over time. This matters more here than in a typical single-currency game,
because a server's currency isn't just a number players see — it will
eventually be **quoted against other servers**. A server that mints
recklessly and sinks nothing will visibly and measurably devalue against
servers that manage their supply responsibly. Inflation stops being a
vague fairness concern and becomes a directly comparable economic outcome.

Sinks should feel **earned and intuitive**, not punitive. A fee that funds
an infrastructure upgrade (furnace/factory leveling) succeeds because the
player is trading currency for a permanent, visible improvement — it
doesn't feel like a tax, it feels like an investment. Any future sink
should follow this same principle: currency leaves circulation in exchange
for something the player actively wanted, not as an arbitrary toll.

Faucets, meanwhile, should always be tied to genuine economic activity —
mining, smelting, crafting, or trading — rather than passive presence.
Currency entering the economy should reflect real production or effort,
not attendance. This is the reasoning behind removing the passive chat
payout entirely: it rewarded being present in a channel rather than
participating in the mining → smelting → crafting → selling loop the rest
of the economy is built around, and it had no natural rate limit tying its
growth to the server's actual sink capacity. Removing it also removes the
single largest ungoverned faucet in the current design, leaving furnace
fees, factory fees, and the market (below) as the primary — and now much
more legible — forces shaping the money supply.

### The daily job board (1.1)

The job board is the second faucet the bot has, and the only one that isn't
the market itself. Once per day a server posts a task — sell it N units of a
material it is short of — and pays every player who completes it a bonus,
once each, on top of what the sale already paid.

The day rolls over at **midnight Arizona time** (`JOB_BOARD_TIMEZONE` in
`utils/job_board.py`), chosen so the reset lands at a time that means
something to the people playing. Note this is deliberately *not* the mining
pool's schedule, which still tops up on UTC midnight — the two used to share
one definition of "today" and no longer do.

#### How the task is sized (1.2)

The task is sized to **pay just over one unit of the server's currency**
(`JOB_BOARD_TARGET_PAYOUT`): N is the fewest units that clear it. The bonus
is what the server itself would pay for those N units, priced at the stock
level when the job was posted — `sale_unit_price`, the same curve `/market
sell` pays on.

Pinning the payout rather than the quantity is what makes the task the same
size for everybody. Both terms derive from member count and cancel:

```
N = TARGET_PAYOUT / (ceiling × target / (target + stock))
  = TARGET_PAYOUT / ceiling × (1 + stock / target)
```

leaving only `stock / target` — a statement about how well supplied a server
is, not how many people are in it. A five-member server and a five-hundred-
member one at the same fraction of target stock are asked for exactly the
same amount.

This replaced a quantity of 10% of target stock, which scaled with member
count. The task is completed *per player*, so sizing it from a server-wide
total grew one person's quota every time somebody joined while nobody's
mining rate grew to match — and if every member completed it the server took
in a quantity quadratic in its own membership. Rarity scaling survived the
change for free, because `1/ceiling` tracks how common a material is.

`JOB_BOARD_MAX_QUANTITY` (600) caps it. Quantity has no natural bound as
stock climbs — the price decays toward zero, so the units needed to clear a
fixed payout grow without one. It binds past roughly five times target stock,
and once it does the payout falls below target, which is the intended trade:
a task nobody can finish pays nothing at all.

#### Why it is bounded

- **It cannot outpace what the goods are worth.** The bonus is what the
  server would pay for the goods, so completing a job is worth about twice a
  plain sale of the same materials and never more. There is no configuration
  under which it pays for something that wasn't produced.
- **Buying the goods back costs more than it paid.** Priced at the flat
  ceiling (as 1.1 did) the board was very slightly printable: sell the task
  quantity, claim, buy the same goods back at the higher stock your own sale
  created, and the round trip came out ahead. Paying the server's own rate
  closes that once a server holds a real amount of the material. It does not
  close on a thinly stocked one — the leak scales with `N / target_stock`, so
  it is worst on the smallest servers (about +0.02 at a hundred members,
  +0.67 for a lone player). See `job_reward` in `data/materials.py` for the
  measured thresholds and what closing it outright would cost.
- **It requires real production.** The only way to claim it is to put
  materials into the market, which raises the server's stock and lowers the
  price it will pay next time — so the faucet partly closes its own tap.
- **It is capped per player per day.** Not a rate that scales with activity,
  a single fixed payout.
- **Gemstones are excluded from it entirely.** Their ceiling prices run from
  5,500 to 500,000 against ore at 0.01, so a single gemstone task would mint
  more in one day than every other faucet combined. Note the payout target
  does *not* protect against this on its own — N floors at one unit, so
  anything worth more than the target per unit simply pays what that unit is
  worth. As of 1.2 this is no longer the board's own rule: gemstones left the
  market entirely (section 3), and `JOB_BOARD_MATERIALS` is now an alias of
  `TRADEABLE_ORDER`. A job can only be finished by selling, so the board's
  vocabulary and the market's have to be the same list.

Every payout goes through `record_minted`, so section 4's accounting sees
it.

---

## 2. Relevance to the Future Exchange

DragonCoin exists solely as a **unit of measurement** — a stable, common
denominator used to quote and compare the value of different servers'
currencies against one another. It is not currently spendable, earnable,
or exposed through any menu or feature. Its entire purpose, for now, is
conceptual: it is the "USD of forex" that lets a future exchange say
"1 unit of Server A's currency is worth X units of DragonCoin, and 1 unit
of Server B's currency is worth Y" — enabling a meaningful ratio between
the two.

This means the real work of preparing for an exchange isn't building
DragonCoin — it's making sure each server's currency has a value that can
be **honestly and consistently calculated**. That value needs to emerge
from something real: the materials the server has acquired, the currency
it has issued, and the ongoing balance between its faucets and sinks. If a
server's currency isn't backed by anything measurable, there is nothing
for DragonCoin to quote it against.

This is also why the server's market (below) is designed to hold and
value real inventory rather than simply generating currency from nothing.
A server's accumulated stockpile of raw and smelted materials, weighed
against how much currency it has issued to acquire that stockpile, is the
foundation an exchange rate will eventually be built on. Getting this
foundation right now — well before any exchange menu exists — is what
will make that later feature meaningful rather than arbitrary.

---

## 3. The Server Market — An Independent Economic Actor

The server itself should be treated as a participant in its own economy,
not merely a rules engine. It maintains its own **independent storage** of
raw and smelted materials, acquired directly from users, and its own
currency obligations tied to that storage. This is a deliberate design
choice: rather than materials only ever moving between players, the server
now has a balance sheet — it can be materially rich or poor, well-stocked
or scarce, in exactly the same way a player's inventory can be.

**Acquisition (buying from users):** The server purchases raw materials
and smelted materials directly from users. This is the server's way of
building up its own inventory — every purchase is currency leaving the
server's supply (a faucet from the user's perspective) in exchange for a
real, finite good entering the server's storage. The price the server
pays scales with how much of that material it already holds: a scarce
material commands closer to full price, while a material the server is
already flush with is worth progressively less to acquire further. This
prevents the server from being an infinite, flat-rate buyer that users can
farm without limit.

**Disposal (selling back to users):** Once the server holds a material, it
can sell that same material back to users at a markup — specifically, the
current acquisition rate **plus the full ceiling price** on top: double the
ceiling price when the server's stock is empty, tapering down toward (but
never below) the ceiling price as its stock grows. Because the spread is
always exactly one ceiling price, selling to the server and immediately
buying back is never profitable. This spread between acquisition price and
resale price is intentional: it is the server's own sink mechanism. Currency a user pays to buy back a material
is removed from circulation entirely (it does not return to another
player), while the server's stock of that material decreases. This closes
the loop — materials and currency both flow in two directions through the
market, rather than currency only ever being minted and materials only
ever being hoarded.

**A critical constraint:** the server can only sell what it actually
possesses. It cannot conjure inventory from nothing — everything in its
storage traces back to materials legitimately acquired from users. It may,
however, **process** what it already holds: when the shared furnace is
completely idle, the server auto-smelts its own surplus ore (only the
portion above that ore's target stock, steering toward a 4:1 iron:steel
ratio) into smelted materials for its stock, paying the same recipe and
coal costs a player would but no fee, and always yielding the furnace to
players' jobs. It queues this one item at a time rather than a whole
batch in one go — waiting for each item to finish before deciding the
next one — so it can't overshoot the 4:1 target by dumping a large
surplus into a single recipe before re-checking the ratio.

This keeps the market honest: the server is a genuine
market-maker sitting between users, not an unlimited vendor. If no one has
sold a given material (or the ore it smelts from) to the server yet, that
material simply isn't available for purchase, and the server's storage —
not an abstract formula — is the real constraint on what it can offer.

**Scope of tradeable items:** Only **ores and smelted materials** are eligible
to be bought or sold through the server market. Component materials, drills,
and other crafted/finished goods are excluded entirely. This preserves the
distinction covered earlier — the server trades in the inputs to its own
economy (and the users'), not in the finished outputs of it. Allowing finished
goods through the market would undercut the value of crafting and, eventually,
undercut any player-driven trade the future order-book marketplace is meant to
enable.

**Gemstones are excluded too, as of 1.2.** This one was learned the hard way
rather than designed in. A ruby's ceiling price is 5,500 against iron ore at
0.01, and target stock for a gemstone is 1 on any server under about 33
members — so the price curve barely damps successive sales, and the first four
rubies sold into a server paid 5,500 + 2,750 + 1,833 + 1,375 = **11,458**. For
scale, a whole day's job board pays a little over 1.00 and the beta server has
minted 7.92 in its lifetime. One player selling one gem did not distort a
server's economy so much as end it: every price, every fee and every
infrastructure threshold in that server became meaningless in a single command.

The exclusion covers buying as well as selling. Once no server can acquire a
gemstone, leaving them in the buy list would only offer players something no
server will ever have in stock.

Note that this makes gemstones *purely* crafting inputs — drill bits,
containers, drill upgrades, ultra dense matter, and the Mining Focus unlock.
That is the intended shape. A gem's value should be what it builds, not what
it fetches, and there was never a number for the latter that both respected a
one-in-a-million drop rate and left the rest of the economy standing.

The historical damage is repaired by `scripts/revert_gem_sales.py`, a one-time
sweep documented in its own module docstring.

**Target stock and ceiling price:** Each material's "target stock" — the
equilibrium point the pricing curve is built around — scales with the
size of the server itself, calculated from the server's human member count
(bots are never counted) multiplied by that material's own per-member
constant. Rarer materials get a smaller constant, so the server's
equilibrium for, say, rubies stays tiny even on a large server, while a
common material like iron ore scales up properly - materials no longer
share one flat constant. This means a material's expected equilibrium
grows naturally as a server grows, rather than being a single fixed number
that becomes meaningless as membership changes. The
"ceiling price" — the maximum the server will ever pay for a unit of that
material, reached only when the server's current stock is zero — is a
predetermined per-material value, serving as the anchor both the buy price
and the resale price's fixed markup are derived from. The per-unit buy price
decays smoothly with stock: full ceiling price at zero stock, **half** the
ceiling price at exactly target stock, and progressively less beyond it —
approaching but never reaching zero. Target stock is an equilibrium, not a
maximum: the server always accepts a sale and always pays for every unit
sold, just at ever-poorer rates the more it is already holding.

**Looking ahead — user-driven orders:** For now, the market is entirely
server-managed: the server is the counterparty on every transaction,
buying and selling directly against its own storage using the pricing
model described above. This is intended as a foundation, not the final
design. Eventually, users themselves should be able to place buy and sell
orders directly against each other, with the server's own market
activity becoming just one participant among many rather than the sole
mechanism. The acquisition/disposal loop, the pricing curve, and the
storage constraint described here are all designed to keep functioning
usefully even after user-driven orders are introduced — the server simply
becomes one more actor with the same rules everyone else has to follow.

---

## 4. Notable Server Economy Statistics to Track

To evaluate the health of a server's economy — both for internal balance
tuning and as the eventual basis for exchange valuation — the following
figures are worth tracking on an ongoing basis:

- **Total currency in circulation** — the sum of all user currency
  balances within the server, representing the total money supply.
- **Total currency minted (all-time and recent)** — cumulative currency
  created through all faucets (market purchases from users, daily job board
  rewards, and any future faucets), both as a running lifetime total and as
  a recent-window figure to spot acceleration.
- **Total currency burned (all-time and recent)** — cumulative currency
  removed through all sinks (furnace, factory, press and scrapper fees, and
  market resale to users), tracked the same way.
- **Net mint/burn delta** — minted minus burned, ideally tracked over a
  rolling recent window rather than only all-time, since an all-time
  number can mask a recently-worsening trend.
- **Server material storage, by material** — current quantity of each raw
  and smelted material the server holds, which directly drives both the
  buy/sell pricing curve and the server's overall valuation.
- **Total server treasury value** — the current market value of everything
  the server holds, calculated using the same live pricing curve used for
  transactions (not historical acquisition cost), since this is the number
  an exchange rate would eventually be built from.
- **Currency-to-treasury ratio** — circulating currency relative to
  treasury value; a rising ratio signals a currency that is being diluted
  relative to what the server actually holds.
- **Market transaction volume** — number and total value of buy/sell
  transactions through the server market over a given window, indicating
  how actively the market mechanic is actually being used relative to
  other parts of the economy.
- **Infrastructure fee volume** — currency drained through furnace, factory,
  press and scrapper fees over a given window, useful both as a sink metric
  and as a possible future input for calculating other dynamic values.
- **Job board completion rate** — how many of a server's active players
  claim the daily bonus, which is both the size of that faucet and a
  reasonable proxy for how many people are playing on any given day.
- **Active economic participants** — count of users who took an
  economically meaningful action (mined, smelted, crafted, or traded
  through the market) within a recent window, as a healthier engagement
  signal than raw chat activity ever was.