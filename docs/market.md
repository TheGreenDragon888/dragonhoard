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

**Mining slots, added in 1.3, are that principle taken to its end** (see
docs/mining.txt). The same fees that level a machine are also totalled
across every machine, and crossing 25 / 125 / 625 / 3,125 of that lifetime
total lets every player in the server keep one more drill in the ground.
Nothing is deducted twice — both are high-water marks read off totals that
only grow — so the effect is that a server's fee burn now buys a *second*
permanent thing, one that raises production for everybody rather than for
whoever queued the job. That deliberately makes the largest sink in the
game more attractive to feed, which is the direction this section argues a
sink should be pushed.

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
material it is short of — and pays a bonus on top of what the sale already
paid.

The day rolls over at **midnight Arizona time** (`JOB_BOARD_TIMEZONE` in
`utils/job_board.py`), chosen so the reset lands at a time that means
something to the people playing. As of 1.2 this is the only date the game
keeps — the mining pool used to run its own on UTC midnight, and the bag
replaced it.

#### How the task is sized

The task is sized to **pay just over one unit of the server's currency**
(`JOB_BOARD_TARGET_PAYOUT`): N is the fewest units whose sale clears it. Since
prices are static (section 3), N is now a constant of the material —
`job_quantity` takes nothing else. At the current price table that is 100 iron
ore, 50 copper ore, 34 coal, 7 iron, 4 copper or 3 steel.

Two earlier versions of this are worth knowing about, because each fixed a real
problem and the current rule inherits both fixes:

- 1.1 asked for 10% of target stock, which scaled with member count. The task is
  completed *per player*, so sizing it from a server-wide total grew one
  person's quota every time somebody joined while nobody's mining rate grew to
  match.
- 1.2 pinned the payout instead, which made the task member-independent but left
  it growing with the server's stock, because the price it was worked back from
  decayed as the shelves filled. `JOB_BOARD_MAX_QUANTITY` (600) existed to bound
  that, since the units needed to clear a fixed payout grow without limit as a
  price decays toward zero. A flat price removes the growth, so 1.3 removed the
  cap with it.

#### The bonus is paid per completion (1.3)

The bonus is a flat `JOB_BOARD_TARGET_PAYOUT`, and it is paid **every time the
task is completed** rather than once per player per day. Sell three times the
task quantity and it pays three times — in a single command, if that is how it
was sold. `daily_job_progress.claims_paid` counts what has already been paid;
the payout is `sold / quantity - claims_paid`, banked in the same statement that
computes it.

This deliberately removes the faucet's only hard cap, and what replaces it is
arithmetic rather than a limit:

- **A completion never pays more than the goods are worth.** N is the fewest
  units clearing the target payout, so N × price ≥ payout by construction and
  the bonus is at most a second copy of the sale. There is no configuration
  under which it pays for something that wasn't produced.
- **Buying the goods back always costs at least what the pair paid out.** The
  market's resale markup is exactly 2× (section 3), so a round trip of N units
  collects N × price + payout and spends 2 × N × price — break-even at its very
  best, on iron ore and copper ore where N × price is exactly 1.00, and a loss
  on everything else. This is the number that has to be re-checked before
  touching either constant: the payout and `MARKET_BUY_MARKUP` only work as a
  pair. The 1.2 version of this bound leaked on thinly-stocked servers; the flat
  price closes it at every stock level and every server size.
- **It requires real production.** The only way to claim it is to put materials
  into the market. With static prices this no longer damps its own tap — selling
  into the server doesn't lower what it pays next time — so the market is a
  flat-rate buyer of the day's material at roughly double price, all day.
- **Gemstones are excluded from it entirely.** Their prices run from 5,500 to
  500,000 against ore at 0.01, so a single gemstone task would mint more in one
  day than every other faucet combined — and now it would do so per completion.
  Note the payout target does *not* protect against this on its own: N floors at
  one unit, so anything worth more than the target per unit simply pays what
  that unit is worth. As of 1.2 this is no longer the board's own rule:
  gemstones left the market entirely (section 3), and `JOB_BOARD_MATERIALS` is
  now an alias of `TRADEABLE_ORDER`. A job can only be finished by selling, so
  the board's vocabulary and the market's have to be the same list.

Every payout goes through `record_minted`, so section 4's accounting sees it.

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

**Prices are static (1.3).** Each material has one price, the same on every
server, on every day, at every stock level. It is a whole number of cents,
and that is enforced rather than merely intended (`MARKET_PRICE_CENTS` in
`data/materials.py` fails at import otherwise). The current table is 0.01
iron ore, 0.02 copper ore, 0.03 coal, 0.15 iron, 0.30 copper, 0.48 steel —
each smelted price being 150% of its recipe's raw inputs, the same balance
rule as before, now derived rather than hand-maintained.

This replaced a decaying curve: the server paid full price at zero stock,
half at its target stock, and progressively less beyond it. The curve's
purpose was to stop the server being an infinite flat-rate buyer that users
could farm without limit — which it is now — and that trade was made
deliberately. What it cost was that a price could only be discovered by
running the command; a large sale paid a rate the player could not work out
in advance; and every figure quoted anywhere in the game had to say which
stock level it was quoted at. Rounding the prices to whole cents is the same
decision taken to its conclusion: a static price is one a player can multiply
in their head, which a price of 0.010588 is not.

The consequence to keep in view is that **the market no longer damps its own
faucet**. Selling into a server used to lower what it would pay for the next
unit; now it doesn't, so the ceiling on what a server can mint in a day is
whatever its players can mine and carry. The sinks (section 1) are what has to
carry that weight, and section 4's mint/burn accounting is how it gets noticed
if they don't.

Rounding to the cent could not preserve the old ratios, because at these
magnitudes the grid is as coarse as the prices: one cent was 94.4% of iron
ore's entire price, so copper ore's 0.0177 had to land on 0.01 or 0.02 with
nothing available in between. It went to 0.02 (+13.1%), which leaves its ratio
to iron ore at a flat 2 rather than 1.67. Two knock-on effects are documented
where they are read: mining focus conversions (`data/materials.py`,
`MINING_FOCUSES`) and the mining efficiency injection figures
(`docs/mining-efficiency.md`).

**Acquisition (buying from users):** The server purchases raw materials
and smelted materials directly from users at that flat price. This is the
server's way of building up its own inventory — every purchase is currency
leaving the server's supply (a faucet from the user's perspective) in exchange
for a real, finite good entering the server's storage.

**Disposal (selling back to users):** Once the server holds a material, it
can sell that same material back to users at **exactly twice** what it paid
(`MARKET_BUY_MARKUP`). That figure is the old rule's fixed point rather than a
new one: resale used to be the acquisition rate plus one full ceiling price,
which was double at zero stock and tapered toward the ceiling as the shelves
filled — with the acquisition rate now flat *at* that ceiling, "plus one
ceiling" is doubling everywhere. Because the spread is always a full price,
selling to the server and immediately buying back is never profitable, and the
same 2× is what keeps the repeatable job board from printing currency
(section 1). This spread is intentional: it is the server's own sink
mechanism. Currency a user pays to buy back a material is removed from
circulation entirely (it does not return to another player), while the
server's stock of that material decreases. This closes the loop — materials
and currency both flow in two directions through the market, rather than
currency only ever being minted and materials only ever being hoarded.

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

This stays on the furnace and is deliberately not extended to the blast
furnace (1.3). One item at a time is the whole point of it, and the blast
furnace's smallest possible action is a hundred: a twenty-member server's
entire target stock is 83 steel or 200 iron (`data/materials.py:
target_stock`), so a single batch would overshoot the thing this is
steering toward rather than approach it. The market tops its own shelves
up; it does not smelt in bulk.

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
rather than designed in. A ruby's price is 5,500 against iron ore at 0.01, and
target stock for a gemstone stays at 1 on any server of realistic size — so the
decaying curve in force at the time barely damped successive sales, and the
first four rubies sold into a server paid 5,500 + 2,750 + 1,833 + 1,375 =
**11,458**. For scale, one job board completion pays a little over 1.00. One
player selling one gem did not distort a server's economy so much as end it:
every price, every fee and every infrastructure threshold in that server became
meaningless in a single command.

1.3's static prices make this exclusion *more* load-bearing, not less. There is
no curve left to damp anything, so four ruby sales would now pay 5,500 apiece.
The list in `TRADEABLE_ORDER` is the only thing standing between a gemstone and
a server's economy.

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

**Target stock:** Each material's "target stock" — how much of it a server of
a given size is expected to hold — scales with the size of the server itself,
calculated from the server's human member count (bots are never counted)
multiplied by that material's own per-member constant. Rarer materials get a
smaller constant, so the server's expected holding of, say, rubies stays tiny
even on a large server, while a common material like iron ore scales up
properly — materials no longer share one flat constant. This means the figure
grows naturally as a server grows, rather than being a single fixed number
that becomes meaningless as membership changes.

Until 1.3 this was the midpoint of the pricing curve — the stock level at which
the server paid half a material's ceiling price. With prices static it is
purely an inventory figure, and exactly two things still read it: the furnace's
auto-smelt, which only processes ore above it (below), and the job board's
choice of material, which weights each candidate by target / (stock + target)
so "what is this server short of" is answered on the same scale for a
five-member server and a five-hundred-member one. It is not a maximum: the
server always accepts a sale and always pays the same rate for every unit
sold, however much it is already holding.

**Looking ahead — user-driven orders:** For now, the market is entirely
server-managed: the server is the counterparty on every transaction,
buying and selling directly against its own storage using the pricing
model described above. This is intended as a foundation, not the final
design. Eventually, users themselves should be able to place buy and sell
orders directly against each other, with the server's own market
activity becoming just one participant among many rather than the sole
mechanism. The acquisition/disposal loop, the fixed spread, and the
storage constraint described here are all designed to keep functioning
usefully even after user-driven orders are introduced — the server simply
becomes one more actor with the same rules everyone else has to follow. A
static price is arguably a better foundation for that than the curve was: it
is a standing bid and ask the server will always honour, which is exactly the
role a market maker plays among other participants.

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
  removed through all sinks (furnace, blast furnace, factory, press and
  scrapper fees, and market resale to users), tracked the same way.
- **Net mint/burn delta** — minted minus burned, ideally tracked over a
  rolling recent window rather than only all-time, since an all-time
  number can mask a recently-worsening trend.
- **Server material storage, by material** — current quantity of each raw
  and smelted material the server holds. It no longer drives the price (1.3),
  but it is still what the server can sell back and what its valuation is
  built from.
- **Total server treasury value** — the current market value of everything
  the server holds, calculated at the same prices used for transactions (not
  historical acquisition cost), since this is the number an exchange rate
  would eventually be built from. Static prices make this a straight sum
  rather than an integral under a curve.
- **Currency-to-treasury ratio** — circulating currency relative to
  treasury value; a rising ratio signals a currency that is being diluted
  relative to what the server actually holds.
- **Market transaction volume** — number and total value of buy/sell
  transactions through the server market over a given window, indicating
  how actively the market mechanic is actually being used relative to
  other parts of the economy.
- **Infrastructure fee volume** — currency drained through furnace, blast
  furnace, factory, press and scrapper fees over a given window, useful both
  as a sink metric and as a possible future input for calculating other
  dynamic values.
- **Job board completion rate** — how many completions a server's players
  claim in a day. Since 1.3 this is a count rather than a headcount, and it is
  worth watching for that reason: the per-completion payout has no cap, so
  this is the one faucet whose size is now bounded only by how much its
  players can mine.
- **Active economic participants** — count of users who took an
  economically meaningful action (mined, smelted, crafted, or traded
  through the market) within a recent window, as a healthier engagement
  signal than raw chat activity ever was.