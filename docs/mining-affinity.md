Mining Affinity
===============

Read docs/mining.txt (Mining Focus) first, then docs/mining-efficiency.md.
Affinity is the third member of that set and the first one that touches the
gem tier. It is a SEPARATE feature from both: it does not require either, is
not gated behind either, and its options (three gemstones) correspond to
neither of theirs.

Focus decides which ore you dig up. Efficiency decides how much of it you get.
Affinity decides which GEMSTONE the gems you find arrive as — and never how
many of them there are.

Unlocked with a diamond, once. Intended for the end of the game, which is
where a diamond puts it: a ruby is one per 11,111 items mined, an obsidian one
per 111,111 and a diamond one per 1,000,000.


Why the gem tier needed anything
--------------------------------

Gem progression was otherwise all-or-nothing. A bag holds exactly one diamond
(docs/mining.txt), so a player either dug it up or didn't, and the 90 rubies
they found on the way were worth the same mining with no way to trade them
toward it. The press converts ore into gems; nothing converted gems into gems.

Affinity is the focus's argument applied one tier up. A copper ore is worth two
iron ore because iron drops twice as often; a ruby is worth a ninetieth of a
diamond for exactly the same reason, and `focus_conversion_rate` computes both
because both are a ratio of drop chances.


What it does
------------

A player commits to one gemstone. On every collection, every gem in the haul
that ISN'T that one is converted into it at the rarity ratio, times
`MINING_AFFINITY_CONVERSION_BONUS`. The chosen gem itself passes through
untouched. Ores are never touched, by any affinity.

At the live bonus of 2.0 the trades are:

| Target | From rubies | From obsidian | From diamonds |
|---|---|---|---|
| Ruby | — | 1 → 20 | 1 → 180 |
| Obsidian | 5 → 1 | — | 1 → 18 |
| Diamond | 45 → 1 | 9 → 2 | — |

Those are rendered from the rate rather than written into the table
(`affinity_ratio`), so retuning a drop chance or the bonus cannot leave the
picker quoting a trade the game no longer makes.


What it delivers
----------------

**The chosen gem comes out at exactly 5x its natural rate, whichever gem it
is.** Per bag (90 ruby, 9 obsidian, 1 diamond):

| Target | Per bag | Naturally | |
|---|---|---|---|
| Ruby | 450 | 90 | 5x |
| Obsidian | 45 | 9 | 5x |
| Diamond | 5 | 1 | 5x |

That uniformity is not a coincidence and it is the number to reason about when
retuning. A bag's gems are exactly thirds by value — 90 rubies, 9 obsidian and
1 diamond are one diamond-equivalent each, which falls out of the drop chances
being 90:9:1 — so whichever gem is chosen, two thirds of the bag is converted
and one third passes through unbonused. In total gem value that is:

    (2 x BONUS + 1) / 3

which is **1.67x at the live bonus**, identical for all three choices. So the
headline multiplier is NOT the conversion bonus: halving the cost of a
conversion buys 1.67x, not 2x, and a true doubling of gem value would want a
bonus of 2.5. tests/test_mining_affinity.py pins the identity, the 1.67x and
the 45-rubies-to-a-diamond trade separately, so retuning the bonus fails on the
figure and not on the structure.

**The two figures say different things and both are true.** 1.67x is what a
player's gems are worth; 5x is how many of the chosen one they hold. The second
is the one the endgame is priced against, because diamonds gate diamond drill
bits (3 each), drill upgrades (3 per level, doubling), the Diamond Container
and Ultra Dense Matter (10).


Why the rarity ratio, when the prices disagree
----------------------------------------------

Gemstones carry market prices (they are not tradeable — see `TRADEABLE_ORDER` —
but `raw_input_cost` values them when comparing recipes), and those prices are
close to rarity-proportional without being exactly so. Measured against them,
an unbonused conversion lands at:

| Conversion | Received, as a % of what was given |
|---|---|
| 90 Rubies → 1 Diamond | 101.01% |
| 9 Obsidian → 1 Diamond | 105.82% |
| 10 Rubies → 1 Obsidian | 95.45% |
| 1 Diamond → 90 Rubies | 99.00% |
| 1 Diamond → 9 Obsidian | 94.50% |
| 1 Obsidian → 10 Rubies | 104.76% |

So the rarity ratio is within about 6% of price-neutral, and errs in both
directions rather than consistently favouring one.
Left at rarity anyway, for the reason `MINING_FOCUSES` gives for the same
choice one tier down: rarity is a ratio a player can hold in their head, and a
gem is chosen for what it builds rather than for what it fetches.


The carry, and why it is not the focus's carry
-----------------------------------------------

A conversion rarely lands on a whole gem, so the remainder is banked against
the player's next collection — the same primitive (`accrue`) that a focus, a
drill's harvest rate and an efficiency all use.

What differs is what it is worth. A focus owes at most a fraction of one iron
ore, which is a hundredth of a cent. An affinity owes at most a fraction of a
diamond, which is up to 89 rubies of mining. Three rules follow, and all three
are departures from the focus's template:

- **The carry is CONVERTED on a change, never reset.** `set_focus` zeroes its
  carry because the most that can destroy is worthless. Doing that here would
  destroy roughly a bag's worth of mining on a free daily action.
- **It converts at the RARITY ratio, not the bonused one.** Re-bonusing
  already-earned value on every change would let a player pump a fraction of a
  diamond into a whole one by switching back and forth. This is the one rule in
  the feature whose absence is a printer rather than a balance question, and
  `test_switching_back_and_forth_mints_nothing` is what holds it.
- **A change pays out what it frees.** Converting 0.98 of a diamond into rubies
  leaves 88 rubies owed, and a carry is supposed to be a fraction, so the
  whole ones fall out at that moment. This is the only time a player is handed
  gems by a command that isn't `/collect`.

Turning the feature off (choosing None) cashes the progress out in rubies
rather than forfeiting it. Converting down always lands on whole numbers of the
base gem, so what that drops is a fraction of one RUBY — as against a fraction
of a diamond, which is the entire reason this isn't a reset.

A carry only ever exists while converting UP. Every downward rate is a whole
number (an obsidian is 20 rubies, a diamond 180), so a Ruby affinity has
nothing to bank and `affinity_progress` reports nothing for it.


Where the progress is shown
---------------------------

The other two features keep their carries as implementation detail. This one
cannot: a player aiming at a diamond who collects a ruby receives NOTHING in
their haul, because 45 of them make one. The rarest event in the game would
read as an empty receipt. So the carry is surfaced in three places, all
rendering `affinity_progress`:

1. **The `/collect` receipt** — a field naming every gem that fed the
   conversion, the percentage standing, and last, after a blank line, how many
   gems the affinity MADE this collection. This is the one that exists to stop
   a gem find reading as nothing.
2. **`/mine status`** — under "Mining Enhancements", the field that lists all
   three features a player has unlocked and nothing they haven't.
3. **`/affinity`** — the picker itself, which is the only one of the three
   reachable without mining anything and therefore the one a player will check.

Progress is a PERCENTAGE, and that is a correction rather than a preference.
It was quoted in a source gem's units, and the first mixed haul anyone mined
showed why that cannot work: 38 rubies and 3 obsidian under a diamond affinity
banked 0.511 of a diamond and reported "23 of 45 Rubies". The figure was
right and it still read as though the obsidian had contributed nothing, when
it had in fact done 0.667 of that diamond against the rubies' 0.844. One carry
fed by several gems cannot be honestly quoted in any one of them, so it is
quoted in none of them.

It is FLOORED, never rounded — 99.9% reads as 99%, because a line reading 100%
is a line claiming a gem the carry has not earned — and a carry under 1% gets
no line at all rather than a line reading 0%.

A ruby affinity never shows progress, and the guard is structural rather than a
named case: every rate INTO a ruby makes at least one whole ruby, so no
fraction is ever owing. Any future gem commoner than everything converting into
it inherits the same silence.

**The "were created this collection" line answers a question the materials list
cannot.** A diamond in that list is just a diamond; whether the pool handed it
over or 45 rubies were melted into it is invisible there, and it was the first
thing a player asked after the first mixed haul.

**The picker is laid out exactly as /focus and /efficiency are** - one field
per option, "(selected)" in the heading of the one you're on - with less text
above it. Those two menus need sentences (a focus that can't feed steel, an
efficiency your focus can't supply); an affinity's options are exchange rates,
written as icons and counts (`🔴 45 → 💠 1 · ⚫ 9 → 💠 2`), and the rates say
what the feature does on their own. Only None, which has no rates, gets words.

**A player's first diamond raises a notice pointing at `/affinity`**, as their
first ruby and obsidian do for `/focus` and `/efficiency`
(data/notifications.py: GEM_UNLOCK_NOTICES). Like those two, the command is
otherwise invisible until you hold the gem that opens it.


Implementation notes
--------------------

- **Applied at collection**, after the focus and the efficiency, for the reason
  utils/mining_focus.py gives: a drill banks real materials drawn from its
  server's pool, which is what keeps the pool's gemstone guarantee honest.
  Order between the three is arbitrary — the other two touch only ore and this
  touches only gems — and is stated so that it stays arbitrary rather than
  becoming accidental.
- **The pool is untouched.** How many gems a player finds is decided by the
  bag and nothing here changes it. An affinity that boosted the draw would be
  incoherent with a bag containing exactly one diamond, and would take gems
  from the other players sharing it.
- **`/mine remove` converts too**, exactly as it applies the focus: pulling a
  drill early is a collection, and must not be a way to keep rubies an affinity
  says now arrive as something else. Comparing the three while adding this one
  turned up that the same path was not applying the EFFICIENCY, which was the
  mirror fault - not a dodge but a forfeit, costing a player the boost they had
  paid an obsidian for whenever they emptied a drill that way. All three run
  there now, in the order `/collect` runs them.
- **Gem attribution in the production ledger changes for affinity holders
  only.** Gems used to be credited to the exact server their drill dug them out
  of, because nothing touched them. An affinity pools them the way a focus pools
  ore, so they are split back out by what each guild's gems were WORTH — one
  diamond and one ruby are not interchangeable the way two iron ore are. A
  haul whose gems came back unchanged keeps the exact attribution it is
  entitled to.
- **The row is the unlock**, as with both siblings.
- **Global per-user**, as with both siblings: `/collect` empties drills across
  every server in one call, so a per-server setting would convert each drill's
  gems differently inside a single receipt.


How these numbers were derived
------------------------------

All of it from `RAW_MATERIALS` drop chances and `pool_bag_contents()`, with no
live data. A gem's value in diamond-equivalents is its count times
`focus_conversion_rate(gem, "diamond")`; a bag's three gem lines come to 1.0
each, which is what makes the total (2 x BONUS + 1) / 3 and the per-gem yield a
flat 5x. The market-price table above is each conversion's received price over
its given price at the unbonused rarity ratio.
tests/test_mining_affinity.py pins every figure here.
